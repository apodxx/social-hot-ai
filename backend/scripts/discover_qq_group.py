"""Discover the QQ bot's group openid(s) — and prove the credentials work.

The operator could not find ``group_openid`` in the console, which is expected: it is not
shown there. It arrives in a ``GROUP_AT_MESSAGE_CREATE`` event, or can be listed through
the group-management endpoint. This script tries, in order:

1. exchange AppID/AppSecret for an app access token (proves the credentials are right);
2. ``GET /v2/users/@me/groups`` for the groups the bot has been added to;
3. if that is unavailable, explain exactly how to obtain the id from an event instead.

Everything here is free: no messages are sent.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
PRODUCTION = "https://api.sgroup.qq.com"
SANDBOX = "https://sandbox.api.sgroup.qq.com"

#: The group list is paginated with a limit of 100 per page.
PAGE_LIMIT = 100
#: Try both bases: a sandbox-only app rejects the production host and vice versa.
BASES = (("production", PRODUCTION), ("sandbox", SANDBOX))


def main(app_id: str, app_secret: str) -> int:
    client = httpx.Client(timeout=30.0, trust_env=False)
    print("=== 1. 换取 app access token ===")
    try:
        response = client.post(
            TOKEN_URL, json={"appId": app_id, "clientSecret": app_secret}
        )
    except httpx.HTTPError as exc:
        print(f"  请求失败：{type(exc).__name__}: {exc}")
        return 1
    print(f"  HTTP {response.status_code}")
    try:
        body: Any = response.json()
    except ValueError:
        print(f"  非 JSON 响应：{response.text[:200]}")
        return 1
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        print(f"  没有拿到 token：{json.dumps(body, ensure_ascii=False)[:300]}")
        return 1
    print(f"  拿到 token（{token[:6]}…，{len(token)} 字符）")
    print(f"  expires_in={body.get('expires_in')}")

    headers = {"Authorization": f"QQBot {token}"}
    print()
    print("=== 2. 列出机器人加入的群 ===")
    for name, base in BASES:
        try:
            listed = client.get(f"{base}/v2/users/@me/groups", headers=headers)
        except httpx.HTTPError as exc:
            print(f"  [{name}] 请求失败：{type(exc).__name__}: {exc}")
            continue
        print(f"  [{name}] HTTP {listed.status_code}")
        if listed.status_code >= 400:
            print(f"    {listed.text[:240]}")
            continue
        try:
            payload = listed.json()
        except ValueError:
            print(f"    非 JSON：{listed.text[:200]}")
            continue
        groups = payload.get("group_openids") or payload.get("data") or []
        if not groups:
            print(f"    响应里没有群：{json.dumps(payload, ensure_ascii=False)[:300]}")
            continue
        print(f"  ✅ 找到 {len(groups)} 个群：")
        for index, group in enumerate(groups, start=1):
            identifier = group if isinstance(group, str) else json.dumps(group, ensure_ascii=False)
            print(f"    {index}. {identifier}")
        print()
        print(f"  把上面任意一个填进 .env 的 QQ_GROUP_OPENID（环境：{name}）")
        return 0

    print()
    print("=== 3. 没能直接列出群 ===")
    print("  这是常见情况：``/v2/users/@me/groups`` 需要机器人已开通群聊权限，")
    print("  而且只能列出机器人**已经加入**的群。请按下面任一种方式取 group_openid：")
    print()
    print("  方式 A（推荐，无需写代码）：")
    print("    1. 把机器人拉进你的 QQ 群（群设置 → 群机器人 → 添加机器人）")
    print("    2. 在群里 @机器人 发任意一句话")
    print("    3. 到 q.qq.com 开发者后台 →「开发设置」→「事件订阅」或「调试」里")
    print("       查看刚收到的事件 GROUP_AT_MESSAGE_CREATE，里面就有 group_openid")
    print()
    print("  方式 B：用沙箱环境（后台「沙箱配置」里可直接看到沙箱群的 openid）")
    print("    注意沙箱要用 QQ_SANDBOX=true 且走 sandbox.api.sgroup.qq.com")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python discover_qq_group.py <AppID> <AppSecret>")
        raise SystemExit(2)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
