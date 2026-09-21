"""按「C语言爱心」这条爆款的关键词去搜同类内容，并把结果推到 QQ 群。

**计费**：每（关键词 × 平台）一次 TikHub 调用（约 $0.0078）。
用 3 个关键词 × 2 个平台 = 6 次 ≈ $0.047 ≈ ¥0.34。

    python scripts/search_similar_and_push.py --dry    # 只搜不发
    python scripts/search_similar_and_push.py          # 搜完推到群里
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

#: 从爆款里拆出来的检索词。设计意图：
#:   * 「爱心代码」——最核心的钩子词；
#:   * 「C语言 爱心」——加技术限定，避开纯手工/手工编织的爱心内容；
#:   * 「Python 爱心」——原帖标签里 python 的内容也在涨（库里已有 2 条），扩一圈同类。
KEYWORDS = ["爱心代码", "C语言爱心", "Python爱心代码"]
PLATFORMS = ["xiaohongshu", "douyin"]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="只搜不发（结果会缓存下来）")
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="用上次搜索的缓存直接推送，**不重新搜索**（省钱）",
    )
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    settings = get_settings()
    cache = Path(settings.media_root_path) / "tmp" / "similar_cache.json"

    items: list[dict] = []
    if args.from_cache:
        # **缓存的意义**：搜索是计费的，而"看看结果再决定发不发"是正常操作。
        # 第一版 --dry 只跳过发送、照旧搜索，于是"先 dry 再发"要花两次钱。
        import json

        if not cache.is_file():
            print(f"没有缓存（{cache}），请先不加 --from-cache 跑一次")
            return 1
        items = json.loads(cache.read_text(encoding="utf-8"))
        print(f"从缓存读取 {len(items)} 条（未重新搜索，未花钱）")
    else:
        calls = len(KEYWORDS) * len(PLATFORMS)
        print(f"将检索 {KEYWORDS}")
        print(
            f"平台 {PLATFORMS} → 共 {calls} 次计费调用"
            f"（约 ${calls * 0.0078:.4f} ≈ ¥{calls * 0.0078 * 7.3:.2f}）"
        )
        print()

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000", timeout=180.0, trust_env=False
        ) as client:
            for keyword in KEYWORDS:
                response = await client.post(
                    "/api/hot/search",
                    json={"keyword": keyword, "platforms": PLATFORMS},
                )
                if response.status_code >= 400:
                    print(f"  「{keyword}」失败：HTTP {response.status_code} {response.text[:120]}")
                    continue
                payload = response.json()
                found = payload.get("items") or payload.get("contents") or []
                print(f"  「{keyword}」→ {len(found)} 条")
                items.extend(found)

        # 去重：按标题前 20 字（跨平台同题内容的标题通常一致）
        seen: set[str] = set()
        deduped: list[dict] = []
        for item in items:
            title = str(item.get("title") or "").strip()
            key = title[:20]
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        items = deduped
        import json

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        print(f"\n共 {len(items)} 条，已缓存到 {cache}")

    print()
    for item in items[: args.limit]:
        print(f"  [{item.get('platform')}] {(item.get('title') or '')[:56]}")

    if args.dry or not items:
        print()
        print("（--dry 或没有结果，未发送）")
        return 0

    lines = [f"「爱心代码」同类内容 {min(len(items), args.limit)} 条", ""]
    for index, item in enumerate(items[: args.limit], start=1):
        title = (item.get("title") or "(无标题)").replace("\n", " ")[:60]
        lines.append(f"{index}. {title}")
        lines.append(f"   {item.get('platform')}")
        url = str(item.get("url") or "")
        if url.startswith("http"):
            lines.append(f"   {url[:100]}")
    body = "\n".join(lines)

    from app.services.notification.qq import QQBotChannel

    channel = QQBotChannel(settings)
    for index in range(0, len(body), 900):
        chunk = body[index : index + 900]
        result = await channel.send("爱心代码·同类内容", chunk)
        print(f"发送 {'ok' if result.ok else '失败'}（{result.status_code}）")
        if not result.ok:
            print(f"  {result.error}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
