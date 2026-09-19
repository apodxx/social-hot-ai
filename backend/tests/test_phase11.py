"""Phase 11: image generation (图片二创) with Qwen on DashScope.

No test spends money: the provider is mocked with ``respx``, and the result download is
mocked too. What is asserted is the request shape (which is where this quietly breaks —
the reference image must be base64, and the size separator is ``*`` not ``x``), the price
computation per billing tier, and the guards that refuse to call at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.services.ai.image_gen import (
    QwenImageClient,
    QwenImageError,
    build_edit_prompt,
    download_generated,
    image_to_data_uri,
)
from app.services.ai.image_service import pick_references

#: A real 1x1 PNG, so the encoder is exercised on actual bytes.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

ENDPOINT = "/api/v1/services/aigc/multimodal-generation/generation"


def _dashscope_response(*urls: str, output_type: str = "qima_output_1k", inputs: int = 1) -> dict:
    return {
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": [{"image": url, "type": "image"} for url in urls],
                    },
                }
            ]
        },
        "usage": {
            "output_width": 1024,
            "output_height": 1024,
            "input_image_count": inputs,
            "input_image_type": "qima_input_1k",
            "output_image_count": len(urls),
            "output_image_type": output_type,
        },
        "request_id": "req-123",
    }


@pytest.fixture
def image_settings(monkeypatch, tmp_path):
    """Settings with a fake key, a media dir under tmp_path, and no real provider."""
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://workspace.example.com")
    monkeypatch.setenv("IMAGE_GEN_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


# --------------------------------------------------------------- input encoding
def test_reference_image_is_sent_as_base64(image_settings, tmp_path):
    """A URL would fail: our originals are local files and provider URLs have expired."""
    path = tmp_path / "shot.png"
    path.write_bytes(TINY_PNG)
    uri = image_to_data_uri(path)
    assert uri.startswith("data:image/png;base64,")
    assert uri.split(",", 1)[1]


def test_unusable_inputs_are_refused_with_a_reason(image_settings, tmp_path):
    """HEIC is a real image the model will not accept — say that, do not send it."""
    heic = tmp_path / "photo.heic"
    heic.write_bytes(b"\x00\x00\x00\x1cftypheic")
    with pytest.raises(QwenImageError) as excinfo:
        image_to_data_uri(heic)
    assert "heic" in str(excinfo.value).lower()

    big = tmp_path / "big.png"
    big.write_bytes(TINY_PNG + b"\x00" * (11 * 1024 * 1024))
    with pytest.raises(QwenImageError) as excinfo:
        image_to_data_uri(big)
    assert "10MB" in str(excinfo.value)

    with pytest.raises(QwenImageError):
        image_to_data_uri(tmp_path / "missing.png")


def test_edit_prompt_forbids_brands_and_real_faces():
    prompt = build_edit_prompt(goal="给一条数码测评帖做封面", caption="这个价位真香", overlay_text="真香")
    assert "重新构图" in prompt
    assert "给一条数码测评帖做封面" in prompt
    assert "这个价位真香" in prompt
    assert "只使用这几个字：真香" in prompt
    assert "品牌标识" in prompt and "真人肖像" in prompt
    # It must ask for a *new* image rather than a near-copy of the reference.
    assert "避免与参考图相似到像同一张" in prompt


# ------------------------------------------------------------------ the request
@pytest.mark.asyncio
async def test_request_shape_matches_the_dashscope_protocol(image_settings, tmp_path):
    reference = tmp_path / "ref.png"
    reference.write_bytes(TINY_PNG)

    with respx.mock:
        route = respx.post("https://workspace.example.com" + ENDPOINT).mock(
            return_value=httpx.Response(
                200, json=_dashscope_response("https://cdn.example/out.png")
            )
        )
        client = QwenImageClient(image_settings)
        try:
            result = await client.edit_image(prompt="把背景换成图书馆", reference_paths=[reference])
        finally:
            await client.aclose()

    assert result.ok and result.urls == ["https://cdn.example/out.png"]
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == image_settings.qwen_image_model
    content = body["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,"), "the image goes first"
    assert content[-1]["text"] == "把背景换成图书馆"
    # DashScope separates width and height with '*'; the OpenAI-compatible mode uses 'x'
    # and mixing them up is a silent 400.
    assert body["parameters"]["size"] == "1024*1024"
    assert body["parameters"]["n"] == 1
    assert route.calls[0].request.headers["authorization"] == "Bearer test-dashscope-key"


@pytest.mark.asyncio
async def test_price_follows_the_reported_billing_tier(image_settings, tmp_path):
    """Cost comes from the tier the provider reports, not from what we asked for."""
    reference = tmp_path / "ref.png"
    reference.write_bytes(TINY_PNG)

    with respx.mock:
        respx.post("https://workspace.example.com" + ENDPOINT).mock(
            return_value=httpx.Response(
                200, json=_dashscope_response("https://cdn.example/a.png", output_type="qima_output_1k")
            )
        )
        client = QwenImageClient(image_settings)
        one_k = await client.edit_image(prompt="p", reference_paths=[reference])
        await client.aclose()

    with respx.mock:
        respx.post("https://workspace.example.com" + ENDPOINT).mock(
            return_value=httpx.Response(
                200, json=_dashscope_response("https://cdn.example/b.png", output_type="qima_output_2k")
            )
        )
        client = QwenImageClient(image_settings)
        two_k = await client.edit_image(prompt="p", reference_paths=[reference])
        await client.aclose()

    assert one_k.estimated_usd == pytest.approx(0.03438 + 0.00275, rel=1e-3)
    assert two_k.estimated_usd == pytest.approx(0.068761 + 0.00275, rel=1e-3)
    assert two_k.estimated_usd > one_k.estimated_usd * 1.9, "2K is roughly double"


@pytest.mark.asyncio
async def test_provider_failures_are_reported_not_faked(image_settings, tmp_path):
    reference = tmp_path / "ref.png"
    reference.write_bytes(TINY_PNG)

    with respx.mock:
        respx.post("https://workspace.example.com" + ENDPOINT).mock(
            return_value=httpx.Response(
                400, json={"code": "InvalidParameter", "message": "Field 'prompt' is required"}
            )
        )
        client = QwenImageClient(image_settings)
        result = await client.edit_image(prompt="", reference_paths=[reference])
        await client.aclose()
    assert not result.ok
    assert "InvalidParameter" in result.error and "prompt" in result.error
    assert result.urls == []

    # A 200 body carrying an error code must not be treated as success either.
    with respx.mock:
        respx.post("https://workspace.example.com" + ENDPOINT).mock(
            return_value=httpx.Response(200, json={"code": "DataInspectionFailed", "message": "blocked"})
        )
        client = QwenImageClient(image_settings)
        result = await client.edit_image(prompt="p", reference_paths=[reference])
        await client.aclose()
    assert not result.ok and "blocked" in result.error


@pytest.mark.asyncio
async def test_an_unconfigured_key_makes_no_request(monkeypatch, tmp_path):
    """Fail before the network, not after paying."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert not settings.dashscope_configured
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post("https://workspace.example.com" + ENDPOINT)
            client = QwenImageClient(settings)
            result = await client.edit_image(prompt="p")
            await client.aclose()
        assert not result.ok and "未配置" in result.error
        assert not route.called
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------- downloading the result
@pytest.mark.asyncio
async def test_generated_image_is_downloaded_before_the_url_expires(image_settings):
    """The provider's URL lives 24 hours; an unsaved image is a paid image that is gone."""
    with respx.mock:
        respx.get("https://cdn.example/out.png").mock(
            return_value=httpx.Response(200, content=TINY_PNG, headers={"content-type": "image/png"})
        )
        paths, errors = await download_generated(
            ["https://cdn.example/out.png"], settings=image_settings
        )

    assert not errors and len(paths) == 1
    assert paths[0].startswith("media/generated/"), "kept apart from downloaded source material"
    saved = image_settings.media_root_path.parent / paths[0]
    assert saved.read_bytes() == TINY_PNG


