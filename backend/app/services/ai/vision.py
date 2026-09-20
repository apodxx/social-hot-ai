"""视觉理解：OCR 与全模态（视频）识别，走 DashScope 的 **OpenAI 兼容接口**。

**不装 openai SDK。** 这个接口就是 ``POST {base}/chat/completions`` 加一个 Bearer，
用项目已有的 httpx 直接调即可——少一个依赖，而且能沿用项目处处使用的
``trust_env=False``（否则本机注册表的系统代理会劫持请求）。

两个端点必须分清（实测）：
  * **OCR** —— 私有部署域名（``ws-*.cn-beijing.maas.aliyuncs.com``），单独配 ``OCR_BASE_URL``；
  * **Omni** —— 标准 ``dashscope.aliyuncs.com``。

图片既接受本地路径也接受 URL：本地图要转成 data URI 才能塞进消息里。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 单图转 data URI 的体积上限。太大就传不动，也没必要。
MAX_INLINE_BYTES = 8 * 1024 * 1024


@dataclass
class VisionResult:
    """一次视觉调用的结果。"""

    text: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def to_data_uri(path: str | Path) -> str:
    """本地图片 → data URI。**模型端点拉不到你本机的文件，必须内联。**"""
    resolved = Path(path)
    blob = resolved.read_bytes()
    if len(blob) > MAX_INLINE_BYTES:
        raise ValueError(f"图片 {resolved.name} 有 {len(blob) // 1024}KB，超过内联上限")
    guessed = mimetypes.guess_type(resolved.name)[0] or "image/jpeg"
    return f"data:{guessed};base64,{base64.b64encode(blob).decode('ascii')}"


def image_content(images: list[str], settings: Any) -> list[dict[str, Any]]:
    """把图片（本地路径或 URL）转成消息里的 content 片段。"""
    parts: list[dict[str, Any]] = []
    base = Path(settings.media_root_path).parent
    for item in images:
        if item.startswith(("http://", "https://")):
            url = item
        else:
            local = Path(item)
            if not local.is_absolute():
                local = base / item
            url = to_data_uri(local)
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


async def _chat(
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    timeout: float = 120.0,
    extra: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> VisionResult:
    import httpx

    if not base_url:
        return VisionResult(error="端点未配置")
    if not api_key:
        return VisionResult(error="DASHSCOPE_API_KEY 未配置")

    body: dict[str, Any] = {"model": model, "messages": messages}
    # **必须显式给足输出额度。** 不给的话用服务端默认值，多图 OCR 的输出会被截断——
    # 表现是"只识别出部分图片的内容"，而用户完全看不出被截断了。
    if max_tokens:
        body["max_tokens"] = max_tokens
    if extra:
        body.update(extra)

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as exc:  # noqa: BLE001
        return VisionResult(error=f"{type(exc).__name__}: {exc}")

    if response.status_code >= 400:
        return VisionResult(error=f"HTTP {response.status_code}: {response.text[:200]}")

    try:
        payload = response.json()
    except ValueError as exc:
        return VisionResult(error=f"返回不是 JSON：{exc}")

    choices = payload.get("choices") or []
    if not choices:
        return VisionResult(error=f"返回里没有 choices：{str(payload)[:200]}")
    message = choices[0].get("message") or {}
    # 有些模型把结果放在 reasoning_content，正文为空时也要拿到东西。
    text = (message.get("content") or "").strip()
    if not text:
        text = (message.get("reasoning_content") or "").strip()
    usage = payload.get("usage") or {}
    finish = choices[0].get("finish_reason") or ""
    result = VisionResult(
        text=text,
        model=payload.get("model") or model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )
    # 截断了要**说出来**：否则用户看到的是"怎么只识别出一部分图片"，
    # 而真正的原因是输出额度用完了（实测踩到过）。
    if finish == "length":
        result.error = (
            f"输出被截断（finish_reason=length，已用满 {result.completion_tokens} tokens）——"
            "识别结果不完整，可以减少图片数量或调大 max_tokens"
        )
    return result


OCR_PROMPT = (
    "请完整、准确地输出这些图片中的所有文字内容。"
    "按图片顺序分段，保留原有的标题与列表结构，**不要翻译、不要总结、不要补充**。"
    "如果某张图没有文字，写「（本图无文字）」。"
)


async def ocr_images(
    images: list[str],
    *,
    settings: Any,
    prompt: str = OCR_PROMPT,
) -> VisionResult:
    """识别图片里的文字。图片可以是本地路径（素材库相对路径）或 URL。"""
    if not images:
        return VisionResult(error="没有可识别的图片")
    limit = max(1, int(getattr(settings, "ocr_max_images", 4)))
    selected = images[:limit]
    try:
        content = image_content(selected, settings)
    except Exception as exc:  # noqa: BLE001
        return VisionResult(error=f"图片读取失败：{exc}")
    content.append({"type": "text", "text": prompt})
    result = await _chat(
        settings.ocr_base_url,
        settings.ocr_model,
        [{"role": "user", "content": content}],
        api_key=settings.dashscope_api_key,
        # 多图密集文字的输出会很长（实测 7 张图能出 3000+ 字），给足额度。
        max_tokens=8192,
    )
    logger.info(
        "ocr %d image(s) via %s -> %d chars (prompt %d / completion %d)",
        len(selected),
        result.model,
        len(result.text),
        result.prompt_tokens,
        result.completion_tokens,
    )
    return result


VIDEO_PROMPT = (
    "请完整描述这段视频：画面内容、字幕/口播文字、以及它在讲什么。"
    "如果有文字信息，逐条列出。不要评价，只做客观转述。"
)


async def understand_video(video_url: str, *, settings: Any, prompt: str = VIDEO_PROMPT) -> VisionResult:
    """理解一段视频（全模态模型）。``video_url`` 必须是可公开访问的地址。"""
    if not video_url.startswith(("http://", "https://")):
        return VisionResult(error="视频需要是可公开访问的 http(s) 地址")
    content = [
        {"type": "video_url", "video_url": {"url": video_url}},
        {"type": "text", "text": prompt},
    ]
    return await _chat(
        settings.omni_base_url,
        settings.omni_model,
        [{"role": "user", "content": content}],
        api_key=settings.dashscope_api_key,
        timeout=300.0,
        # 用户给的示例里明确写了：该模型仅支持文本输出。
        extra={"modalities": ["text"]},
    )
