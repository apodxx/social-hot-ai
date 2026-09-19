"""检查 .env 里哪些值绑定了本机、哪些字段是空的、哪些是密钥（值遮蔽）。

用于「在另一台电脑上部署」前的体检：换机器时最容易踩的是写死的绝对路径与
localhost 数据库地址。所有密钥值只打印前后几位，不输出完整内容。

    python scripts/audit_env_for_deploy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
#: 只按**后缀**判断，否则 ``DEEPSEEK_MAX_TOKENS``、``WATCH_KEYWORDS`` 会被误报成密钥
#: （第一版就是用"包含 KEY"匹配的，把这两个当成了密钥）。
SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "WEBHOOK_URL")
#: 换一台机器就需要改的东西。
MACHINE_HINTS = ("D:\\", "C:\\", "E:\\", "/Users/", "/home/", "localhost", "127.0.0.1", "pgdata")

COMMENT = "#"


def main() -> int:
    if not ENV_FILE.is_file():
        print(f"找不到 {ENV_FILE}")
        return 1
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    entries = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith(COMMENT) and "=" in line
    ]
    print(f".env：{len(lines)} 行，其中 {len(entries)} 条配置")
    print()

    print("--- 值为空（部署前必须填） ---")
    empty = [e for e in entries if not e.partition("=")[2].strip()]
    for entry in empty:
        print(f"  {entry.partition('=')[0]}")
    if not empty:
        print("  （没有）")
    print()

    print("--- 绑定本机的值（换机器要改） ---")
    machine = [e for e in entries if any(h in e.partition("=")[2] for h in MACHINE_HINTS)]
    for entry in machine:
        print(f"  {entry}")
    if not machine:
        print("  （没有）")
    print()

    print("--- 密钥类字段（值已遮蔽） ---")
    for entry in entries:
        key, _, value = entry.partition("=")
        if key.upper().endswith(SECRET_SUFFIXES) and value.strip():
            masked = f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "***"
            print(f"  {key} = {masked}  (len {len(value)})")
    print()

    print("--- 与部署相关的开关 ---")
    for entry in entries:
        key = entry.partition("=")[0]
        if key in {
            "SCHEDULER_ENABLED",
            "NOTIFICATION_ENABLED",
            "WATCH_SEARCH_ENABLED",
            "IMAGE_GEN_ENABLED",
            "QQ_ENABLED",
            "MEDIA_DOWNLOAD_ENABLED",
            "DETAIL_FETCH_ENABLED",
            "INTEREST_ONLY",
        }:
            print(f"  {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