@pytest.mark.asyncio
async def test_a_failed_download_is_reported(image_settings):
    with respx.mock:
        respx.get("https://cdn.example/gone.png").mock(return_value=httpx.Response(404))
        paths, errors = await download_generated(
            ["https://cdn.example/gone.png"], settings=image_settings
        )
    assert paths == [] and errors


# ------------------------------------------------------------ reference picking
def test_references_prefer_the_layout_cover_and_need_a_local_file(image_settings, tmp_path):
    """Only downloaded images can be sent — the provider cannot fetch our expired URLs."""
    media_root = image_settings.media_root_path
    first = media_root / "aa" / "one.png"
    second = media_root / "bb" / "two.png"
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(TINY_PNG)
    second.write_bytes(TINY_PNG)

    media = {
        "images": [
            {"url": "https://cdn/1.png", "local_path": "media/aa/one.png"},
            {"url": "https://cdn/2.png", "local_path": "media/bb/two.png"},
            {"url": "https://cdn/3.png", "local_path": None},  # never downloaded
        ]
    }
    picked = pick_references(media, settings=image_settings, limit=2)
    assert [path.name for path in picked] == ["one.png", "two.png"]

    # The layout plan's cover choice leads.
    reordered = pick_references(media, settings=image_settings, limit=1, layout_cover_index=1)
    assert [path.name for path in reordered] == ["two.png"]

    # A row claiming a local file that is gone must not be sent.
    (media_root / "aa" / "one.png").unlink()
    remaining = pick_references(media, settings=image_settings, limit=2)
    assert [path.name for path in remaining] == ["two.png"]


