"""The local material library (Phase 9).

Why download at all: the provider image URLs are **signed and expire**. A real
Xiaohongshu capture carries ``…?imageView2/…&sign=65e5d78f…&t=6aad2bac``, and a Weibo
picture URL is a bare CDN path that the platform may rotate. A row that stores only the
link is therefore not "saved" in any useful sense — a few hours later the image is gone
and the item can no longer be used as material. So the file itself is fetched and the
row keeps the local path; the original URL is kept alongside for provenance.

Design rules, all of them load-bearing:

* **Bounded.** Per-file size, images per item, and a wall-clock budget per batch. A
  mislabelled URL that redirects to a video, or a CDN that hangs, must not stall a run.
* **Content-addressed.** Files are named by the SHA-256 of their bytes, so the same
  image reached through two items is stored once, and a re-download cannot corrupt an
  existing file (identical content means the same name).
* **Failures are data.** A fetch that fails is recorded on the image (``local_path``
  stays empty) and the run continues; the caller decides whether that matters.
* **Never fatal.** Nothing here raises for a network problem: it is an enrichment step.

Verifying the bytes matters: a CDN that answers 200 with an HTML error page would
otherwise be stored as a ".jpg" and only reveal itself much later, inside a rewrite.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import httpx

from app.core.config import Settings, get_settings
from app.models.hot_content import MediaBundle, MediaImage

logger = logging.getLogger(__name__)

#: What a downloaded image may claim to be. Anything else is a redirect, an error
#: page, or a video that should not be in the material library.
ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif")

#: Magic bytes, checked because Content-Type can lie or be absent.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: ISO base media file format brands. HEIC/AVIF are **real images** that a browser
#: cannot render — douyin's CDN serves HEIC when the URL does not ask for jpeg, and a
#: real run had all 19 douyin pictures rejected as "not a known image format" because
#: this list did not exist. Valid is valid; whether it is *usable* is a separate fact,
#: and the caller is told which one it got.
_ISO_BMFF_BRANDS = {
    b"heic": ".heic",
    b"heix": ".heic",
    b"hevc": ".heic",
    b"hevx": ".heic",
    b"mif1": ".heic",
    b"avif": ".avif",
    b"avis": ".avif",
}

#: Formats a browser will not display; recorded so the operator is not surprised.
BROWSER_UNFRIENDLY_EXTENSIONS = (".heic", ".avif")


def _iso_bmff_extension(data: bytes) -> str | None:
    """``.heic``/``.avif`` when the bytes are an ISO-BMFF image, else ``None``."""
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None
    return _ISO_BMFF_BRANDS.get(data[8:12])


def _looks_like_image(data: bytes) -> bool:
    """True when the leading bytes are a known image signature."""
    if any(data.startswith(signature) for signature, _kind in _MAGIC):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return _iso_bmff_extension(data) is not None


def _extension_for(data: bytes, content_type: str | None) -> str:
    """Pick an extension from the bytes, falling back to the declared type."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    iso = _iso_bmff_extension(data)
    if iso:
        return iso
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/heic": ".heic",
        "image/avif": ".avif",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), ".bin")


@dataclass
class DownloadReport:
    """What one batch did."""

    attempted: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    failed: int = 0
    bytes_written: int = 0
    stopped_early: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "downloaded": self.downloaded,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "bytes_written": self.bytes_written,
            "stopped_early": self.stopped_early or None,
            "errors": self.errors[:10],
        }


