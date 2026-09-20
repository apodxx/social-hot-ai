"""Editing deployment settings from the admin UI (spec section 三十二).

Section 三十二 lets the operator change the TikHub/DeepSeek credentials, the hot
count, the run times, the account profile, and the notification channel — with one
hard rule: **an API key must never be displayed in full**.

Three decisions are worth stating:

* **Secrets are write-only.** :func:`mask` returns a recognisable fragment
  (``sk-c374…dac4``) or nothing at all, and the full value is never sent to a
  client. A blank field on save means "leave it alone", so an operator can edit a
  neighbouring value without retyping a key.
* **The ``.env`` file is edited in place.** Comments, ordering, and keys this
  module does not know about are preserved; only the named keys change. The write
  is atomic (temp file + replace) so a crash cannot truncate the file.
* **Restart semantics are reported, not hidden.** Everything read from the
  environment at startup — credentials, limits, cron times — needs a restart.
  The account profile does *not*, because it is re-read on every run, so the API
  clears its cache instead of demanding a restart.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

ENV_PATH = PROJECT_ROOT / ".env"


def env_file_path() -> Path:
    """The environment file this module edits.

    Resolved on every call rather than bound as a default argument. ``def f(path=ENV_PATH)``
    captures the value once at import, so a test could not redirect the write without
    rewriting the deployment's real ``.env`` — and an API test of ``PUT /api/settings``
    would do exactly that.
    """
    return ENV_PATH

_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


@dataclass(frozen=True)
class SettingSpec:
    """One editable setting."""

    key: str
    group: str
    label: str
    kind: str = "string"  # string | int | bool | secret | time_list
    help: str = ""
    #: True when the value is only read at process start.
    restart_required: bool = True


#: Everything the admin UI may change. Deliberately a allow-list: an unknown key
#: in a request is rejected rather than written into the environment file.
EDITABLE: tuple[SettingSpec, ...] = (
    SettingSpec("TIKHUB_API_KEY", "tikhub", "TikHub API Key", "secret"),
    SettingSpec("TIKHUB_BASE_URL", "tikhub", "TikHub Base URL"),
    SettingSpec("TIKHUB_TIMEOUT_SECONDS", "tikhub", "请求超时（秒）", "int"),
    SettingSpec("TIKHUB_MAX_RETRIES", "tikhub", "最大重试次数", "int"),
    SettingSpec("DEEPSEEK_API_KEY", "deepseek", "DeepSeek API Key", "secret"),
    SettingSpec("DEEPSEEK_MODEL", "deepseek", "DeepSeek 模型", help="如 deepseek-flash"),
    SettingSpec("DEEPSEEK_MAX_TOKENS", "deepseek", "分析最大输出 token", "int"),
    SettingSpec("REWRITE_MAX_TOKENS", "deepseek", "二创最大输出 token", "int"),
    SettingSpec("HOT_LIMIT_PER_PLATFORM", "hot", "每平台条数", "int"),
    SettingSpec("HOT_FETCH_TIMES", "hot", "定时运行时间", "time_list", help="如 08:00,12:00,18:00"),
    SettingSpec(
        "SEARCH_LIMIT_PER_PLATFORM",
        "hot",
        "搜索每平台条数",
        "int",
        help="影响入库量与图片下载量，不影响计费次数",
    ),
    SettingSpec("MEDIA_DOWNLOAD_ENABLED", "media", "下载图片到本地素材库", "bool", help="平台图片链接会过期，建议开启"),
    SettingSpec("MEDIA_MAX_IMAGES_PER_ITEM", "media", "每条最多下载图片数", "int"),
    SettingSpec("MEDIA_MAX_BYTES_PER_FILE", "media", "单张图片最大字节", "int"),
    SettingSpec("MEDIA_ROOT", "media", "素材库目录（相对项目根）"),
    SettingSpec(
        "INTEREST_KEYWORDS",
        "interest",
        "领域关键词（逗号分隔）",
        "string",
        help="决定哪些内容会被送去 AI 分析；标题命中权重更高",
    ),
    SettingSpec(
        "INTEREST_NEGATIVE_KEYWORDS",
        "interest",
        "排除关键词（逗号分隔）",
        "string",
        help="命中即降权，例如娱乐八卦类",
    ),
    SettingSpec(
        "INTEREST_ONLY",
        "interest",
        "只分析领域相关内容",
        "bool",
        help="开启后与领域无关的条目不送 AI 分析，省钱；但榜单没有相关内容时可能无候选",
    ),
    SettingSpec("WATCH_SEARCH_ENABLED", "watch", "启用定时领域搜索", "bool", help="每轮按关键词搜索，会计费"),
    SettingSpec(
        "WATCH_KEYWORDS",
        "watch",
        "领域搜索词（逗号分隔）",
        "string",
        help="每个词每平台 1 次计费调用",
    ),
    SettingSpec("WATCH_SEARCH_PLATFORMS", "watch", "领域搜索平台", "string", help="如 xiaohongshu,douyin"),
    SettingSpec("WATCH_SEARCH_LIMIT", "watch", "每个词保留条数", "int", help="不影响计费次数"),
    SettingSpec("WATCH_DOWNLOAD_MEDIA", "watch", "下载领域搜索结果的图片", "bool"),
    SettingSpec(
        "DASHSCOPE_API_KEY",
        "image",
        "DashScope API Key（千问图像）",
        "secret",
        help="阿里云百炼，用于图片二创",
    ),
    SettingSpec("DASHSCOPE_BASE_URL", "image", "DashScope 工作空间 Endpoint", "string"),
    SettingSpec("QWEN_IMAGE_MODEL", "image", "图像模型", "string", help="如 qwen-image-3.0-pro"),
    SettingSpec(
        "OCR_BASE_URL",
        "image",
        "OCR 专用 Endpoint",
        "string",
        help="私有部署域名（ws-*.maas.aliyuncs.com），与上面的 DashScope 域名不同，必须单独填",
    ),
    SettingSpec("OCR_MODEL", "image", "OCR 模型", "string", help="如 qwen3.5-ocr / qwen-vl-ocr"),
    SettingSpec(
        "OCR_MAX_IMAGES",
        "image",
        "单次最多识别几张图",
        "int",
        help="每张约 1000 图片 token，多了既慢又贵",
    ),
    SettingSpec(
        "OMNI_BASE_URL", "image", "全模态 Endpoint（视频理解）", "string",
        help="标准 dashscope.aliyuncs.com 的 compatible-mode 地址",
    ),
    SettingSpec("OMNI_MODEL", "image", "全模态模型", "string", help="如 qwen3.8-omni-flash"),
    SettingSpec("IMAGE_GEN_ENABLED", "image", "启用图片二创", "bool"),
    SettingSpec(
        "IMAGE_GEN_SIZE",
        "image",
        "输出尺寸",
        "size",
        help="如 1024*1024（1K 档 $0.034/张）。超过 2.25M 像素按 2K 档 $0.069/张 计费",
    ),
    SettingSpec("IMAGE_GEN_N", "image", "每次生成张数（1-6）", "int", help="每张都单独计费"),
    SettingSpec("IMAGE_GEN_MAX_PER_RUN", "image", "单次运行最多生成张数", "int", help="防止意外批量烧钱"),
    SettingSpec("ANALYSIS_MAX_CANDIDATES", "analysis", "送分析的候选条数", "int"),
    SettingSpec("ANALYSIS_MAX_SELECTED", "analysis", "最终入选条数", "int"),
    SettingSpec("ANALYSIS_REUSE_HOURS", "analysis", "分析复用时长（小时）", "int"),
    SettingSpec("ANALYSIS_SEMANTIC_MERGE", "analysis", "语义话题合并", "bool"),
    SettingSpec("DETAIL_FETCH_ENABLED", "detail", "抓取详情正文（计费）", "bool"),
    SettingSpec("DETAIL_MAX_ITEMS", "detail", "每次最多抓取详情条数", "int"),
    SettingSpec("REWRITE_REUSE_HOURS", "rewrite", "二创复用时长（小时）", "int"),
    SettingSpec("SCHEDULER_ENABLED", "scheduler", "启用定时任务", "bool"),
    SettingSpec("SCHEDULER_TIMEZONE", "scheduler", "调度时区"),
    SettingSpec("QQ_ENABLED", "qq", "启用 QQ 推送", "bool"),
    SettingSpec("QQ_APP_ID", "qq", "机器人 AppID", "string"),
    SettingSpec("QQ_APP_SECRET", "qq", "机器人 AppSecret", "secret"),
    SettingSpec(
        "QQ_GROUP_OPENID",
        "qq",
        "目标群 group_openid",
        "string",
        help="推荐：图片（富媒体）只支持群聊与单聊，而文字子频道要求机器人常驻 WebSocket",
    ),
    SettingSpec("QQ_TARGET_OPENID", "qq", "目标用户 user_openid（单聊，无图片）", "string"),
    SettingSpec("QQ_SANDBOX", "qq", "使用沙箱环境", "bool"),
    SettingSpec(
        "QQ_MAX_IMAGES",
        "qq",
        "每次最多发送图片数",
        "int",
        help="每张图是一条独立消息；QQ 主动消息单关系限 20/qpm、1000 条/天",
    ),
    SettingSpec(
        "ARTICLE_MAX_TOKENS",
        "ai",
        "科普文章输出上限",
        "int",
        help="文章很长，太小会被截断成不合法 JSON；DeepSeek 通常最高 8192",
    ),
    SettingSpec("NOTIFICATION_ENABLED", "notification", "启用通知", "bool"),
    SettingSpec("NOTIFICATION_CHANNEL", "notification", "通知通道"),
    SettingSpec("NOTIFICATION_CHANNELS", "notification", "通知通道（多个，逗号分隔）"),
    SettingSpec("WECHAT_ENABLED", "notification", "启用企业微信", "bool"),
    SettingSpec("WECHAT_WEBHOOK_URL", "notification", "企业微信机器人 Webhook", "secret"),
    SettingSpec("QQ_ENABLED", "notification", "启用 QQ 官方 Bot", "bool"),
    SettingSpec("QQ_APP_ID", "notification", "QQ AppID"),
    SettingSpec("QQ_APP_SECRET", "notification", "QQ AppSecret", "secret"),
    SettingSpec("QQ_TARGET_OPENID", "notification", "QQ 目标 openid"),
    SettingSpec("QQ_CHANNEL_ID", "notification", "QQ 频道 ID"),
    SettingSpec("MCP_ENABLED", "mcp", "启用 MCP 服务", "bool", help="挂载 /mcp/ 供 AI 客户端调用"),
    SettingSpec(
        "MCP_ALLOW_BILLED",
        "mcp",
        "允许 MCP 调用计费工具",
        "bool",
        help="关闭后采集/分析/二创/流水线工具不会注册",
    ),
)

SPEC_BY_KEY = {spec.key: spec for spec in EDITABLE}

#: Keys the UI must never receive in full.
SECRET_KEYS = frozenset(spec.key for spec in EDITABLE if spec.kind == "secret")


def mask(value: str, *, keep_start: int = 6, keep_end: int = 4) -> str:
    """A recognisable fragment of a secret, never the whole thing."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= keep_start + keep_end:
        return "•" * len(text)
    return f"{text[:keep_start]}…{text[-keep_end:]}"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs, ignoring comments and blank lines."""
    path = path or env_file_path()
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def write_env_file(updates: dict[str, str], path: Path | None = None) -> list[str]:
    """Apply ``updates`` in place; returns the keys that changed.

    Comments and unknown keys survive, existing keys keep their position, and new
    keys are appended. The file is replaced atomically.
    """
    path = path or env_file_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    changed: list[str] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        match = _LINE_RE.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        key = match.group(1)
        if key not in updates:
            continue
        seen.add(key)
        new_value = updates[key]
        reordered = f"{key}={new_value}"
        if line != reordered:
            changed.append(key)
        lines[index] = reordered

    appended = [key for key in updates if key not in seen]
    if appended:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# added by the admin UI")
        for key in appended:
            lines.append(f"{key}={updates[key]}")
        changed.extend(appended)

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as handle_file:
            handle_file.write(content)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    logger.info("settings file updated: %s", ", ".join(changed) or "no change")
    return changed


def _validation_reason(exc: Exception) -> str:
    """Pull the human-readable reason out of a pydantic ``ValidationError``.

    ``str(exc)`` is multi-line: a header, the field name, then the message with a
    ``[type=..., input_value=...]`` suffix. Surfacing only the field name (as the
    first attempt did) tells the operator nothing about what to change.
    """
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    if len(lines) <= 1:
        return str(exc)
    # The message is the line carrying the `[type=...]` marker; the trailing line
    # is pydantic's "For further information visit ..." link, which says nothing
    # about what the operator should actually change.
    reason = next((line for line in lines if " [type=" in line), None)
    if reason is None:
        reason = next(
            (line for line in reversed(lines) if not line.startswith("For further information")),
            lines[-1],
        )
    return reason.split(" [type=", 1)[0].strip()


def validate_updates(updates: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Check a request against the allow-list; returns ``(clean, errors)``.

    Typed values are validated by constructing a :class:`Settings` with that field
    set, so the schema stays the single source of truth: a rule added to the
    configuration is enforced here automatically. (A plain ``int()`` check was not
    enough — ``ANALYSIS_MAX_SELECTED=0`` passed it and would have produced a run
    that selects nothing.)
    """
    clean: dict[str, str] = {}
    errors: list[str] = []
    for key, raw in updates.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            errors.append(f"{key} is not editable here")
            continue
        text = "" if raw is None else str(raw).strip()
        if spec.kind == "secret" and text == "":
            # Blank means "keep the stored value", never "erase the key".
            continue
        if "\n" in text:
            errors.append(f"{key} must not contain a newline")
            continue
        if spec.kind in {"int", "bool", "time_list", "size"}:
            try:
                Settings(**{spec.key.lower(): text})
            except Exception as exc:  # noqa: BLE001 - the validation message is the point
                errors.append(f"{key} is invalid: {_validation_reason(exc)}")
                continue
        clean[key] = text
    return clean, errors


