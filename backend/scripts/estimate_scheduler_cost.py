"""算一下"打开早中晚自动推送"每天要花多少钱，以及现有余额能撑多久。

只读余额与配置，不发送任何消息、不触发任何管线。

    python scripts/estimate_scheduler_cost.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

#: TikHub 每次搜索调用的实测单价（本项目实测，不是估算）。
TIKHUB_PER_CALL = 0.0078
#: 一次完整管线实测的 DeepSeek 花费（22,115 tokens ≈ ¥0.14）。
DEEPSEEK_PER_RUN_CNY = 0.14
USD_TO_CNY = 7.3


def main() -> int:
    settings = get_settings()
    client = httpx.Client(timeout=30.0, trust_env=False)
    info = client.get(
        settings.tikhub_base_url.rstrip("/") + "/api/v1/tikhub/user/get_user_info",
        headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
    ).json()["user_data"]
    balance = float(info["balance"])

    watch_calls = settings.watch_billed_calls_per_run
    hot_calls = 3  # 每次抓三个平台的榜单
    # HOT_FETCH_TIMES 是**逗号分隔的字符串**（config 里类型就是 str），
    # 直接 len() 会得到字符数（"08:00,12:00,18:00" 是 17）。
    # 第一版就是这么写错的，把每天 3 轮算成了 17 轮、成本虚高 5.7 倍。
    time_points = [chunk.strip() for chunk in settings.hot_fetch_times.split(",") if chunk.strip()]
    runs = len(time_points) or 1

    per_run_usd = (watch_calls + hot_calls) * TIKHUB_PER_CALL
    per_run_cny = per_run_usd * USD_TO_CNY + DEEPSEEK_PER_RUN_CNY
    per_day_usd = per_run_usd * runs
    per_day_cny = per_run_cny * runs

    print(f"TikHub 余额：${balance:.4f}（另有免费额度 ${float(info.get('free_credit') or 0):.4f}）")
    print(f"每日运行次数：{runs} 次 → {time_points}")
    print()
    print("每一轮（实测校准）：")
    print(f"  领域搜索 {watch_calls:>2} 次调用            = ${watch_calls * TIKHUB_PER_CALL:.4f}")
    print(f"  热榜抓取 {hot_calls} 个平台             = ${hot_calls * TIKHUB_PER_CALL:.4f}")
    print(f"  DeepSeek 分析 + 二创            ≈ ¥{DEEPSEEK_PER_RUN_CNY:.2f}")
    print(f"  合计                            ≈ ¥{per_run_cny:.2f} / 轮")
    print()
    print("每天：")
    print(f"  TikHub                          = ${per_day_usd:.3f} ≈ ¥{per_day_usd * USD_TO_CNY:.2f}")
    print(f"  DeepSeek                        ≈ ¥{DEEPSEEK_PER_RUN_CNY * runs:.2f}")
    print(f"  合计                            ≈ ¥{per_day_cny:.2f} / 天")
    print()
    if per_day_usd > 0:
        print(f"TikHub 余额可支撑约 {balance / per_day_usd:.0f} 天（之后领域搜索开始失败但管线继续跑）")
    print()
    print("注意：这些钱花在**采集与分析**上，与推送能否成功无关——")
    print("      主动消息无权限时，每轮推送都会以 40034105 失败，但上面的钱照花。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
