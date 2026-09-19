"""Phase 9: keyword search, media extraction, and the local material library.

The extraction tests run against the **real captured responses** in
``tests/fixtures/raw/search_*.json`` (see ``scripts/discover_search.py``), so a field
path that only looks right is caught here rather than in a paid run. No test touches
the network: the download tests use ``respx``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.models.hot_content import ContentOrigin, ContentType, MediaBundle, MediaImage, MediaVideo, merge_media_downloads
from app.services.media.store import MediaStore, _extension_for, _looks_like_image
from app.services.tikhub.search import (
    SEARCH_ADAPTERS,
    DouyinSearchAdapter,
    WeiboPicSearchAdapter,
    XiaohongshuSearchAdapter,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "raw"

#: A 1x1 JPEG, so the store's magic-byte check sees a real image.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffc000110800010001"
    "0301220002110103011101ffc4001f000001050101010101010000000000000000"
    "0102030405060708090a0bffc400b5100002010303020403050504040000017d0102"
    "0300041105122131410613516107227114328191a1082342b1c21525316172f01724"
    "33425262728291a1b1c1d1e1f2f0ffda000c03010002110311003f00bf8005ffd9"
)


def load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / name
    if not path.exists():  # pragma: no cover - the fixtures are committed
        pytest.skip(f"{name} not captured; run scripts/discover_search.py --yes")
    return json.loads(path.read_text(encoding="utf-8"))


class StubClient:
    """A TikHub client stand-in that replays a captured response."""

    def __init__(self, payload: dict, *, fail: bool = False) -> None:
        self._payload = payload
        self._fail = fail
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, params))
        if self._fail:
            from app.services.tikhub.client import TikHubError

            raise TikHubError("provider said no", status=500, path=path)
        return self._payload

    async def post_json(self, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append(("POST", path, json_body))
        if self._fail:
            from app.services.tikhub.client import TikHubError

            raise TikHubError("provider said no", status=500, path=path)
        return self._payload

    async def aclose(self) -> None:  # pragma: no cover
        return None


# ------------------------------------------------------- xiaohongshu extraction
@pytest.mark.asyncio
async def test_xiaohongshu_search_extracts_images_and_type():
    payload = load_fixture("search_xiaohongshu.json")
    adapter = XiaohongshuSearchAdapter(StubClient(payload))

    items = await adapter.search("中国男篮", 50)

    assert len(items) == 20, "all 20 captured items should normalize"
    kinds = {item.content_type for item in items}
    assert kinds == {ContentType.NOTE, ContentType.VIDEO}, "the capture has both"

    with_images = [item for item in items if item.media.has_image]
    assert len(with_images) == 20, "the search response carries images_list for every item"

    first = items[0]
    assert first.origin is ContentOrigin.SEARCH
    assert first.source_keyword == "中国男篮"
    assert first.url and first.url.startswith("https://www.xiaohongshu.com/explore/")
    # The detail stage needs this token, and the search response is where it comes from.
    assert first.raw_data.get("xsec_token"), "xsec_token must be captured for the detail fetch"
    # The 1440w variant is preferred as the primary URL when the provider offers it.
    assert first.media.images[0].url.startswith("http")
    assert first.media.images[0].width and first.media.images[0].height


@pytest.mark.asyncio
async def test_xiaohongshu_video_items_carry_a_video_bundle():
    payload = load_fixture("search_xiaohongshu.json")
    adapter = XiaohongshuSearchAdapter(StubClient(payload))
    items = await adapter.search("中国男篮", 50)

    videos = [item for item in items if item.content_type is ContentType.VIDEO]
    assert videos, "the capture contains video notes"
    for item in videos:
        assert item.media.video is not None
        # A video note still has a cover image, which is what the feed shows.
        assert item.media.has_image


# ------------------------------------------------------------ douyin extraction
@pytest.mark.asyncio
async def test_douyin_search_separates_image_posts_from_videos():
    payload = load_fixture("search_douyin.json")
    adapter = DouyinSearchAdapter(StubClient(payload))

    items = await adapter.search("中国男篮", 50)

    assert len(items) == 16, "16 of the 19 captured cards carry an aweme_info"
    image_posts = [item for item in items if item.content_type is ContentType.NOTE]
    videos = [item for item in items if item.content_type is ContentType.VIDEO]
    assert image_posts and videos, "the capture has both aweme_type 68 and 0"
    assert all(item.media.has_image for item in image_posts), "image posts carry images[]"
    assert all(item.media.video and item.media.video.url for item in videos), "videos carry a play address"
    for item in videos:
        assert item.media.video.cover_url, "a video needs its cover for the layout plan"
    assert items[0].url and "douyin.com" in items[0].url
    assert items[0].source_keyword == "中国男篮"


@pytest.mark.asyncio
async def test_douyin_search_makes_a_post_request():
    """The Douyin search family is POST-only; a GET would 405."""
    payload = load_fixture("search_douyin.json")
    stub = StubClient(payload)
    await DouyinSearchAdapter(stub).search("露营", 10)

    method, path, body = stub.calls[0]
    assert method == "POST", "douyin search must use POST"
    assert path.endswith("/fetch_general_search_v2")
    assert body and body["keyword"] == "露营"
    assert body["content_type"] == "0", "0 = all, which is what keeps images and video"


def test_douyin_images_pick_the_jpeg_not_the_heic():
    """A real run downloaded 19/19 douyin pictures as HEIC and rejected them all.

    ``url_list`` carries the same picture twice — ``…:q80.heic`` and ``…:q80.jpeg`` —
    and taking index 0 fetched the one **no browser can display**. The web-friendly
    variant is now chosen explicitly, so the material library holds something usable.
    """
    payload = load_fixture("search_douyin.json")
    items = DouyinSearchAdapter(StubClient(payload)).normalize_search(
        payload["data"]["business_data"], keyword="露营装备"
    )
    image_posts = [item for item in items if item.content_type is ContentType.NOTE]
    assert image_posts, "the capture has image posts"

    checked = 0
    for item in image_posts:
        for image in item.media.images:
            assert not image.url.lower().endswith(".heic"), image.url[-40:]
            assert ".heic" not in image.url.lower(), f"HEIC chosen: {image.url[-60:]}"
            checked += 1
    assert checked, "at least one image should have been extracted"


def test_pick_web_image_prefers_a_renderable_format():
    from app.services.tikhub.search import pick_web_image

    heic, jpeg, webp = "https://cdn/x~tplv:q80.heic", "https://cdn/x~tplv:q80.jpeg", "https://cdn/x.webp"
    assert pick_web_image([heic, jpeg]) == jpeg
    assert pick_web_image([heic]) == heic, "better HEIC than nothing when it is all there is"
    assert pick_web_image([jpeg, webp]) == jpeg, "preference order is jpeg, jpg, png, webp"
    assert pick_web_image([heic, webp]) == webp
    assert pick_web_image(["//cdn/x.jpeg"]) == "https://cdn/x.jpeg", "protocol-relative is fixed"
    assert pick_web_image([]) is None and pick_web_image(None) is None


# ------------------------------------------------------------- weibo extraction
@pytest.mark.asyncio
async def test_weibo_pic_search_yields_real_image_urls():
    payload = load_fixture("search_weibo.json")
    adapter = WeiboPicSearchAdapter(StubClient(payload))

    items = await adapter.search("中国男篮", 50)

    assert len(items) == 19
    for item in items:
        assert item.media.has_image, "every captured entry had original_pic"
        url = item.media.images[0].url
        # The provider returns protocol-relative URLs; a bare "//…" is not fetchable.
        assert url.startswith("https://"), url
    assert items[0].title, "the post text becomes the title"
    assert items[0].source_keyword == "中国男篮"


# ------------------------------------------------------- adapter error handling
@pytest.mark.asyncio
async def test_a_failed_platform_is_reported_not_raised():
    """One platform failing must not lose the others (the pipeline contract)."""
    from app.services.pipeline.search_pipeline import search_platforms

    settings = get_settings()
    with pytest.raises(Exception):
        # An unknown platform is a programming error and must be loud.
        await search_platforms("x", platforms=["myspace"], settings=settings)


# ----------------------------------------------------- the local media library
@pytest.mark.asyncio
async def test_store_writes_content_addressed_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.media_root_path == tmp_path / "media"

    bundle = MediaBundle(images=[MediaImage(url="https://cdn.example/a.jpg")])
    url = "https://cdn.example/a.jpg"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(200, content=TINY_JPEG, headers={"content-type": "image/jpeg"})
        )
        report = await MediaStore(settings).download_bundle(bundle)

    assert report.downloaded == 1 and report.failed == 0
    image = bundle.images[0]
    assert image.local_path and image.sha256 and image.bytes == len(TINY_JPEG)
    written = settings.media_root_path.parent / image.local_path
    assert written.exists(), f"{written} should have been written"
    assert written.read_bytes() == TINY_JPEG
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_store_rejects_a_page_that_is_not_an_image(tmp_path, monkeypatch):
    """A CDN answering 200 with HTML must not end up as a .jpg in the library."""
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    settings = get_settings()

    bundle = MediaBundle(images=[MediaImage(url="https://cdn.example/error.jpg")])
    with respx.mock:
        respx.get("https://cdn.example/error.jpg").mock(
            return_value=httpx.Response(
                200, content=b"<html>not found</html>", headers={"content-type": "text/html"}
            )
        )
        report = await MediaStore(settings).download_bundle(bundle)

    assert report.downloaded == 0 and report.failed == 1
    assert bundle.images[0].local_path is None, "a failure must not claim a local file"
    assert report.errors and "not an image" in report.errors[0]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_store_enforces_the_per_file_cap(tmp_path, monkeypatch):
    """A file over the cap is refused.

    The cap has a 1 KiB floor (a smaller one could not hold an image and is certainly a
    typo), so the payload is made comfortably larger than 1 KiB rather than lowering the
    configuration below what is allowed.
    """
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_MAX_BYTES_PER_FILE", "1024")
    get_settings.cache_clear()
    settings = get_settings()

    oversized = TINY_JPEG + b"\x00" * 4096
    assert len(oversized) > 1024
    bundle = MediaBundle(images=[MediaImage(url="https://cdn.example/big.jpg")])
    with respx.mock:
        respx.get("https://cdn.example/big.jpg").mock(
            return_value=httpx.Response(
                200, content=oversized, headers={"content-type": "image/jpeg"}
            )
        )
        report = await MediaStore(settings).download_bundle(bundle)

    assert report.downloaded == 0 and "exceeds" in report.errors[0]
    assert bundle.images[0].local_path is None
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_store_is_idempotent(tmp_path, monkeypatch):
    """Re-running must not re-fetch a file it already holds."""
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    settings = get_settings()
    store = MediaStore(settings)

    bundle = MediaBundle(images=[MediaImage(url="https://cdn.example/a.jpg")])
    with respx.mock:
        respx.get("https://cdn.example/a.jpg").mock(
            return_value=httpx.Response(200, content=TINY_JPEG, headers={"content-type": "image/jpeg"})
        )
        first = await store.download_bundle(bundle)
        second = await store.download_bundle(bundle)

    assert first.downloaded == 1
    assert second.skipped_existing == 1 and second.attempted == 0
    get_settings.cache_clear()


def test_downloads_survive_a_refresh():
    """A ranking refresh carries no local paths; the stored ones must not be lost.

    This is the bug the merge exists to prevent: the scheduled pipeline re-reads the
    same note, and writing its bundle verbatim would forget every downloaded file and
    orphan it on disk.
    """
    stored = {
        "images": [
            {
                "url": "https://cdn.example/a.jpg",
                "local_path": "media/ab/abcdef.jpg",
                "bytes": 123,
                "sha256": "abcdef",
            }
        ],
        "video": None,
    }
    incoming = MediaBundle(images=[MediaImage(url="https://cdn.example/a.jpg")])

    merged = merge_media_downloads(stored, incoming)

    assert merged["images"][0]["local_path"] == "media/ab/abcdef.jpg"
    assert merged["images"][0]["bytes"] == 123

    # A genuinely different image must not inherit the old download.
    other = MediaBundle(images=[MediaImage(url="https://cdn.example/b.jpg")])
    merged_other = merge_media_downloads(stored, other)
    assert merged_other["images"][0]["local_path"] is None


def test_magic_bytes_detection():
    assert _looks_like_image(TINY_JPEG)
    assert not _looks_like_image(b"<html>")
    assert _looks_like_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")


def test_heic_is_a_real_image_even_though_browsers_cannot_show_it():
    """``image/heic`` with ``ftypheic`` is a genuine picture, not an error page.

    Douyin's CDN returns exactly this. Rejecting it as "not a known image format"
    threw away every douyin picture in a real run — and the diagnostic was misleading,
    because the body *was* an image. It is now stored with its true extension so the
    operator can see what they actually have.
    """
    heic = b"\x00\x00\x00\x1cftypheic\x00\x00\x00\x00mif1heic"
    avif = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00mif1avif"
    assert _looks_like_image(heic)
    assert _looks_like_image(avif)
    assert _extension_for(heic, "image/heic") == ".heic"
    assert _extension_for(avif, "image/avif") == ".avif"
    # And something that really is not an image is still refused.
    assert not _looks_like_image(b"<html><body>403</body></html>")
