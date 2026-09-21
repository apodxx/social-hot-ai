"""验证三个平台的链接识别 + 端到端分流。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.ai.agent import is_weibo_link  # noqa: E402
from app.services.tikhub.note_link import is_xiaohongshu_link  # noqa: E402
from app.services.tikhub.video_link import is_douyin_link  # noqa: E402

CASES = [
    ("https://weibo.com/1234/abcdef", "weibo"),
    ("https://m.weibo.cn/detail/123", "weibo"),
    ("https://www.iesdouyin.com/share/note/123/", "douyin"),
    ("https://v.douyin.com/abc/", "douyin"),
    ("https://www.xiaohongshu.com/explore/68c15f20", "xiaohongshu"),
    ("https://xhslink.cn/o/abc", "xiaohongshu"),
    ("https://github.com/psf/requests", "其它"),
    ("https://example.com/post", "其它"),
]


def classify(url: str) -> str:
    if is_douyin_link(url):
        return "douyin"
    if is_xiaohongshu_link(url):
        return "xiaohongshu"
    if is_weibo_link(url):
        return "weibo"
    return "其它"


def main() -> int:
    print("平台识别：")
    bad = 0
    for url, expect in CASES:
        got = classify(url)
        ok = got == expect
        bad += 0 if ok else 1
        print(f"  [{'OK ' if ok else 'FAIL'}] {url[:44]:46} -> {got}")
    print()
    print("全部正确 ✅" if not bad else f"{bad} 个错")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
