"""The bundled admin UI is served by the API process itself.

These tests exist because of a real incident: ``_mount_frontend`` ended with two
statements copied from ``create_app`` (``app.state.settings = settings``) that
referenced a name not in scope. The whole test suite passed for as long as
``frontend/dist`` did not exist — the moment ``npm run build`` produced it, the app
raised ``NameError`` during ``create_app`` and the server could not start at all.

So the mount path is tested with a fake build present, and it asserts the four
properties that matter: the shell, the hashed assets, deep links, and the fact that
an unknown ``/api/*`` path is a JSON 404 rather than the SPA's HTML.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def fake_dist(monkeypatch, tmp_path):
    """A stand-in for ``frontend/dist`` with one shell and one hashed asset."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="app"></div>'
        '<script type="module" src="/assets/index-abc123.js"></script></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc123.js").write_text(
        "console.log('social-hot-ai bundle')\n", encoding="utf-8"
    )
    # A file outside dist that a path-traversal attempt would try to reach.
    (tmp_path / "secret.txt").write_text("do not serve me", encoding="utf-8")
    monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)
    return dist


@pytest.fixture
def client(monkeypatch, sqlite_db, fake_dist):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_app_starts_with_a_build_present(client):
    """The regression guard: building the UI must not break app creation."""
    assert client.get("/").status_code == 200


def test_root_serves_the_shell(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<div id="app">' in response.text


def test_hashed_assets_are_served(client):
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "social-hot-ai bundle" in response.text


def test_deep_links_fall_back_to_the_shell(client):
    """A refresh on a Vue route must not 404 — the router reads the real path."""
    for path in ("/hot", "/topics/12", "/rewrites/34", "/settings"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert '<div id="app">' in response.text, path


def test_unknown_api_path_is_a_json_404(client):
    """An unknown /api path must not be answered with the SPA's HTML."""
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_assets_path_does_not_fall_back(client):
    """A missing asset must 404: serving index.html here breaks module loading."""
    response = client.get("/assets/missing-file.js")
    assert response.status_code == 404


def test_path_traversal_outside_dist_is_not_served(client):
    response = client.get("/../secret.txt")
    assert "do not serve me" not in response.text


def test_api_still_works_alongside_the_ui(client):
    """Mounting the catch-all must not shadow the API routes."""
    response = client.get("/api/system/stats?today=false")
    assert response.status_code == 200
    assert "stats" in response.json()
