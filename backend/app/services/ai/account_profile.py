"""The account profile the AI must write for.

The spec requires the analysis to judge "是否适合当前账号" and the rewriting to
follow the account's style, so the profile is a first-class input rather than
baked into a prompt. It lives in a user-editable JSON file
(``account_profile.json`` at the project root by default) and is read fresh on
every run, so editing the file is the whole workflow — no restart, no migration.

A missing file is fine (the analysis still runs, unpersonalised, with a warning).
A *malformed* file is not: :func:`load_account_profile` raises so the mistake is
visible in the API response instead of silently producing bland output.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AccountProfile(BaseModel):
    """Who the content is for, and what it must never say."""

    account_name: str = ""
    field: str = ""
    target_audience: str = ""
    style: str = ""
    tone: str = ""
    preferred_topics: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing meaningful was configured."""
        return not any(
            (self.account_name, self.field, self.target_audience, self.style, self.tone)
        ) and not self.preferred_topics

    def to_prompt_block(self) -> str:
        """Render the profile for a model prompt (Chinese, matching the output)."""
        if self.is_empty:
            return "账号定位：未配置（按通用科技/互联网内容判断）。"
        lines = ["账号定位："]
        if self.account_name:
            lines.append(f"- 账号名称：{self.account_name}")
        if self.field:
            lines.append(f"- 领域：{self.field}")
        if self.target_audience:
            lines.append(f"- 目标受众：{self.target_audience}")
        if self.style:
            lines.append(f"- 表达风格：{self.style}")
        if self.tone:
            lines.append(f"- 语气：{self.tone}")
        if self.preferred_topics:
            lines.append(f"- 偏好话题：{'、'.join(self.preferred_topics)}")
        if self.forbidden:
            lines.append(f"- 禁止：{'、'.join(self.forbidden)}")
        return "\n".join(lines)


@lru_cache(maxsize=8)
def _load_cached(path_text: str) -> AccountProfile:
    """Read and validate one profile file (cached per path)."""
    path = Path(path_text)
    if not path.exists():
        logger.warning("account profile %s not found; analysis runs unpersonalised", path)
        return AccountProfile()
    raw = path.read_text(encoding="utf-8")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"account profile {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"account profile {path} must be a JSON object")
    profile = AccountProfile.model_validate(payload)
    logger.info(
        "account profile loaded from %s (field=%r, preferred_topics=%d)",
        path,
        profile.field,
        len(profile.preferred_topics),
    )
    return profile


def load_account_profile(path: Path | str) -> AccountProfile:
    """Load the profile, raising ``ValueError`` on a malformed file."""
    return _load_cached(str(Path(path)))


def clear_profile_cache() -> None:
    """Forget cached profiles (tests, and after an in-process edit)."""
    _load_cached.cache_clear()