class MediaStore:
    """Fetches images into ``<media_root>/<ab>/<sha256><ext>``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def root(self) -> Path:
        return self._settings.media_root_path

    def _target(self, digest: str, extension: str) -> Path:
        # Two-character shard: a flat directory with tens of thousands of files is
        # slow to list on Windows and unpleasant to browse.
        return self.root / digest[:2] / f"{digest}{extension}"

    def _relative(self, path: Path) -> str:
        """Store paths relative to the project root so the DB stays portable."""
        try:
            return path.relative_to(self._settings.media_root_path.parent).as_posix()
        except ValueError:  # pragma: no cover - only if media_root is outside the project
            return path.as_posix()

    async def download_bundle(
        self,
        bundle: MediaBundle,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> DownloadReport:
        """Fetch every image in ``bundle`` that is not already on disk.

        Mutates the bundle's images in place, recording ``local_path``/``bytes``/
        ``sha256`` on success. Returns a report; never raises for a fetch failure.
        """
        report = DownloadReport()
        if not self._settings.media_download_enabled:
            report.stopped_early = "media_download_enabled is false"
            return report

        wanted: list[MediaImage] = [
            image
            for image in bundle.images[: self._settings.media_max_images_per_item]
            if (image.url_large or image.url)
        ]
        if len(bundle.images) > len(wanted):
            logger.info(
                "media: taking %d of %d images (media_max_images_per_item)",
                len(wanted),
                len(bundle.images),
            )
        if not wanted:
            return report

        owns_client = client is None
        active = client or httpx.AsyncClient(
            timeout=self._settings.media_timeout_seconds,
            follow_redirects=True,
            # Never route CDN requests through the machine's proxy settings.
            trust_env=False,
            headers={"User-Agent": "SocialHotAI/0.9 (+local material library)"},
        )
        deadline = time.monotonic() + self._settings.media_batch_seconds
        try:
            for image in wanted:
                if time.monotonic() > deadline:
                    report.stopped_early = (
                        f"batch budget of {self._settings.media_batch_seconds:.0f}s exhausted"
                    )
                    logger.warning("media: %s", report.stopped_early)
                    break
                if image.local_path and (self.root.parent / image.local_path).exists():
                    report.skipped_existing += 1
                    continue
                report.attempted += 1
                await self._fetch_one(image, active, report)
        finally:
            if owns_client:
                await active.aclose()
        return report

    async def _fetch_one(
        self, image: MediaImage, client: httpx.AsyncClient, report: DownloadReport
    ) -> None:
        """Fetch one image, verifying that it really is an image."""
        url = image.url_large or image.url
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            report.failed += 1
            report.errors.append(f"{type(exc).__name__}: {exc}")
            return

        if response.status_code >= 400:
            report.failed += 1
            report.errors.append(f"HTTP {response.status_code} for {url[:80]}")
            return

        declared = response.headers.get("content-type", "")
        if declared and not declared.split(";")[0].strip().lower().startswith("image/"):
            report.failed += 1
            report.errors.append(f"not an image ({declared}) for {url[:80]}")
            return

        data = response.content
        if not data:
            report.failed += 1
            report.errors.append(f"empty body for {url[:80]}")
            return
        limit = self._settings.media_max_bytes_per_file
        if len(data) > limit:
            report.failed += 1
            report.errors.append(f"{len(data)} bytes exceeds the {limit} byte cap for {url[:80]}")
            return
        if not _looks_like_image(data):
            # A CDN answering 200 with an HTML error page is a real failure mode.
            report.failed += 1
            report.errors.append(f"body is not a known image format for {url[:80]}")
            return

        digest = hashlib.sha256(data).hexdigest()
        extension = _extension_for(data, declared)
        target = self._target(digest, extension)
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                # Write then move: a crash mid-write must not leave a truncated file
                # that later looks like a valid cache hit.
                temporary = target.with_suffix(target.suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(target)
        except OSError as exc:
            report.failed += 1
            report.errors.append(f"could not write {target.name}: {exc}")
            return

        image.local_path = self._relative(target)
        image.bytes = len(data)
        image.sha256 = digest
        report.downloaded += 1
        report.bytes_written += len(data)


async def download_for_bundles(
    bundles: Iterable[MediaBundle], settings: Settings | None = None
) -> DownloadReport:
    """Fetch the images of several bundles with one shared HTTP client.

    Batches share a client so a run reuses connections instead of reconnecting per
    image, and so the wall-clock budget covers the whole run rather than each item.
    """
    resolved = settings or get_settings()
    store = MediaStore(resolved)
    report = DownloadReport()
    client = httpx.AsyncClient(
        timeout=resolved.media_timeout_seconds,
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "SocialHotAI/0.9 (+local material library)"},
    )
    deadline = time.monotonic() + resolved.media_batch_seconds
    try:
        for bundle in bundles:
            if time.monotonic() > deadline:
                report.stopped_early = (
                    f"batch budget of {resolved.media_batch_seconds:.0f}s exhausted"
                )
                break
            partial = await store.download_bundle(bundle, client=client)
            report.attempted += partial.attempted
            report.downloaded += partial.downloaded
            report.skipped_existing += partial.skipped_existing
            report.failed += partial.failed
            report.bytes_written += partial.bytes_written
            report.errors.extend(partial.errors)
    finally:
        await client.aclose()
    return report


def storage_summary(settings: Settings | None = None) -> dict[str, object]:
    """Files and bytes currently held, for the settings page and diagnostics."""
    resolved = settings or get_settings()
    root = resolved.media_root_path
    if not root.is_dir():
        return {"root": str(root), "files": 0, "bytes": 0, "exists": False}
    files = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and path.suffix != ".part":
            files += 1
            total += path.stat().st_size
    return {"root": str(root), "files": files, "bytes": total, "exists": True}


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "DownloadReport",
    "MediaStore",
    "download_for_bundles",
    "storage_summary",
]
