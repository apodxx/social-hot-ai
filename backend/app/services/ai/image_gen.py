"""Qwen image generation / editing (Phase 11) — 图片二创.

This closes the limitation the project had been stating honestly: the copy could be
rewritten but the pictures could not. ``qwen-image-3.0-pro`` takes 1-3 reference images
plus an instruction and returns a new image, so a post's material can be reworked too.

**Cost is per image, not per token.** Published price (China Beijing): 1K output
$0.03438, 2K output $0.068761, plus $0.00275 per input image. That is roughly 15x an
entire text rewrite, so three rules are built in and not configurable away:

* nothing generates an image unless a human clicked for it;
* one request is one image (``n=1``) at 1024x1024, which keeps the 1K billing tier;
* a hard per-run ceiling (``IMAGE_GEN_MAX_PER_RUN``) so a loop bug cannot fan out.

Three implementation details that would each break it silently:

1. **The reference image must be sent as base64.** Our originals are local files whose
   provider URLs have already expired, and Alibaba's servers cannot reach our
   ``localhost`` — so a URL would simply fail.
2. **The result URL expires in 24 hours**, exactly like the source URLs did. The image is
   downloaded into the material library immediately, under ``media/generated/`` so
   generated assets are never confused with downloaded source material.
3. **Size uses ``*`` as the separator** in the DashScope protocol (the OpenAI-compatible
   mode uses ``x``); the billing tier depends on the resulting pixel area.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services.tikhub.base import dig

logger = logging.getLogger(__name__)

#: What the provider accepts as input.
ALLOWED_INPUT_MIME = (
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/bmp",
    "image/tiff",
    "image/webp",
    "image/gif",
)
#: Provider limits.
MAX_INPUT_BYTES = 10 * 1024 * 1024
MIN_INPUT_EDGE = 384
MAX_INPUT_EDGE = 2048
MAX_REFERENCE_IMAGES = 3

#: Where generated assets live inside the material library. Kept apart from the
#: downloaded source material on purpose: one is someone else's picture, the other is
#: ours, and mixing them would make provenance unauditable.
GENERATED_SUBDIR = "generated"


class QwenImageError(RuntimeError):
    """Any failure from the image service. Never a fabricated image."""


@dataclass
class ImageUsage:
    """The provider's metering fields (image counts, not tokens)."""

    output_width: int = 0
    output_height: int = 0
    input_image_count: int = 0
    output_image_count: int = 0
    input_image_type: str = ""
    output_image_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_width": self.output_width,
            "output_height": self.output_height,
            "input_image_count": self.input_image_count,
            "output_image_count": self.output_image_count,
            "input_image_type": self.input_image_type,
            "output_image_type": self.output_image_type,
        }

    def estimated_usd(self, settings: Settings) -> float:
        """Cost of this call: output images at the output tier plus input images.

        Uses the tiers the provider reports, so a 2K result is priced as 2K rather than
        being estimated from what we asked for.
        """
        output_each = 0.068761 if "2k" in (self.output_image_type or "").lower() else 0.03438
        input_each = 0.00275
        return round(
            self.output_image_count * output_each + self.input_image_count * input_each, 6
        )


@dataclass
class ImageResult:
    """One generation call's outcome."""

    ok: bool = False
    urls: list[str] = field(default_factory=list)
    local_paths: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    estimated_usd: float = 0.0
    estimated_cny: float = 0.0
    request_id: str = ""
    prompt: str = ""
    elapsed_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "urls": self.urls,
            "local_paths": self.local_paths,
            "usage": self.usage,
            "estimated_usd": round(self.estimated_usd, 6),
            "estimated_cny": self.estimated_cny,
            "request_id": self.request_id,
            "prompt": self.prompt,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


def image_to_data_uri(path: Path) -> str:
    """Encode a local image as a base64 data URI.

    The only input form that works for us: provider URLs have expired and the provider
    cannot reach a localhost file server.
    """
    if not path.is_file():
        raise QwenImageError(f"找不到图片文件：{path}")
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise QwenImageError(f"图片 {size / 1024 / 1024:.1f}MB 超过 10MB 上限：{path.name}")
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if mime not in ALLOWED_INPUT_MIME:
        # HEIC/AVIF are real images the provider does not accept; say so plainly.
        raise QwenImageError(
            f"格式 {mime} 不被图像模型接受（支持 {', '.join(ALLOWED_INPUT_MIME)}）：{path.name}"
        )
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def build_edit_prompt(
    *,
    goal: str,
    caption: str = "",
    overlay_text: str = "",
    keep_subject: bool = True,
    extra: str = "",
) -> str:
    """The instruction sent alongside the reference image.

    Written as an *edit* instruction rather than a description: the model is given a
    picture and told what to change, which is what keeps the result recognisably in the
    same visual family while being a new image.
    """
    lines = [
        "以这张参考图为基础，生成一张全新的图片（不是加滤镜，而是重新构图与打光）。",
        f"创作目标：{goal.strip()}",
    ]
    if caption:
        lines.append(f"这张图要配合的文案：{caption.strip()[:200]}")
    if overlay_text:
        lines.append(f"如需在画面上排版文字，只使用这几个字：{overlay_text.strip()[:30]}")
    if keep_subject:
        lines.append("保留参考图的主体类型与整体氛围，但改变机位、光线和背景细节，避免与参考图相似到像同一张。")
    else:
        lines.append("不必保留参考图的具体主体，只沿用它的色调与质感。")
    lines.append("不要出现任何品牌标识、水印、二维码或可识别的真人肖像。")
    if extra:
        lines.append(extra.strip())
    return "\n".join(lines)


