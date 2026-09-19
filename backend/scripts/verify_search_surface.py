"""Verify the Phase 9 surface without spending anything.

Two things are worth checking on a live server and neither costs money:

* the search endpoint's **validation** runs before any provider call, so a blank
  keyword or an unknown platform is rejected for free (and if that ordering ever
  breaks, a typo would start costing money);
* the stored rows actually expose the new media/origin fields, and ``/media/`` is
  served by its own mount rather than swallowed by the SPA fallback.

A real search is *not* run here: it costs one billed TikHub call per platform. The
script ends by saying so.

    python scripts/verify_search_surface.py [base_url]
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
for argument in sys.argv[1:]:
    if argument.startswith("http"):
        BASE = argument.rstrip("/")

# trust_env=False: the machine's registry proxy turns local calls into 502s with an
# empty body and nothing in the server log.
CLIENT = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(name)


def main() -> int:
    # --- validation must reject before spending -------------------------------
    empty = CLIENT.post("/api/hot/search", json={"keyword": ""})
    check("a blank keyword is rejected (422)", empty.status_code == 422, str(empty.status_code))

    blank = CLIENT.post("/api/hot/search", json={"keyword": "   "})
    check("a whitespace-only keyword is rejected (422)", blank.status_code == 422, str(blank.status_code))

    unknown = CLIENT.post(
        "/api/hot/search", json={"keyword": "露营", "platforms": ["myspace"]}
    )
    check("an unknown platform is rejected (422)", unknown.status_code == 422, str(unknown.status_code))
    if unknown.status_code == 422:
        detail = unknown.json().get("detail", "")
        check("the rejection names the supported platforms", "xiaohongshu" in detail, detail[:80])

    too_long = CLIENT.post("/api/hot/search", json={"keyword": "x" * 200})
    check("an over-long keyword is rejected (422)", too_long.status_code == 422, str(too_long.status_code))

    # A valid search would spend money; make sure the endpoint exists and is POST.
    options = CLIENT.options("/api/hot/search")
    check(
        "the endpoint exists and accepts POST",
        options.status_code in (200, 204, 405),
        f"HTTP {options.status_code}",
    )

    # --- stored rows carry the new fields ------------------------------------
    stored = CLIENT.get("/api/hot/stored", params={"limit": 3})
    check("GET /api/hot/stored is 200", stored.status_code == 200, str(stored.status_code))
    if stored.status_code == 200:
        body = stored.json()
        items = body.get("items") or []
        check("stored list is non-empty", bool(items), f"total={body.get('total')}")
        if items:
            first = items[0]
            for field in ("origin", "image_count", "source_keyword", "media", "content_type"):
                check(f"stored rows expose '{field}'", field in first)
            check(
                "media carries the image list and flags",
                isinstance(first.get("media"), dict)
                and {"images", "video", "has_image", "has_video"} <= set(first["media"]),
            )
            check(
                "existing ranking rows are marked as such",
                first.get("origin") in {"hot", "search"},
                str(first.get("origin")),
            )

    filtered = CLIENT.get("/api/hot/stored", params={"origin": "search", "limit": 1})
    check("the origin filter is accepted", filtered.status_code == 200, str(filtered.status_code))
    print(f"        rows with origin=search: {filtered.json().get('total') if filtered.status_code == 200 else '?'}")

    with_images = CLIENT.get("/api/hot/stored", params={"with_images": "true", "limit": 1})
    check("the with_images filter is accepted", with_images.status_code == 200, str(with_images.status_code))
    print(f"        rows with images: {with_images.json().get('total') if with_images.status_code == 200 else '?'}")

    # --- the media mount ------------------------------------------------------
    missing = CLIENT.get("/media/definitely-missing.jpg")
    check("a missing media file is 404", missing.status_code == 404, str(missing.status_code))
    check(
        "the SPA fallback does not swallow /media/",
        "<!doctype html>" not in missing.text.lower(),
        missing.headers.get("content-type", ""),
    )

    # --- the MCP surface grew by one billed tool ------------------------------
    print()
    print("note: a real search costs one billed TikHub call per platform and is not run here.")
    print("      it is available in the UI ('搜索话题') and as the MCP tool search_topic,")
    print("      both behind an explicit confirmation.")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed (nothing was spent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
