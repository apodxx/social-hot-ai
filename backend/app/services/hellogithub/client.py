"""HelloGitHub 数据源。

用它的**官方 API**（``https://api.hellogithub.com/v1/``），不爬网页——
实测返回结构化数据，含标题、中文简介、作者、仓库全名，正好够写文案。

配图用 **GitHub 的社交预览卡片**（``opengraph.githubassets.com``）：
每个仓库都有一张现成的卡片图，比作者头像合适得多。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://api.hellogithub.com/v1/"
#: GitHub 的社交预览图。``/1/`` 是缓存版本号，实测可取。
OG_IMAGE = "https://opengraph.githubassets.com/1/{full_name}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SocialHotAI/1.0)"}


@dataclass
class Repo:
    """HelloGitHub 上的一个项目。"""

    full_name: str = ""
    title: str = ""
    summary: str = ""
    author: str = ""
    item_id: str = ""
    url: str = ""
    is_hot: bool = False

    @property
    def og_image(self) -> str:
        return OG_IMAGE.format(full_name=self.full_name) if self.full_name else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "title": self.title,
            "summary": self.summary,
            "author": self.author,
            "url": self.url,
            "og_image": self.og_image,
            "is_hot": self.is_hot,
        }


@dataclass
class RepoList:
    """一次列表请求的结果。"""

    repos: list[Repo] = field(default_factory=list)
    page: int = 1
    has_more: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.repos) and not self.error


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_repos(payload: Any) -> list[Repo]:
    """把 API 返回解析成 :class:`Repo` 列表。

    ``success`` 在返回里是**字符串** ``"True"`` 而不是布尔值（实测），
    所以不能直接 ``if payload["success"]``。
    """
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    repos: list[Repo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        full_name = _as_text(item.get("full_name"))
        if not full_name:
            continue
        repos.append(
            Repo(
                full_name=full_name,
                title=_as_text(item.get("title")),
                summary=_as_text(item.get("summary")),
                author=_as_text(item.get("author")),
                item_id=_as_text(item.get("item_id")),
                url=f"https://github.com/{full_name}",
                is_hot=_as_text(item.get("is_hot")).lower() == "true",
            )
        )
    return repos


async def fetch_repo_readme(full_name: str, *, timeout: float = 25.0) -> tuple[str, str]:
    """取 GitHub 仓库的 README。返回 ``(正文, 错误)``。

    **为什么要单独做**：项目里已有的 ``load_readme`` 是给 README 原始地址写的，
    它会**明确拒绝仓库主页**（报错原文："或是仓库主页而非文件原始地址"）。
    而用户给的通常就是 ``https://github.com/owner/repo`` 这种主页链接。

    走 GitHub API ``/readme`` 而不是拼 ``raw.githubusercontent.com/…/README.md``：
    后者要猜分支名与文件名，而 API 会自己处理默认分支、大小写、子目录、``.rst`` 等情况。
    未认证的限额是 60 次/小时，够用。
    """
    import base64

    import httpx

    if not full_name:
        return "", "仓库名为空"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=True,
            headers={**HEADERS, "Accept": "application/vnd.github+json"},
        ) as client:
            response = await client.get(f"https://api.github.com/repos/{full_name}/readme")
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"

    if response.status_code == 404:
        return "", "这个仓库不存在，或者是私有的"
    if response.status_code == 403:
        return "", "GitHub API 限流（未认证每小时 60 次），稍后再试"
    if response.status_code >= 400:
        return "", f"GitHub API 返回 {response.status_code}"

    try:
        payload = response.json()
    except ValueError as exc:
        return "", f"返回不是 JSON：{exc}"

    name = _as_text(payload.get("name")) or "README"
    content = payload.get("content")
    encoding = _as_text(payload.get("encoding"))
    if encoding == "base64" and isinstance(content, str):
        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return "", f"README 解码失败：{exc}"
    elif isinstance(content, str):
        text = content
    else:
        return "", "README 是空文件，或返回里没有正文"
    if not text.strip():
        return "", "README 是空文件"
    logger.info("github readme %s (%s) -> %d chars", full_name, name, len(text))
    return text, ""


async def fetch_repos(*, page: int = 1, timeout: float = 25.0) -> RepoList:
    """取 HelloGitHub 首页列表（每页 20 个）。**免费**，不花钱。"""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=True,
            headers=HEADERS,
        ) as client:
            response = await client.get(API_BASE, params={"page": page})
    except Exception as exc:  # noqa: BLE001
        return RepoList(page=page, error=f"{type(exc).__name__}: {exc}")

    if response.status_code >= 400:
        return RepoList(page=page, error=f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        return RepoList(page=page, error=f"返回不是 JSON：{exc}")

    repos = parse_repos(payload)
    has_more = _as_text(payload.get("has_more")).lower() == "true" if isinstance(payload, dict) else False
    logger.info("hellogithub page %d -> %d repos", page, len(repos))
    return RepoList(
        repos=repos,
        page=page,
        has_more=has_more,
        error="" if repos else "接口返回里没有项目",
    )
