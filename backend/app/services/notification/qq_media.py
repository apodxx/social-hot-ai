"""QQ 群聊富媒体（图片）上传与发送 —— 官方 API v2。

为什么必须是分片上传：官方给了两条路——URL 上传和分片上传。URL 上传要求图片**已在公网
可访问**，而我们的图片是本地文件、这台机器没有公网地址，所以那条路根本走不通。
官方文档也明确说分片上传的意义就在于"无需开发者提供公网 CDN 地址"。

四步（字段名全部来自官方文档，不是猜的）：

```
1. POST /v2/groups/{group_id}/upload_prepare
   {file_type, file_size, file_name, md5, sha1, md5_10m}
   -> {upload_id, block_size, parts:[{index, presigned_url, block_size}], upload_config}
2. PUT  每个 presigned_url（分片内容）
3. POST /v2/groups/{group_id}/upload_part_finish
   {upload_id, part_index, block_size, md5}
4. POST /v2/groups/{group_openid}/files
   {file_type, srv_send_msg:false, file_name, upload_id}
   -> {file_uuid, file_info, ttl}
```

拿到 `file_info` 后用 `msg_type=7` + `media.file_info` 发送。

三个容易踩的点：

* ``file_size``/``block_size`` 在协议里是**字符串**，传数字可能被判参数错误；
* ``md5_10m`` 是**文件前 10002432 字节**的 MD5——我们的图都小于这个值，所以等于整文件 MD5，
  但代码按定义实现，避免哪天传大图时算错；
* ``file_info`` **有时效**（``ttl``，示例里 300 秒），所以上传后要尽快发送，不能缓存复用；
* 图片只支持 **png/jpg**（官方表格），webp/gif 会被降级或报错——我们素材库里最多的恰恰是
  webp，所以这条必须显式检查并说清楚，而不是等接口报错。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: 官方限制：图片 png/jpg，软限制 20MB、硬限制 200MB。我们只发图片。
IMAGE_FILE_TYPE = 1
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
#: 需要先转成 jpg 才能发的格式。**这不是边角情况**：素材库里 453 张图里有 370 张是 webp，
#: 而 QQ 富媒体图片只吃 png/jpg。不做转换的话"发送原帖图文"会退化成只发文字。
CONVERTIBLE_EXTENSIONS = (
    ".webp",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".heic",
    ".heif",
    ".avif",
)
#: 转换后的缓存目录（按内容哈希命名，重复推送同一张图不会重复转换）。
CONVERTED_SUBDIR = "converted"
SOFT_LIMIT_BYTES = 20 * 1024 * 1024
#: md5_10m 的定义：文件前 10002432 字节。
MD5_10M_WINDOW = 10_002_432


class QQMediaError(RuntimeError):
    """上传或发送失败。绝不假装成功。"""


@dataclass
class FileDigests:
    """预上传需要的三个校验值。"""

    size: int
    md5: str
    sha1: str
    md5_10m: str

    def as_prepare_body(self, *, file_name: str, file_type: int = IMAGE_FILE_TYPE) -> dict[str, Any]:
        # size 与 block_size 在协议里是字符串。
        return {
            "file_type": file_type,
            "file_size": str(self.size),
            "file_name": file_name,
            "md5": self.md5,
            "sha1": self.sha1,
            "md5_10m": self.md5_10m,
        }


def digest_file(path: Path) -> FileDigests:
    """一次读取算出 size / md5 / sha1 / md5_10m。"""
    data = path.read_bytes()
    return FileDigests(
        size=len(data),
        md5=hashlib.md5(data).hexdigest(),
        sha1=hashlib.sha1(data).hexdigest(),
        md5_10m=hashlib.md5(data[:MD5_10M_WINDOW]).hexdigest(),
    )


@dataclass
class MediaUploadResult:
    """一次图片上传的结果。"""

    ok: bool = False
    file_info: str = ""
    file_uuid: str = ""
    ttl: int = 0
    file_name: str = ""
    parts_uploaded: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "file_info": self.file_info,
            "file_uuid": self.file_uuid,
            "ttl": self.ttl,
            "file_name": self.file_name,
            "parts_uploaded": self.parts_uploaded,
            "error": self.error,
        }


def content_type_for(path: Path) -> str:
    """上传分片时必须带的真实 MIME 类型。

    实测：带 ``application/octet-stream`` 时，连一张 512×512 的标准 PNG 都会在合并阶段
    被拒为 ``850019 富媒体文件格式不支持``——所有格式无一例外地失败，说明被检查的不是
    文件内容而是这个元数据。所以按扩展名给出真实类型。
    """
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    return "image/jpeg"


def check_image(path: Path) -> None:
    """本地校验，**在发请求之前**。

    QQ 富媒体图片只吃 png/jpg。素材库里下载下来的图大多是 webp，所以调用方应先经过
    :func:`ensure_sendable` 转换；这里只负责在最后一道确认格式与大小。
    """
    if not path.is_file():
        raise QQMediaError(f"图片不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        hint = (
            "（可先转成 jpg）" if suffix in CONVERTIBLE_EXTENSIONS else ""
        )
        raise QQMediaError(
            f"QQ 富媒体图片只支持 png/jpg，这张是 {suffix or '未知格式'}：{path.name}{hint}"
        )
    size = path.stat().st_size
    if size > SOFT_LIMIT_BYTES:
        raise QQMediaError(
            f"图片 {size / 1024 / 1024:.1f}MB 超过 20MB 软限制（超过会被降级成文件消息）：{path.name}"
        )


def convert_to_jpg(source: Path, target_dir: Path) -> Path:
    """把任意 Pillow 能读的图片转成 jpg，按内容哈希缓存。

    用内容哈希命名，所以同一张图被推送多次只会转换一次；转换结果留在
    ``media/converted/``，与原图分开，不污染素材库。
    """
    data = source.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / f"{fingerprint}.jpg"
    if destination.is_file():
        return destination

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - declared in requirements
        raise QQMediaError(
            "需要 Pillow 才能把 webp 转成 jpg 发送：pip install Pillow"
        ) from exc

    try:
        with Image.open(source) as image:
            # JPEG 不支持透明通道，带 alpha 的图要先落到白底，否则保存会报错。
            if image.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                converted = image.convert("RGBA")
                background.paste(converted, mask=converted.split()[-1])
                frame = background
            else:
                frame = image.convert("RGB")
            # 超过 2000px 的长边对 QQ 预览没有意义，只会让上传更慢更大。
            if max(frame.size) > 2000:
                frame.thumbnail((2000, 2000), Image.LANCZOS)
            frame.save(destination, "JPEG", quality=90, optimize=True)
    except QQMediaError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow 对损坏文件的异常种类很多
        raise QQMediaError(f"无法转换 {source.name}：{type(exc).__name__}: {exc}") from exc
    logger.info("已把 %s 转成 jpg 用于 QQ 发送（%d 字节）", source.name, destination.stat().st_size)
    return destination


def ensure_sendable(path: Path, *, cache_dir: Path) -> Path:
    """返回一个 QQ 能接受的 png/jpg 路径：本来就是就原样返回，否则转换。"""
    if not path.is_file():
        raise QQMediaError(f"图片不存在：{path}")
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return path
    if path.suffix.lower() not in CONVERTIBLE_EXTENSIONS:
        raise QQMediaError(
            f"不认识的图片格式 {path.suffix or '（无扩展名）'}：{path.name}"
        )
    return convert_to_jpg(path, cache_dir)


async def upload_group_image(
    client: httpx.AsyncClient,
    *,
    group_openid: str,
    path: Path,
    headers: dict[str, str],
) -> MediaUploadResult:
    """把一张本地图片上传到群聊富媒体，返回可用于发送的 ``file_info``。"""
    result = MediaUploadResult(file_name=path.name)
    try:
        check_image(path)
    except QQMediaError as exc:
        result.error = str(exc)
        return result

    digests = digest_file(path)

    # --- step 1: 预上传 -------------------------------------------------------
    try:
        prepare = await client.post(
            f"/v2/groups/{group_openid}/upload_prepare",
            json=digests.as_prepare_body(file_name=path.name),
            headers=headers,
        )
    except httpx.HTTPError as exc:
        result.error = f"预上传请求失败：{type(exc).__name__}: {exc}"
        return result
    if prepare.status_code >= 400:
        result.error = f"预上传被拒绝：{_explain(prepare)}"
        return result
    plan = prepare.json() if prepare.content else {}
    upload_id = str(plan.get("upload_id") or "")
    parts = plan.get("parts") or []
    if not upload_id or not isinstance(parts, list) or not parts:
        result.error = f"预上传响应缺少 upload_id/parts：{str(plan)[:200]}"
        return result

    # --- step 2 & 3: 逐片 PUT，然后通知服务端 ---------------------------------
    #
    # **不要把 ``index`` 当成 0 基偏移量。** 官方文档写"分片序号，从 0 开始"，但实测
    # 返回的是 ``index: 1``；按 ``index * block_size`` 切片会让第一片从文件末尾开始，
    # PUT 出 **0 字节**，随后合并报一个误导性的 ``850019 富媒体文件格式不支持``
    # （实测：连 1881 字节的标准 PNG 都"格式不支持"，因为上传的其实是空对象）。
    #
    # 所以偏移量按**排序后各片自身大小累加**得出：对 0 基/1 基序号都正确，
    # 也天然处理最后一片较小的情况。
    data = path.read_bytes()
    ordered = sorted(parts, key=lambda item: int(item.get("index") or 0))
    offset = 0
    for position, part in enumerate(ordered):
        remaining = len(data) - offset
        size = int(part.get("block_size") or 0) or remaining
        chunk = data[offset : offset + size]
        offset += len(chunk)
        part_index = int(part.get("index") or position)
        presigned = str(part.get("presigned_url") or "")
        if not presigned:
            result.error = f"分片 {part_index} 没有 presigned_url"
            return result
        if not chunk:
            # 空分片一定会以"格式不支持"的形式在合并阶段爆出来，在这里就说清楚。
            result.error = (
                f"分片 {part_index} 切出来是空的（文件 {len(data)} 字节，"
                f"已用偏移 {offset - len(chunk)}，声明的 block_size {size}）"
            )
            return result
        try:
            # PUT 到对象存储，**不带 Authorization**：预签名 URL 自身已授权，
            # 多带 QQ 的 token 反而可能让签名校验失败。Content-Type 必须是真实的图片类型。
            put = await client.put(
                presigned,
                content=chunk,
                headers={"Content-Type": content_type_for(path)},
            )
        except httpx.HTTPError as exc:
            result.error = f"分片 {part_index} 上传失败：{type(exc).__name__}: {exc}"
            return result
        if put.status_code >= 400:
            result.error = f"分片 {part_index} PUT 被拒绝：HTTP {put.status_code} {put.text[:120]}"
            return result

        try:
            finish = await client.post(
                f"/v2/groups/{group_openid}/upload_part_finish",
                json={
                    "upload_id": upload_id,
                    "part_index": part_index,
                    "block_size": str(len(chunk)),
                    "md5": hashlib.md5(chunk).hexdigest(),
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            result.error = f"分片 {part_index} 确认失败：{type(exc).__name__}: {exc}"
            return result
        if finish.status_code >= 400:
            result.error = f"分片 {part_index} 确认被拒绝：{_explain(finish)}"
            return result
        result.parts_uploaded += 1

    if offset != len(data):
        result.error = f"分片覆盖不完整：上传了 {offset} 字节，文件是 {len(data)} 字节"
        return result

    # --- step 4: 合并 ---------------------------------------------------------
    try:
        merge = await client.post(
            f"/v2/groups/{group_openid}/files",
            json={
                "file_type": IMAGE_FILE_TYPE,
                "srv_send_msg": False,
                "file_name": path.name,
                "upload_id": upload_id,
            },
            headers=headers,
        )
    except httpx.HTTPError as exc:
        result.error = f"合并请求失败：{type(exc).__name__}: {exc}"
        return result
    if merge.status_code >= 400:
        result.error = f"合并被拒绝：{_explain(merge)}"
        return result
    body = merge.json() if merge.content else {}
    # 200 也可能是错误：QQ 会在 200 响应体里放 ``{"code": 850018, ...}``。只看
    # file_info 会把它报成"响应缺少 file_info"，用户拿不到任何可行动的信息。
    provider_error = _provider_error(body)
    if provider_error:
        result.error = f"合并失败：{provider_error}"
        return result
    result.file_info = str(body.get("file_info") or "")
    result.file_uuid = str(body.get("file_uuid") or "")
    result.ttl = int(body.get("ttl") or 0)
    if not result.file_info:
        result.error = f"合并响应没有 file_info：{str(body)[:200]}"
        return result
    result.ok = True
    logger.info(
        "QQ 图片已上传：%s -> file_uuid=%s ttl=%ss（分片 %d）",
        path.name,
        result.file_uuid,
        result.ttl,
        result.parts_uploaded,
    )
    return result


#: 把官方错误码翻译成能行动的一句话。
ERROR_HINTS = {
    "850018": "群被禁言或机器人被禁言",
    "850019": "不支持的文件格式（图片只支持 png/jpg）",
    "850031": "文件超过大小限制",
    "850027": "发送数据超时，稍后重试",
    "40093001": "上传通道异常，重试",
    "40093002": "超过今天发送文件容量上限，明天再试",
    "11244": "机器人不在该群 / 群 openid 不对",
    "11253": "主动消息被限制（用户可能关闭了允许主动发送）",
    # 实测遇到：机器人没有主动消息权限。解决办法是带 msg_id 走被动回复
    # （用户先 @ 一次机器人），或在后台申请主动消息权限。
    "40034105": "主动消息无权限：需要先用 @机器人 触发一次，再带 msg_id 被动回复；或在后台开通主动消息权限",
    "40034003": "消息被安全拦截（内容可能触发了风控）",
    "40054005": "重复消息（msg_seq 需要递增）",
}


def _provider_error(body: Any) -> str:
    """从 200 响应体里认出错误（QQ 会把错误码放在成功状态码的响应里）。"""
    if not isinstance(body, dict):
        return ""
    code = body.get("code")
    if code in (None, 0, "0", ""):
        return ""
    message = str(body.get("message") or body.get("msg") or "")
    hint = ERROR_HINTS.get(str(code), "")
    text = f"code={code}"
    if message:
        text += f" {message}"
    if hint:
        text += f"（{hint}）"
    return text


def _explain(response: httpx.Response) -> str:
    """把 QQ 的错误响应转成一句能行动的话。"""
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = f"HTTP {response.status_code}"
    if isinstance(body, dict):
        code = str(body.get("code") or "")
        message = str(body.get("message") or "")
        if code:
            detail += f" code={code}"
        if message:
            detail += f" {message}"
        hint = ERROR_HINTS.get(code, "")
        if hint:
            detail += f"（{hint}）"
    else:
        detail += f" {response.text[:120]}"
    return detail


__all__ = [
    "IMAGE_EXTENSIONS",
    "IMAGE_FILE_TYPE",
    "MD5_10M_WINDOW",
    "FileDigests",
    "MediaUploadResult",
    "QQMediaError",
    "check_image",
    "content_type_for",
    "digest_file",
    "upload_group_image",
]