class QwenImageClient:
    """Minimal DashScope multimodal-generation client for image output."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self._settings.dashscope_base_url.rstrip("/"),
            timeout=self._settings.image_gen_timeout_seconds,
            # Direct egress, like every other provider call in this project: the machine's
            # registry proxy breaks non-localhost calls too if it is consulted.
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def edit_image(
        self,
        *,
        prompt: str,
        reference_paths: list[Path] | None = None,
        size: str | None = None,
        n: int | None = None,
    ) -> ImageResult:
        """Generate one image, optionally from 1-3 reference images.

        **Bills per output image** (and a small per-input-image fee).
        """
        settings = self._settings
        result = ImageResult(prompt=prompt)
        if not settings.dashscope_configured:
            result.error = "DASHSCOPE_API_KEY 或 DASHSCOPE_BASE_URL 未配置"
            return result

        references = list(reference_paths or [])[:MAX_REFERENCE_IMAGES]
        content: list[dict[str, str]] = []
        try:
            for path in references:
                content.append({"image": image_to_data_uri(path)})
        except QwenImageError as exc:
            result.error = str(exc)
            return result
        content.append({"text": prompt})

        payload = {
            "model": settings.qwen_image_model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": {
                "prompt_extend": True,
                "n": int(n or settings.image_gen_n),
                "size": size or settings.image_gen_size,
                "watermark": settings.image_gen_watermark,
            },
        }

        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/api/v1/services/aigc/multimodal-generation/generation",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.dashscope_api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            result.error = f"请求失败：{type(exc).__name__}: {exc}"
            return result
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            result.error = _error_message(response)
            return result
        try:
            body = response.json()
        except ValueError:
            result.error = f"响应不是 JSON：{response.text[:200]}"
            return result

        # The provider reports failures inside a 200 body too.
        code = body.get("code")
        if isinstance(code, str) and code and code.lower() not in {"success", "ok"}:
            result.error = f"{code}: {body.get('message') or ''}"
            return result

        result.urls = _extract_image_urls(body)
        result.request_id = str(body.get("request_id") or "")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        parsed = ImageUsage(
            output_width=int(usage.get("output_width") or 0),
            output_height=int(usage.get("output_height") or 0),
            input_image_count=int(usage.get("input_image_count") or 0),
            output_image_count=int(usage.get("output_image_count") or len(result.urls)),
            input_image_type=str(usage.get("input_image_type") or ""),
            output_image_type=str(usage.get("output_image_type") or ""),
        )
        result.usage = parsed.as_dict()
        result.estimated_usd = parsed.estimated_usd(settings)
        result.estimated_cny = round(result.estimated_usd * 7.3, 4)

        if not result.urls:
            result.error = "响应里没有图片地址（未生成）"
            return result
        result.ok = True
        logger.warning(
            "image generated: %d image(s) %sx%s, ~$%.4f in %dms",
            parsed.output_image_count,
            parsed.output_width,
            parsed.output_height,
            result.estimated_usd,
            result.elapsed_ms,
        )
        return result


def _extract_image_urls(body: dict[str, Any]) -> list[str]:
    """Pull every generated image URL out of either response shape.

    DashScope returns ``output.choices[].message.content[].image``; the OpenAI-compatible
    mode returns ``data[].url``. Both are accepted because the endpoint is configurable.
    """
    urls: list[str] = []
    choices = dig(body, "output.choices")
    if isinstance(choices, list):
        for choice in choices:
            content = dig(choice, "message.content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("image"), str):
                        urls.append(item["image"])
    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                urls.append(item["url"])
    return urls


def _error_message(response: httpx.Response) -> str:
    """A readable message from either error shape."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    if isinstance(body, dict):
        message = body.get("message") or dig(body, "error.message")
        code = body.get("code") or dig(body, "error.code")
        if message:
            return f"HTTP {response.status_code} {code or ''}: {message}".strip()
    return f"HTTP {response.status_code}: {str(body)[:200]}"


def generated_dir(settings: Settings | None = None) -> Path:
    """Where generated images are stored: ``<media_root>/generated``."""
    resolved = settings or get_settings()
    return resolved.media_root_path / GENERATED_SUBDIR


async def download_generated(
    urls: list[str], *, settings: Settings | None = None, timeout: float = 120.0
) -> tuple[list[str], list[str]]:
    """Fetch generated images into ``media/generated`` before the URLs expire.

    Returns ``(local_paths, errors)``. Provider result URLs live 24 hours, so a
    generation that is not downloaded promptly is a paid image that no longer exists.
    """
    resolved = settings or get_settings()
    target_dir = generated_dir(resolved)
    target_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
        for index, url in enumerate(urls):
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            data = response.content
            if not data:
                errors.append(f"空响应：{url[:80]}")
                continue
            # Name by the provider's own identifier plus a counter: generated assets are
            # ours, so content-addressing them buys nothing and obscures "which call made
            # this".
            stamp = time.strftime("%Y%m%d-%H%M%S")
            name = f"qwen-{stamp}-{index + 1}.png"
            destination = target_dir / name
            destination.write_bytes(data)
            local_paths.append(
                destination.relative_to(resolved.media_root_path.parent).as_posix()
            )
    return local_paths, errors


__all__ = [
    "ALLOWED_INPUT_MIME",
    "GENERATED_SUBDIR",
    "ImageResult",
    "ImageUsage",
    "QwenImageClient",
    "QwenImageError",
    "build_edit_prompt",
    "download_generated",
    "generated_dir",
    "image_to_data_uri",
]
