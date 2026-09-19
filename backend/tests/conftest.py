"""Shared pytest fixtures.

No test in this suite touches the network: the client is exercised through
``respx``, and the adapters are exercised through payloads. Real captured
responses live in ``tests/fixtures/raw/`` (see ``scripts/discover_raw.py``) and
are used when present, so the normalisers can be validated against genuine
provider data without repeating the billed call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings, get_settings  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "raw"


@pytest.fixture
def settings() -> Settings:
    """Settings with a fake key and no retry sleeps.

    Field names are lower-case on purpose: ``extra="ignore"`` would silently
    drop upper-case init kwargs, and the fixture would then fall back to the
    real environment and hit the network.
    """
    return Settings(
        tikhub_api_key="test-key",
        tikhub_base_url="https://api.tikhub.io",
        tikhub_max_retries=2,
        tikhub_backoff_base_seconds=0.0,
        hot_limit_per_platform=5,
        database_url="",
        deepseek_api_key="test-deepseek-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash",
        deepseek_max_retries=2,
        # Interest filtering is on by default (the shipped configuration), but that
        # would couple every unrelated test to the keyword list: a test item titled
        # "某热点事件甲" matches no interest keyword, so with INTEREST_ONLY the analysis
        # would have no candidates and the rewriting tests would fail for a reason that
        # has nothing to do with what they test. The filtering itself is covered by
        # dedicated Phase 10 tests, and the default is asserted there.
        interest_only=False,
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """``get_settings`` is cached; tests mutate the environment."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def load_raw_fixture(platform: str) -> dict[str, Any] | None:
    """A captured real response, or ``None`` when the capture has not run."""
    path = FIXTURE_DIR / f"{platform}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def raw_fixture():
    """Expose :func:`load_raw_fixture` to tests."""
    return load_raw_fixture


@pytest.fixture
def item_factory():
    """Build :class:`HotContent` items without a provider in the loop."""
    from app.models.hot_content import ContentType, HotContent, Platform

    def make(
        platform: str,
        content_id: str,
        title: str,
        *,
        url: str | None = None,
        hot_value: int | None = None,
        rank: int | None = None,
        content_type: ContentType = ContentType.TOPIC,
        **extra,
    ) -> HotContent:
        resolved = Platform(platform) if isinstance(platform, str) else platform
        # ``raw_data`` may be supplied explicitly (a provider payload stands in for
        # the seed marker), so it must not be passed twice.
        raw_data = extra.pop("raw_data", {"seed": content_id})
        return HotContent(
            id=HotContent.make_id(resolved, content_id),
            platform=resolved,
            platform_content_id=content_id,
            title=title,
            url=url,
            hot_value=hot_value,
            rank=rank,
            content_type=content_type,
            raw_data=raw_data,
            **extra,
        )

    return make


@pytest.fixture
async def sqlite_db():
    """An in-memory SQLite database with the real schema installed.

    The models are written with portable types precisely so this works, which
    keeps the Phase 2 suite free of any database server dependency.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db import database as database_module

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    database_module.set_engine(engine)
    await database_module.create_all()
    try:
        yield database_module
    finally:
        await database_module.dispose_engine()


@pytest.fixture
def session_factory(sqlite_db):
    """Session factory bound to the SQLite fixture."""
    from app.db.database import get_session_factory

    return get_session_factory()
