"""Verify the running server: the admin UI, the assets, the API and the SPA fallback.

Two rules this script exists to enforce, both learned from real incidents:

* ``trust_env=False`` — the Windows registry proxy (127.0.0.1:7890) makes httpx route
  localhost through a proxy and return 502 with no server-side log at all.
* Capture with httpx, never ``Invoke-WebRequest`` — PowerShell decodes a charset-less
  UTF-8 body as ISO-8859-1 and silently mangles Chinese text.

    python scripts/verify_server.py [base_url] [--probe]

``--probe`` additionally performs the live connectivity check (TikHub's free account
metadata endpoint and DeepSeek's model list — no tokens, no billing). Without it the
status check runs with ``?probe=false`` so the default run makes no external call.
"""

from __future__ import annotations

import sys

import httpx

ARGS = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
PROBE = "--probe" in sys.argv
BASE = ARGS[0] if ARGS else "http://127.0.0.1:8000"
# A localhost call must not consult the system proxy.
CLIENT = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)

failures: list[str] = []


def _real_secrets() -> dict[str, str]:
    """The actual secret values from ``.env``, to prove none of them leak."""
    import sys as _sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in _sys.path:
        _sys.path.insert(0, str(backend_root))

    from app.services.settings_editor import SECRET_KEYS, read_env_file

    on_disk = read_env_file()
    return {key: on_disk.get(key, "") for key in sorted(SECRET_KEYS)}


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(name)


def main() -> int:
    # --- the SPA shell ---------------------------------------------------------
    root = CLIENT.get("/")
    check("GET / is 200", root.status_code == 200, str(root.status_code))
    check("GET / is HTML", root.headers.get("content-type", "").startswith("text/html"))
    check("GET / contains the app mount", 'id="app"' in root.text)
    check("GET / has no charset mojibake", "\ufffd" not in root.text)

    # The bundle path is hashed, so read it out of the shell rather than guessing.
    import re

    scripts = re.findall(r'src="(/assets/[^"]+)"', root.text)
    check("the shell references a hashed bundle", bool(scripts), ", ".join(scripts))
    for script in scripts:
        asset = CLIENT.get(script)
        check(f"GET {script} is 200", asset.status_code == 200, str(asset.status_code))
        check(f"{script} is javascript", "javascript" in asset.headers.get("content-type", ""))
        check(f"{script} is non-empty", len(asset.content) > 10000, f"{len(asset.content)} bytes")

    styles = re.findall(r'href="(/assets/[^"]+\.css)"', root.text)
    for style in styles:
        asset = CLIENT.get(style)
        check(f"GET {style} is 200", asset.status_code == 200, str(asset.status_code))

    # --- deep links (a browser refresh on a Vue route) -------------------------
    for path in ("/hot", "/topics", "/rewrites", "/tasks", "/settings"):
        page = CLIENT.get(path)
        check(f"deep link {path} serves the shell", page.status_code == 200 and 'id="app"' in page.text)

    # --- the API ---------------------------------------------------------------
    stats = CLIENT.get("/api/system/stats", params={"today": "false"})
    check("GET /api/system/stats is 200", stats.status_code == 200, str(stats.status_code))
    if stats.status_code == 200:
        payload = stats.json()["stats"]
        print(
            "        totals: items={total_items} analyses={analyses} selected={selected} "
            "rewrites={rewrites} needs_review={needs_review}".format(**payload)
        )

    stored = CLIENT.get("/api/hot/stored", params={"limit": 2})
    check("GET /api/hot/stored is 200", stored.status_code == 200, str(stored.status_code))
    if stored.status_code == 200:
        body = stored.json()
        print(f"        stored rows: total={body['total']} returned={len(body['items'])}")
        for item in body["items"][:2]:
            title = item["title"][:34]
            check(
                f"stored title is not mojibake: {title}",
                "\ufffd" not in item["title"],
            )

    settings_response = CLIENT.get("/api/settings")
    check("GET /api/settings is 200", settings_response.status_code == 200)
    if settings_response.status_code == 200:
        body = settings_response.json()["settings"]
        check("the settings page has groups", bool(body.get("groups")))
        # Checking for a prefix like "sk-" is useless: the *mask* legitimately keeps
        # the first characters (sk-c37…). The real assertion is that the deployed
        # secret values themselves never cross the wire.
        leaked = [
            key
            for key, value in _real_secrets().items()
            if len(value) >= 8 and value in settings_response.text
        ]
        check(
            "no configured secret value is returned",
            not leaked,
            f"leaked: {', '.join(leaked)}" if leaked else f"checked {len(_real_secrets())} keys",
        )

    status = CLIENT.get("/api/system/status", params={"probe": "true" if PROBE else "false"})
    check("GET /api/system/status is 200", status.status_code == 200)
    if status.status_code == 200:
        body = status.json()
        print("        phase={phase} probe={}".format(PROBE, **body))
        for name, component in body["components"].items():
            print(f"        {name}: {component['status']} — {component.get('detail')}")
        if PROBE:
            check(
                "TikHub is reachable and the key is valid",
                body["components"]["tikhub"]["status"] == "connected",
                body["components"]["tikhub"].get("detail", ""),
            )
            check("the app reports itself healthy", body["success"] is True)
        else:
            # Not "not_configured": the keys are present, the probe was just skipped.
            check(
                "a skipped probe is not reported as unconfigured",
                body["components"]["tikhub"]["status"] == "skipped",
                body["components"]["tikhub"]["status"],
            )

    # --- the API must win over the SPA catch-all -------------------------------
    missing = CLIENT.get("/api/definitely-not-a-route")
    check("unknown /api path is 404", missing.status_code == 404, str(missing.status_code))
    check(
        "unknown /api path is JSON, not HTML",
        missing.headers.get("content-type", "").startswith("application/json"),
        missing.headers.get("content-type", ""),
    )
    check("unknown /api path does not serve the shell", 'id="app"' not in missing.text)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