@pytest.mark.asyncio
async def test_an_item_without_material_is_refused_without_calling(
    sqlite_db, image_settings, item_factory, monkeypatch
):
    """No reference image means no call: there is nothing to edit from."""
    from app.db.database import session_scope
    from app.services.ai import image_service
    from app.services.pipeline.hot_pipeline import store_items

    item = item_factory("weibo", "w1", "没有图片的条目")
    async with session_scope(image_settings) as session:
        await store_items([item], settings=image_settings, session=session)

    calls: list[str] = []

    class ExplodingClient:
        async def edit_image(self, **kwargs):  # pragma: no cover - must not run
            calls.append("called")
            raise AssertionError("no provider call may happen without reference material")

        async def aclose(self) -> None:
            return None

    outcome = await image_service.generate_for_content(
        1, settings=image_settings, client=ExplodingClient()
    )
    assert not outcome.ok
    assert "没有可用的本地图片素材" in outcome.error
    assert calls == []
    assert outcome.estimated_usd == 0


# --------------------------------------------------- the 1 image / 1K policy
def test_the_one_image_one_k_policy_cannot_be_raised(monkeypatch, tmp_path):
    """1 image at 1K is a *policy*, not a default any request can override.

    Every extra image is billed again, and above 2,250,000 px the tier doubles — so both
    are refused at configuration time with a message that says what it would cost.
    """
    from app.core.config import Settings

    base = dict(tikhub_api_key="", database_url="", deepseek_api_key="", dashscope_api_key="k")

    assert Settings(**base).image_gen_n == 1
    assert Settings(**base).image_gen_size == "1024*1024"
    assert Settings(**base).image_gen_cost_usd_per_image == pytest.approx(0.03438)

    for count in (2, 6, 0):
        with pytest.raises(Exception) as excinfo:
            Settings(**base, image_gen_n=count)
        assert "每次一张" in str(excinfo.value) or "must be 1" in str(excinfo.value)

    # 2K doubles the price, so the tier is enforced rather than merely documented.
    with pytest.raises(Exception) as excinfo:
        Settings(**base, image_gen_size="2048*2048")
    assert "2K" in str(excinfo.value) and "double" in str(excinfo.value)

    # The OpenAI-style 'x' separator would be a 400 at the provider.
    with pytest.raises(Exception):
        Settings(**base, image_gen_size="1024x1024")


def test_settings_editor_also_enforces_the_policy():
    """The settings page is a path to these values too, so it must reject them as well."""
    from app.services.settings_editor import validate_updates

    clean, errors = validate_updates({"IMAGE_GEN_N": "3"})
    assert not clean and errors and "每次一张" in errors[0]

    clean, errors = validate_updates({"IMAGE_GEN_SIZE": "2048*2048"})
    assert not clean and errors and "2K" in errors[0]

    clean, errors = validate_updates({"IMAGE_GEN_N": "1", "IMAGE_GEN_SIZE": "1024*1024"})
    assert clean == {"IMAGE_GEN_N": "1", "IMAGE_GEN_SIZE": "1024*1024"} and not errors


@pytest.mark.asyncio
async def test_no_interface_can_ask_for_more_than_one_image(monkeypatch, sqlite_db):
    """No request field and no MCP parameter exposes a count to raise.

    Asserted against the real MCP tool schema rather than the source text: a substring
    check for ``n:`` matched ``caption: str = ""`` and passed for the wrong reason.
    """
    from mcp.client import Client

    from app.api.routes.image import GenerateRequest
    from app.core.config import get_settings
    from app.mcp.server import build_mcp_server

    assert "n" not in GenerateRequest.model_fields, "the API must not offer an image count"

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        async with Client(build_mcp_server(get_settings())) as client:
            listed = await client.list_tools()
    finally:
        get_settings.cache_clear()

    tool = next(t for t in listed.tools if t.name == "generate_post_image")
    # mcp 2.x uses snake_case on the model (the JSON field stays ``inputSchema``).
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
    properties = (schema or {}).get("properties", {})
    assert "n" not in properties, f"the MCP tool must not offer a count: {sorted(properties)}"
    assert set(properties) <= {"hot_content_id", "goal", "caption", "overlay_text", "keep_subject"}


@pytest.mark.asyncio
async def test_every_request_sends_exactly_one_image_at_1k(image_settings, tmp_path):
    """Whatever the path, the wire always carries n=1 and the 1K size."""
    reference = tmp_path / "ref.png"
    reference.write_bytes(TINY_PNG)

    with respx.mock:
        route = respx.post("https://workspace.example.com" + ENDPOINT).mock(
            return_value=httpx.Response(200, json=_dashscope_response("https://cdn.example/o.png"))
        )
        client = QwenImageClient(image_settings)
        await client.edit_image(prompt="p", reference_paths=[reference])
        await client.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["parameters"]["n"] == 1
    assert body["parameters"]["size"] == "1024*1024"
    # 1024*1024 is inside the 1K tier, which is what keeps the price at $0.03438.
    width, _, height = body["parameters"]["size"].partition("*")
    assert int(width) * int(height) <= 2_250_000
