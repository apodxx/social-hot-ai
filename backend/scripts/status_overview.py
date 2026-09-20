"""一条命令看清整个系统现在在跑什么、下次推送是什么时候、还要花多少钱。

只读配置与余额，不触发任何管线、不发送任何消息。

    python scripts/status_overview.py
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

BASE = "http://127.0.0.1:8000"
TIKHUB_PER_CALL = 0.0078
USD_TO_CNY = 7.3


def main() -> int:
    settings = get_settings()
    client = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)

    print("=" * 62)
    print("SocialHot AI 运行状态")
    print("=" * 62)

    try:
        schedule = client.get("/api/pipeline/schedule").json()["schedule"]
    except Exception as exc:  # noqa: BLE001
        print(f"后端未响应（{type(exc).__name__}）——是不是没启动？")
        return 1

    now = datetime.datetime.now()
    print(f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("【定时推送】")
    print(f"  调度器    : {'运行中' if schedule['running'] else '已停止'}")
    print(f"  时间点    : {schedule['times']}  ({schedule['timezone']})")
    for job in schedule.get("jobs") or []:
        print(f"    · {job['id']:22} 下次 {job['next_run_time']}")

    print()
    print("【通知通道】")
    try:
        notification = client.get("/api/notification/status").json()["notification"]
        state = "就绪" if notification["ready"] else "被阻塞"
        print(f"  状态      : {state}  通道 {notification['usable']}")
        for reason in notification.get("blocked_by") or []:
            print(f"    ⚠️ {reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"  读取失败：{exc}")

    print()
    print("【领域搜索】")
    print(f"  关键词    : {schedule['watch_keywords']}")
    print(f"  平台      : {schedule['watch_platforms']}")
    print(f"  每轮调用  : {schedule['watch_billed_calls_per_run']} 次计费")

    print()
    print("【每日成本与续航】")
    runs = len([c for c in settings.hot_fetch_times.split(",") if c.strip()]) or 1
    watch = schedule["watch_billed_calls_per_run"]
    per_run_usd = (watch + 3) * TIKHUB_PER_CALL
    per_day_usd = per_run_usd * runs
    per_day_cny = per_day_usd * USD_TO_CNY + 0.14 * runs
    print(f"  每轮 ≈ ¥{per_run_usd * USD_TO_CNY + 0.14:.2f}   每天 {runs} 轮 ≈ ¥{per_day_cny:.2f}")
    try:
        info = client.get(
            settings.tikhub_base_url.rstrip("/") + "/api/v1/tikhub/user/get_user_info",
            headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
        ).json()["user_data"]
        balance = float(info["balance"])
        print(f"  TikHub 余额 ${balance:.4f} → 约可支撑 {balance / per_day_usd:.0f} 天")
    except Exception as exc:  # noqa: BLE001
        print(f"  余额查询失败：{exc}")

    print()
    print("【分步进度】打开后台「知识科普」页可以看到流式进度；")
    print("            命令行预览定时推送内容：")
    print("            python scripts/preview_scheduled_push.py")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
