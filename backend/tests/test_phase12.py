"""Phase 12: pushing 原帖 + 二创 图文 to a QQ group through the official Bot API.

No test sends anything: the QQ API is mocked with ``respx`` and the presigned PUT goes to
a fake object store. What is asserted is the part that silently breaks — the four-step
chunked upload's request bodies (``file_size``/``block_size`` are **strings**, ``md5_10m``
is the first 10002432 bytes), that images become ``msg_type=7`` messages carrying
``file_info``, and that a webp is refused *before* any request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.services.notification.qq import QQBotChannel
from app.services.notification.qq_media import (
    MD5_10M_WINDOW,
    QQMediaError,
    check_image,
    digest_file,
    upload_group_image,
)

BASE = "https://api.sgroup.qq.com"
GROUP = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

#: A real 1x1 PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def qq_settings(monkeypatch, tmp_path):
    """QQ configured for a group, with a media dir under tmp_path."""
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_APP_ID", "102000001")
    monkeypatch.setenv("QQ_APP_SECRET", "test-secret")
    monkeypatch.setenv("QQ_GROUP_OPENID", GROUP)
    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


def _token_route() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 7200})
    )


def _prepare_route(parts: int = 1) -> None:
    respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
        return_value=httpx.Response(
            200,
            json={
                "upload_id": "upload_abc",
                "block_size": "1000000",
                "parts": [
                    {
                        "index": index,
                        "presigned_url": f"https://cos.example/part{index}",
                        "block_size": "1000000",
                    }
                    for index in range(parts)
                ],
                "upload_config": {"concurrency": 1, "retry_timeout": 300, "retry_delay": 1},
            },
        )
    )


# ------------------------------------------------------------------- digests
def test_digests_match_the_documented_definitions(tmp_path):
    """``md5_10m`` is the first 10002432 bytes — not "the first 10 MB"."""
    path = tmp_path / "img.png"
    path.write_bytes(PNG)
    digests = digest_file(path)
    assert digests.size == len(PNG)
    assert digests.md5 == hashlib.md5(PNG).hexdigest()
    assert digests.sha1 == hashlib.sha1(PNG).hexdigest()
    # Smaller than the window, so it is the whole file.
    assert digests.md5_10m == hashlib.md5(PNG).hexdigest()

    body = digests.as_prepare_body(file_name="img.png")
    # The protocol wants strings here; integers get rejected as a parameter error.
    assert body["file_size"] == str(len(PNG))
    assert isinstance(body["file_type"], int) and body["file_type"] == 1
    assert body["file_name"] == "img.png"
    assert MD5_10M_WINDOW == 10_002_432


def test_a_large_file_hashes_only_the_md5_10m_window(tmp_path):
    """The window is a prefix, not the whole file — so it must differ for a big file."""
    path = tmp_path / "big.bin"
    payload = b"\x00" * (MD5_10M_WINDOW + 1000)
    path.write_bytes(payload)
    digests = digest_file(path)
    assert digests.md5_10m == hashlib.md5(payload[:MD5_10M_WINDOW]).hexdigest()
    assert digests.md5_10m != digests.md5, "the prefix must not equal the whole file"


@pytest.mark.asyncio
async def test_part_index_is_one_based_in_practice(qq_settings, tmp_path):
    """**The bug that made every image fail.** 文档说分片序号从 0 开始，实测返回的是 1。

    ``index * block_size`` 于是让第一片从文件末尾开始，PUT 出 **0 字节**，而合并阶段报的是
    误导性的 ``850019 富媒体文件格式不支持``——连 1881 字节的标准 PNG 都"格式不支持"。
    偏移量必须按各片自身大小累加，对 0 基/1 基都正确。
    """
    image = tmp_path / "cover.png"
    payload = PNG * 3
    image.write_bytes(payload)

    with respx.mock:
        # index starts at 1, and block_size happens to equal the whole file (as QQ does
        # for small images).
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_id": "upload_x",
                    "block_size": str(len(payload)),
                    "parts": [
                        {
                            "index": 1,
                            "presigned_url": "https://cos.example/p1",
                            "block_size": str(len(payload)),
                        }
                    ],
                    "upload_config": {"concurrency": 1},
                },
            )
        )
        put = respx.put("https://cos.example/p1").mock(return_value=httpx.Response(200))
        finish = respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"file_uuid": "u", "file_info": "FI", "ttl": 300})
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client, group_openid=GROUP, path=image, headers={}
            )

    assert result.ok, result.error
    # The decisive assertion: the PUT carried the whole file, not nothing.
    sent = put.calls[0].request.content
    assert sent == payload, f"PUT sent {len(sent)} bytes, expected {len(payload)}"
    # part_index is echoed back as the provider gave it (1), not renumbered.
    assert json.loads(finish.calls[0].request.content)["part_index"] == 1


@pytest.mark.asyncio
async def test_an_empty_chunk_fails_loudly(qq_settings, tmp_path):
    """A zero-byte part must be reported where it happens, not as "format unsupported"."""
    image = tmp_path / "cover.png"
    image.write_bytes(PNG)

    with respx.mock:
        # A part that claims a size the file cannot fill.
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_id": "upload_x",
                    "block_size": "999999",
                    "parts": [
                        {"index": 1, "presigned_url": "https://cos.example/a", "block_size": "999999"},
                        {"index": 2, "presigned_url": "https://cos.example/b", "block_size": "999999"},
                    ],
                },
            )
        )
        respx.put("https://cos.example/a").mock(return_value=httpx.Response(200))
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client, group_openid=GROUP, path=image, headers={}
            )
    assert not result.ok
    assert "空的" in result.error or "覆盖不完整" in result.error


@pytest.mark.asyncio
async def test_multi_part_upload_splits_by_accumulated_size(qq_settings, tmp_path):
    """Multiple parts must tile the file exactly once, with no gap or overlap."""
    image = tmp_path / "big.png"
    payload = bytes(range(256)) * 40  # 10240 bytes
    image.write_bytes(payload)
    half = len(payload) // 2

    with respx.mock:
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_id": "upload_x",
                    "block_size": str(half),
                    "parts": [
                        # Deliberately out of order and 1-based.
                        {"index": 2, "presigned_url": "https://cos.example/p2", "block_size": str(len(payload) - half)},
                        {"index": 1, "presigned_url": "https://cos.example/p1", "block_size": str(half)},
                    ],
                },
            )
        )
        first = respx.put("https://cos.example/p1").mock(return_value=httpx.Response(200))
        second = respx.put("https://cos.example/p2").mock(return_value=httpx.Response(200))
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"file_uuid": "u", "file_info": "FI", "ttl": 300})
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client, group_openid=GROUP, path=image, headers={}
            )

    assert result.ok, result.error
    assert result.parts_uploaded == 2
    # Reassembling the parts in index order must reproduce the file exactly.
    assert first.calls[0].request.content + second.calls[0].request.content == payload


@pytest.mark.asyncio
async def test_msg_seq_continues_across_parts(qq_settings, tmp_path):
    """回复同一个 msg_id 的多条消息共享一个序号空间。

    真实踩到过两次：第二部分又从 1 开始编号，撞上第一部分的文本，官方报
    ``40054005 消息被去重``。第一次是 ``push_item`` 修了，但命令行脚本自己写了一遍
    循环、没传 ``start_seq``，于是同样的问题又出现——现在两边都走 ``send_parts``。
    """
    from app.services.notification.push import PartReport, send_parts

    media_root = qq_settings.media_root_path
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "a.png").write_bytes(PNG)

    with respx.mock:
        _token_route()
        _prepare_route()
        respx.put("https://cos.example/part0").mock(return_value=httpx.Response(200))
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"file_uuid": "u", "file_info": "FI", "ttl": 300})
        )
        messages = respx.post(f"{BASE}/v2/groups/{GROUP}/messages").mock(
            return_value=httpx.Response(200, json={"id": "m"})
        )
        channel = QQBotChannel(qq_settings)
        parts = [
            PartReport(label="二创", text="第一部分", image_paths=["media/a.png"]),
            PartReport(label="原帖", text="第二部分", image_paths=["media/a.png"]),
        ]
        used = await send_parts(channel, parts, passive_id="ROBOT1.0.msg")

    bodies = [json.loads(call.request.content) for call in messages.calls]
    assert len(bodies) == 4, "2 texts + 2 images"
    seqs = [body["msg_seq"] for body in bodies]
    assert seqs == [1, 2, 3, 4], f"sequences must be unique and increasing, got {seqs}"
    assert len(set(seqs)) == len(seqs), "a repeated msg_seq is rejected as a duplicate"
    # Every message is a passive reply to the same user message.
    assert all(body["msg_id"] == "ROBOT1.0.msg" for body in bodies)
    assert all(part.ok for part in parts)
    assert used == 4


@pytest.mark.asyncio
async def test_platforms_are_separate_parts_each_with_both_image_kinds(
    sqlite_db, qq_settings, item_factory
):
    """运营方要求：小红书/微博/抖音分别发送，且图片既要有 AI 生成的也要有原帖自带的。"""
    from app.db.database import session_scope
    from app.services.notification.compose import build_push_parts

    settings = qq_settings
    # Two original images on disk, and one generated image.
    root = settings.media_root_path
    (root / "aa").mkdir(parents=True, exist_ok=True)
    (root / "aa" / "orig1.jpg").write_bytes(PNG)
    (root / "aa" / "orig2.jpg").write_bytes(PNG)
    (root / "generated").mkdir(parents=True, exist_ok=True)
    (root / "generated" / "gen1.png").write_bytes(PNG)

    from app.models.ai_rewrite import AiRewriteRecord
    from app.models.image_generation import ImageGenerationRecord
    from app.services.pipeline.hot_pipeline import store_items

    item = item_factory("weibo", "w1", "某个话题")
    async with session_scope(settings) as session:
        await store_items([item], settings=settings, session=session)
        # Attach the two downloaded originals to the stored row.
        from sqlalchemy import select

        from app.models.hot_content import HotContentRecord

        row = (
            await session.execute(select(HotContentRecord).where(HotContentRecord.id == 1))
        ).scalars().first()
        row.media = {
            "images": [
                {"url": "https://cdn/1.jpg", "local_path": "media/aa/orig1.jpg"},
                {"url": "https://cdn/2.jpg", "local_path": "media/aa/orig2.jpg"},
            ]
        }
        row.image_count = 2
        session.add(
            AiRewriteRecord(
                hot_content_id=1,
                xiaohongshu_title="小红书标题",
                xiaohongshu_content="小红书正文",
                xiaohongshu_hashtags=["编程", "AI"],
                weibo_title="微博标题",
                weibo_content="微博正文",
                douyin_hook="抖音钩子",
                douyin_script="抖音脚本",
                status="NEEDS_REVIEW",
            )
        )
        session.add(
            ImageGenerationRecord(
                hot_content_id=1,
                status="success",
                local_paths=["media/generated/gen1.png"],
                model="qwen-image-3.0-pro",
                size="1024*1024",
            )
        )

    parts = await build_push_parts(1, settings=settings)
    labels = [part.label for part in parts]
    assert labels == ["小红书", "微博", "抖音", "原帖"], labels

    for part in parts[:3]:
        assert "media/generated/gen1.png" in part.image_paths, "AI 生成的图要带上"
        assert "media/aa/orig1.jpg" in part.image_paths, "原帖自带的图也要带上"
        # Generated first: it is our own material and what gets published.
        assert part.image_paths[0] == "media/generated/gen1.png"

    original = parts[3]
    assert all("generated" not in path for path in original.image_paths), (
        "原帖那条只该带原图"
    )


@pytest.mark.asyncio
async def test_max_messages_stops_at_the_passive_quota_and_can_resume(qq_settings, tmp_path):
    """被动额度用尽要停下，且下一次调用能接着发还没发完的部分。"""
    from app.services.notification.push import PartReport, send_parts

    media_root = qq_settings.media_root_path
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "a.png").write_bytes(PNG)

    parts = [
        PartReport(label="小红书", text="一", image_paths=["media/a.png"]),
        PartReport(label="微博", text="二", image_paths=["media/a.png"]),
        PartReport(label="抖音", text="三", image_paths=["media/a.png"]),
    ]

    with respx.mock:
        _token_route()
        _prepare_route()
        respx.put("https://cos.example/part0").mock(return_value=httpx.Response(200))
        finish = respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"file_uuid": "u", "file_info": "FI", "ttl": 300})
        )
        messages = respx.post(f"{BASE}/v2/groups/{GROUP}/messages").mock(
            return_value=httpx.Response(200, json={"id": "m"})
        )
        channel = QQBotChannel(qq_settings)
        first = await send_parts(channel, parts, passive_id="M", max_messages=2)
        assert first == 2, "two messages: the text + one image of part 1"
        assert parts[0].ok is True
        assert parts[1].ok is False and parts[2].ok is False

        # A second @ resumes with a fresh quota and only sends what is left.
        second = await send_parts(channel, parts, passive_id="M2", max_messages=5)
        assert second == 4, "part 2 and part 3: 2 texts + 2 images"

    assert all(part.ok for part in parts)
    # One upload per call. Within the second call two parts share the same image and
    # uploaded it once — without the cache it would be three uploads in total, not two.
    assert finish.call_count == 2, (
        f"expected 1 upload per call (2 total), got {finish.call_count} — the file_info "
        "cache is per call, so a later @ re-uploads (correct: the ttl may have expired)"
    )
    seqs = [json.loads(call.request.content).get("msg_seq") for call in messages.calls]
    assert len(seqs) == 6


def test_webp_is_converted_so_original_images_actually_send(tmp_path, qq_settings):
    """素材库 453 张里 370 张是 webp，而 QQ 图片只吃 png/jpg。

    不转换的话「发送原帖图文」会退化成只发文字——正是这个功能要解决的问题。所以
    ``ensure_sendable`` 负责转成 jpg，``check_image`` 才是最后一道格式确认。
    """
    from PIL import Image

    from app.services.notification.qq_media import CONVERTED_SUBDIR, ensure_sendable

    source = tmp_path / "shot.webp"
    # A real webp, produced by Pillow itself, so the round trip is genuine.
    Image.new("RGB", (2400, 1200), (200, 40, 40)).save(source, "WEBP")
    assert source.suffix == ".webp"

    converted = ensure_sendable(source, cache_dir=tmp_path / CONVERTED_SUBDIR)
    assert converted.suffix == ".jpg" and converted.is_file()
    check_image(converted)  # must pass now
    with Image.open(converted) as image:
        assert image.format == "JPEG"
        # Long edge is capped: a 2400px original is downscaled for chat previews.
        assert max(image.size) <= 2000

    # Cached by content hash, so pushing the same picture twice encodes once.
    again = ensure_sendable(source, cache_dir=tmp_path / CONVERTED_SUBDIR)
    assert again == converted
    assert len(list((tmp_path / CONVERTED_SUBDIR).glob("*.jpg"))) == 1


def test_transparency_survives_the_jpeg_conversion(tmp_path, qq_settings):
    """JPEG has no alpha channel, so an RGBA webp must be flattened, not crash."""
    from PIL import Image

    from app.services.notification.qq_media import CONVERTED_SUBDIR, ensure_sendable

    source = tmp_path / "logo.webp"
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(source, "WEBP")

    converted = ensure_sendable(source, cache_dir=tmp_path / CONVERTED_SUBDIR)
    with Image.open(converted) as image:
        assert image.mode == "RGB", "flattened onto white"


def test_a_corrupt_image_is_reported_not_crashed(tmp_path, qq_settings):
    from app.services.notification.qq_media import CONVERTED_SUBDIR, QQMediaError, ensure_sendable

    broken = tmp_path / "broken.webp"
    broken.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 not really an image")
    with pytest.raises(QQMediaError) as excinfo:
        ensure_sendable(broken, cache_dir=tmp_path / CONVERTED_SUBDIR)
    assert "无法转换" in str(excinfo.value)


def test_webp_is_refused_by_check_image_itself(tmp_path, qq_settings):
    """The material library is mostly webp, and QQ images only accept png/jpg.

    Failing locally with a reason beats a 850019 from the API that the operator cannot act
    on — and this is the single most likely failure in practice.
    """
    webp = tmp_path / "shot.webp"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    with pytest.raises(QQMediaError) as excinfo:
        check_image(webp)
    assert "webp" in str(excinfo.value) and "png/jpg" in str(excinfo.value)

    with pytest.raises(QQMediaError):
        check_image(tmp_path / "missing.png")

    oversized = tmp_path / "huge.png"
    oversized.write_bytes(PNG + b"\x00" * (21 * 1024 * 1024))
    with pytest.raises(QQMediaError) as excinfo:
        check_image(oversized)
    assert "20MB" in str(excinfo.value)


# ------------------------------------------------------- the 4-step upload
@pytest.mark.asyncio
async def test_chunked_upload_follows_the_documented_four_steps(qq_settings, tmp_path):
    image = tmp_path / "cover.png"
    image.write_bytes(PNG)

    with respx.mock:
        _prepare_route()
        put = respx.put("https://cos.example/part0").mock(return_value=httpx.Response(200))
        finish = respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        merge = respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(
                200, json={"file_uuid": "uuid_1", "file_info": "FILEINFO", "ttl": 300}
            )
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client,
                group_openid=GROUP,
                path=image,
                headers={"Authorization": "QQBot tok-1"},
            )

    assert result.ok and result.file_info == "FILEINFO" and result.ttl == 300
    assert result.parts_uploaded == 1

    # step 1 body
    prepare_body = json.loads(
        respx.calls[0].request.content
    ) if False else None  # kept explicit: assert via the route below
    assert put.called and finish.called and merge.called

    # step 3 body: the part's actual size and md5
    finish_body = json.loads(finish.calls[0].request.content)
    assert finish_body["upload_id"] == "upload_abc"
    assert finish_body["part_index"] == 0
    assert finish_body["block_size"] == str(len(PNG))
    assert finish_body["md5"] == hashlib.md5(PNG).hexdigest()

    # step 4 body: merge by upload_id, and do NOT let the upload auto-send
    merge_body = json.loads(merge.calls[0].request.content)
    assert merge_body["upload_id"] == "upload_abc"
    assert merge_body["file_type"] == 1
    assert merge_body["srv_send_msg"] is False, (
        "srv_send_msg=true would send an extra message and consume proactive quota"
    )

    # the PUT must not carry the QQ token: the presigned URL is already authorised
    put_headers = put.calls[0].request.headers
    assert "authorization" not in {key.lower() for key in put_headers}


@pytest.mark.asyncio
async def test_upload_prepare_body_carries_all_three_checksums(qq_settings, tmp_path):
    image = tmp_path / "cover.png"
    image.write_bytes(PNG)

    with respx.mock:
        prepare = respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
            return_value=httpx.Response(200, json={"upload_id": "u", "parts": []})
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client, group_openid=GROUP, path=image, headers={}
            )

    # No parts means it must fail, not silently continue.
    assert not result.ok and "parts" in result.error
    body = json.loads(prepare.calls[0].request.content)
    assert set(body) == {"file_type", "file_size", "file_name", "md5", "sha1", "md5_10m"}
    assert body["file_size"] == str(len(PNG))


@pytest.mark.asyncio
async def test_provider_errors_are_explained(qq_settings, tmp_path):
    image = tmp_path / "cover.png"
    image.write_bytes(PNG)

    with respx.mock:
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
            return_value=httpx.Response(200, json={"upload_id": "u", "parts": [{"index": 0, "presigned_url": "https://cos.example/p0", "block_size": "999"}]})
        )
        respx.put("https://cos.example/p0").mock(return_value=httpx.Response(403))
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client, group_openid=GROUP, path=image, headers={}
            )
    assert not result.ok and "403" in result.error

    # A known error code becomes advice, not just a number.
    with respx.mock:
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_prepare").mock(
            return_value=httpx.Response(200, json={"upload_id": "u", "parts": [{"index": 0, "presigned_url": "https://cos.example/p0", "block_size": "999"}]})
        )
        respx.put("https://cos.example/p0").mock(return_value=httpx.Response(200))
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"code": 850018, "message": "bot muted"})
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            result = await upload_group_image(
                client, group_openid=GROUP, path=image, headers={}
            )
    assert not result.ok
    assert "850018" in result.error and "禁言" in result.error


# --------------------------------------------------- sending text + images
@pytest.mark.asyncio
async def test_send_rich_sends_text_then_one_message_per_image(qq_settings, tmp_path):
    """每张图是一条独立消息：富媒体接口一次只能带一个 file_info。"""
    media_root = qq_settings.media_root_path
    media_root.mkdir(parents=True, exist_ok=True)
    first = media_root / "a.png"
    second = media_root / "b.png"
    first.write_bytes(PNG)
    second.write_bytes(PNG)

    with respx.mock:
        _token_route()
        _prepare_route()
        respx.put("https://cos.example/part0").mock(return_value=httpx.Response(200))
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"file_uuid": "u", "file_info": "FI", "ttl": 300})
        )
        messages = respx.post(f"{BASE}/v2/groups/{GROUP}/messages").mock(
            return_value=httpx.Response(200, json={"id": "msg-1"})
        )

        channel = QQBotChannel(qq_settings)
        result = await channel.send_rich(
            "二创", "标题\n\n正文", ["media/a.png", "media/b.png"]
        )

    assert result.ok
    bodies = [json.loads(call.request.content) for call in messages.calls]
    assert len(bodies) == 3, "1 text + 2 images"
    assert bodies[0]["msg_type"] == 0 and "正文" in bodies[0]["content"]
    assert bodies[1]["msg_type"] == 7 and bodies[1]["media"]["file_info"] == "FI"
    assert bodies[2]["msg_type"] == 7
    # msg_seq must increase, otherwise a repeat reply is rejected as a duplicate.
    assert [body["msg_seq"] for body in bodies] == [1, 2, 3]
    assert result.detail["images_sent"] == 2


@pytest.mark.asyncio
async def test_a_bad_image_does_not_fail_the_whole_push(qq_settings, tmp_path):
    """一张 webp 只该被跳过并说明原因，不该让整条推送失败。"""
    media_root = qq_settings.media_root_path
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "ok.png").write_bytes(PNG)
    (media_root / "bad.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")

    with respx.mock:
        _token_route()
        _prepare_route()
        respx.put("https://cos.example/part0").mock(return_value=httpx.Response(200))
        respx.post(f"{BASE}/v2/groups/{GROUP}/upload_part_finish").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/files").mock(
            return_value=httpx.Response(200, json={"file_uuid": "u", "file_info": "FI", "ttl": 300})
        )
        respx.post(f"{BASE}/v2/groups/{GROUP}/messages").mock(
            return_value=httpx.Response(200, json={"id": "m"})
        )
        channel = QQBotChannel(qq_settings)
        result = await channel.send_rich("二创", "text", ["media/ok.png", "media/bad.webp"])

    assert result.ok, "the good image still goes"
    assert result.detail["images_sent"] == 1
    skipped = " ".join(result.detail["skipped"])
    assert "bad.webp" in skipped and "webp" in skipped


@pytest.mark.asyncio
async def test_a_channel_target_is_refused_with_the_reason(qq_settings):
    """文字子频道需要机器人常驻 WebSocket，HTTP 路径发不出去——要说清楚而不是发个死请求。"""
    channel_settings = qq_settings.model_copy(
        update={"qq_group_openid": "", "qq_channel_id": "channel-1"}
    )
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/channels/channel-1/messages")
        channel = QQBotChannel(channel_settings)
        result = await channel.send("标题", "正文")
    assert not result.ok
    assert "WebSocket" in result.error
    assert not route.called, "must not fire a request that cannot work"


@pytest.mark.asyncio
async def test_images_are_refused_for_a_single_user_target(qq_settings):
    """单聊与群聊的上传接口不互通，所以图片路径只支持群。"""
    user_settings = qq_settings.model_copy(
        update={"qq_group_openid": "", "qq_target_openid": "user-1"}
    )
    channel = QQBotChannel(user_settings)
    result = await channel.send_rich("二创", "text", ["media/a.png"])
    assert not result.ok and "只支持 QQ 群" in result.error
