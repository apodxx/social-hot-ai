"""验证推送里的链接清理：坏链接要退回搜索页，跟踪参数要去掉，必要参数要保留。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.notification.manager import item_link  # noqa: E402


class FakeItem:
    def __init__(self, url: str, platform: str = "weibo", title: str = "某条热搜"):
        self.url = url
        self.platform = platform
        self.title = title


CASES = [
    ("javascript:void(0);", "weibo", "坏链接 → 该退回搜索页"),
    ("", "douyin", "空链接 → 该退回搜索页"),
    ("", "weibo", "空链接 → 该退回搜索页"),
    (
        "https://s.weibo.com/weibo?q=%23abc%23&t=31&band_rank=9&Refer=top",
        "weibo",
        "微博 → 只留 q",
    ),
    (
        "https://www.xiaohongshu.com/explore/x?xsec_token=KEEP&foo=drop",
        "xiaohongshu",
        "小红书 → 留 xsec_token",
    ),
    (
        "https://www.iesdouyin.com/share/video/123/?mid=x&u_code=0",
        "douyin",
        "抖音 → 只留路径",
    ),
]


def main() -> int:
    for url, platform, note in CASES:
        out = item_link(FakeItem(url, platform))
        shown = url if url else "(空)"
        print(f"{note}")
        print(f"   输入: {shown[:60]}")
        print(f"   输出: {out[:80]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