def describe_settings(settings: Settings, path: Path | None = None) -> dict[str, Any]:
    """The current values, secrets masked, grouped for the UI."""
    path = path or env_file_path()
    on_disk = read_env_file(path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for spec in EDITABLE:
        current = on_disk.get(spec.key)
        if current is None:
            current = str(getattr(settings, spec.key.lower(), "") or "")
        entry: dict[str, Any] = {
            "key": spec.key,
            "label": spec.label,
            "kind": spec.kind,
            "help": spec.help,
            "restart_required": spec.restart_required,
            "configured": bool(current),
        }
        if spec.kind == "secret":
            entry["value"] = ""  # never returned
            entry["masked"] = mask(current)
        else:
            entry["value"] = current
        groups.setdefault(spec.group, []).append(entry)
    return {
        "path": str(path),
        "exists": path.exists(),
        "groups": groups,
        "secret_keys": sorted(SECRET_KEYS),
        "note": (
            "环境变量在进程启动时读取：这些修改需要重启后端才生效。"
            "账号定位不走环境变量，保存后立即生效。"
        ),
    }


def mask_all(values: dict[str, str]) -> dict[str, str]:
    """Mask every secret-looking entry of a mapping (diagnostics helper)."""
    return {
        key: mask(value) if key in SECRET_KEYS else value for key, value in values.items()
    }


__all__ = [
    "EDITABLE",
    "ENV_PATH",
    "SECRET_KEYS",
    "SettingSpec",
    "describe_settings",
    "env_file_path",
    "mask",
    "mask_all",
    "read_env_file",
    "validate_updates",
    "write_env_file",
]
