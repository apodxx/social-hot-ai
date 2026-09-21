"""把 HelloGitHub 列表按机器人实际会发的格式发到群里，供人工确认。免费。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402
from app.services.hellogithub.client import fetch_repos  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402


async def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    listing = await fetch_repos(page=1)
    if not listing.ok:
        print(f"取列表失败：{listing.error}")
        return 1

    repos = listing.repos[:count]
    lines = [f"HelloGitHub 最新开源项目 {len(repos)} 个（共 {len(listing.repos)} 个）", ""]
    for index, repo in enumerate(repos, start=1):
        lines.append(f"{index}. {repo.title}")
        lines.append(f"   {repo.full_name}" + ("  🔥热门" if repo.is_hot else ""))
        if repo.summary:
            summary = repo.summary.replace("\n", " ")
            lines.append(f"   {summary[:70]}{'…' if len(summary) > 70 else ''}")
    lines.append("")
    lines.append("想让我把某一个写成小红书文案，发「分析 <仓库全名>」即可。")
    body = "\n".join(lines)

    print(body[:600])
    print()
    channel = QQBotChannel(get_settings())
    result = await channel.send("github最新列表", body)
    print(f"发送：ok={result.ok} status={result.status_code} parts={result.parts}")
    if result.error:
        print(f"错误：{result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
