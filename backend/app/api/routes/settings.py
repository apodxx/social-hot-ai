"""``/api/settings`` — Phase 7: the settings page's backend (spec section 三十二).

Guarantees this endpoint makes, and why each one matters:

* **A secret is never returned in full.** ``GET`` sends a mask (``sk-c374…dac4``)
  or an empty string; the full value stays on disk.
* **A blank secret field means "keep it".** Editing the hot count must not force
  retyping a key, and must never erase one by accident.
* **Only known keys are writable.** Anything else is rejected with a 422 rather
  than written into the environment file.
* **Restart semantics are reported.** Environment values are read at process
  start, so the response says ``restart_required`` instead of implying the change
  is already live. The account profile is the exception: it is re-read on every
  run, so saving it takes effect immediately.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.ai.account_profile import AccountProfile, clear_profile_cache, load_account_profile
from app.services.settings_editor import (
    EDITABLE,
    describe_settings,
    validate_updates,
    write_env_file,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class SettingsUpdate(BaseModel):
    """The values to change, keyed by environment-variable name."""

    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Environment-variable names to update; an empty secret value keeps the stored one.",
    )


class ProfileUpdate(BaseModel):
    """The account profile the AI writes for."""

    account_name: str = ""
    field: str = ""
    target_audience: str = ""
    style: str = ""
    tone: str = ""
    preferred_topics: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


@router.get("/settings", summary="Editable settings with secrets masked (free)")
async def get_settings_endpoint() -> dict[str, Any]:
    """Current configuration, grouped for the settings page."""
    settings = get_settings()
    described = describe_settings(settings)
    described["editable_keys"] = [spec.key for spec in EDITABLE]
    return {"success": True, "settings": described}


@router.put(
    "/settings",
    summary="Update settings",
    description=(
        "Writes the named keys into the environment file, preserving comments and "
        "unknown keys. **Secrets are write-only**: send a value to set one, leave the "
        "field empty to keep the stored one. The response reports `restart_required`, "
        "because environment values are read when the process starts."
    ),
)
async def update_settings_endpoint(payload: SettingsUpdate) -> dict[str, Any]:
    """Apply a settings change to the environment file."""
    clean, errors = validate_updates(payload.values)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    if not clean:
        return {
            "success": True,
            "changed": [],
            "restart_required": False,
            "note": "没有需要修改的项（密钥留空表示保持原值）",
        }
    changed = write_env_file(clean)
    logger.info("settings updated via the admin UI: %s", ", ".join(changed))
    return {
        "success": True,
        "changed": changed,
        "restart_required": bool(changed),
        "note": "环境变量在进程启动时读取：请重启后端使这些修改生效",
    }


@router.put(
    "/settings/profile",
    summary="Save the account profile",
    description=(
        "Writes `account_profile.json` and reloads it immediately — unlike the "
        "environment file, no restart is needed, because the profile is re-read on "
        "every analysis run."
    ),
)
async def update_profile_endpoint(payload: ProfileUpdate) -> dict[str, Any]:
    """Save the account profile and clear the loader cache."""
    settings = get_settings()
    path: Path = settings.account_profile_file
    profile = AccountProfile.model_validate(payload.model_dump())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write {path}: {exc}") from exc
    clear_profile_cache()
    # Read it back so the response proves what is now effective.
    reloaded = load_account_profile(path)
    logger.info("account profile saved via the admin UI: %s", path)
    return {
        "success": True,
        "path": str(path),
        "profile": reloaded.model_dump(mode="json"),
        "prompt_block": reloaded.to_prompt_block(),
        "restart_required": False,
        "note": "账号定位在每次运行时重新读取，已立即生效",
    }
