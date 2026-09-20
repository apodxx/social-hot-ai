"""常驻 QQ 机器人：按 @ 的指令推送内容。

在群里 @机器人 并说出指令即可：

| 指令 | 行为 | 计费 |
|---|---|---|
| `小红书` / `微博` / `抖音` | 回复**最新一篇知识科普**的对应平台文案 | 免费（只读数据库） |
| `知识科普` | **随机挑一个标签，生成一篇新文章**并回复 | **计费**：1-2 次 DeepSeek + 1 次搜图，约 ¥0.05 |
| `帮助` | 指令列表 | 免费 |

「随机生成」是**会花钱**的动作，而群里的任何人都能触发它，所以内置两道闸：
**冷却时间**（默认 90 秒）与**每日上限**（默认 10 次）。超限时如实回复还剩多久/今天已用几次，
而不是默默扣钱。

回复格式：**1 条文字 + 最多 3 张图**（可用 ``--images`` 调整）。

为什么要常驻而不是一次性脚本：用户是随时 @ 的，而被动回复的 ``msg_id`` 只在收到事件的
5 分钟内有效——所以必须有个进程一直连着网关，收到就立刻回。

    python scripts/qq_bot.py                # 前台常驻
    python scripts/qq_bot.py --images 3     # 每条回复最多 3 张图
    python scripts/qq_bot.py --sandbox      # 用沙箱环境
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings, get_settings  # noqa: E402
from app.models.knowledge import KnowledgeArticleRecord  # noqa: E402
from app.services.notification.qq import QQBotChannel  # noqa: E402

PRODUCTION = "https://api.sgroup.qq.com"
SANDBOX = "https://sandbox.api.sgroup.qq.com"
INTENT_GROUP_AND_C2C = 1 << 25
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_HELLO = 10
HEADERS = {"User-Agent": "SocialHotAI/1.0 (QQBot)"}
MAX_RECONNECTS = 50

#: 消息里可能带 ``<@!1234>`` 这类提及标记，匹配指令前先去掉。
#:
#: **必须是字母数字，不能只写 ``\d+``。** 实测机器人 ID 是**十六进制**
#: （``D0F1E2A80ADFCE7447691C7105532D67``），只匹配数字会让 @ 标记整段留下，
#: 指令于是变成 ``<@D0F1…> 知识科普``、谁也认不出来。
MENTION_RE = re.compile(r"<@!?[0-9A-Za-z]+>")

#: 群消息有两种事件类型，**都要处理**：
#:   ``GROUP_AT_MESSAGE_CREATE`` —— 平台判定为"@了机器人"
#:   ``GROUP_MESSAGE_CREATE``    —— 群里的普通消息（@ 标记以文本形式出现在 content 里）
#: 实测本机器人收到的是后者（content 里带 ``<@十六进制ID>``）。只认前者会让机器人
#: 对 @ 完全没反应——而症状看起来和"事件没送达"一模一样。
GROUP_MESSAGE_EVENTS = ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE")

#: ``关键词：mysql`` / ``关键词: mysql`` / ``主题：xxx`` —— 全角半角冒号都收。
KEYWORD_RE = re.compile(r"^(?:关键词|主题|keyword)\s*[:：]\s*(.+)$", re.IGNORECASE)

#: 平台指令 → knowledge_articles.platforms 里的键。
#: 平台指令 → knowledge_articles.platforms 里的键。
#:
#: **同时接受带「科普」前缀的写法。** 按钮上写的是「科普小红书」（为了和热点组并排时能分清），
#: 而人可能照着按钮打字、也可能直接打「小红书」——两种都得认，否则会出现
#: "按钮写着 X、帮助说 Y、照着 X 打却不认"这种最让人困惑的情况（实测差点就这样）。
PLATFORM_COMMANDS = {
    "小红书": "xiaohongshu",
    "微博": "weibo",
    "抖音": "douyin",
    "科普小红书": "xiaohongshu",
    "科普微博": "weibo",
    "科普抖音": "douyin",
}
PLATFORM_LABELS = {value: key for key, value in PLATFORM_COMMANDS.items()}

#: 从**热点列表**取内容的指令 —— 加「热点」前缀，和科普文章的 `@小红书` 区分开。
#:
#: 两套内容完全不同，混在一起会弄不清：
#:   ``小红书``      → 我们自己写的**科普文章**的对应平台文案（knowledge_articles）
#:   ``热点小红书``   → **热点列表里别人的帖子**被 AI 改写后的对应平台文案（hot_contents + ai_rewrites）
HOT_PLATFORM_COMMANDS: dict[str, str] = {
    "热点小红书": "xiaohongshu",
    "热点微博": "weibo",
    "热点抖音": "douyin",
}
HOT_LABELS = {value: key for key, value in HOT_PLATFORM_COMMANDS.items()}

#: 内嵌指令面板：**点一下就把指令填进输入框**，不用手打。
#:
#: 为什么用按钮而不是让用户去控制台配「指令面板」：控制台配置要人工维护、且只在部分场景
#: 生效；而 ``keyboard`` 是发消息接口的字段，**我这边直接带上就能用**。
#: 实测它可以挂在**普通文本消息**上（``msg_type=0`` + ``keyboard`` → 200），
#: 所以不需要 Markdown 模板权限（那会撞 304036/304127）。
#:
#: ``action.type=2`` 是指令按钮（"自动在输入框插入 @bot data"）。文档说 ``enter``
#: （点一下直接发送、无需再按发送键）**仅单聊可用**，所以群里是"填入输入框"——
#: 依然比手打省事。
#:
#: ``label`` 上限 10 个字符；``permission.type=2`` 表示所有人可用；
#: ``style``：0 灰框 / 1 蓝框 / 4 蓝底白字。
COMMAND_KEYBOARD: dict[str, Any] = {
    "content": {
        "rows": [
            {
                # 第一行：我们自己写的**科普文章**
                "buttons": [
                    {
                        "id": "cmd_xiaohongshu",
                        "render_data": {"label": "科普小红书", "style": 1},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "科普小红书"},
                    },
                    {
                        "id": "cmd_weibo",
                        "render_data": {"label": "科普微博", "style": 1},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "科普微博"},
                    },
                    {
                        "id": "cmd_douyin",
                        "render_data": {"label": "科普抖音", "style": 1},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "科普抖音"},
                    },
                ]
            },
            {
                # 第二行：**热点列表**里别人的帖子被改写
                "buttons": [
                    {
                        "id": "cmd_hot_xhs",
                        "render_data": {"label": "热点小红书", "style": 0},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "热点小红书"},
                    },
                    {
                        "id": "cmd_hot_wb",
                        "render_data": {"label": "热点微博", "style": 0},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "热点微博"},
                    },
                    {
                        "id": "cmd_hot_dy",
                        "render_data": {"label": "热点抖音", "style": 0},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "热点抖音"},
                    },
                ]
            },
            {
                "buttons": [
                    {
                        "id": "cmd_knowledge",
                        "render_data": {"label": "知识科普", "style": 4},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "知识科普"},
                    },
                    {
                        "id": "cmd_help",
                        "render_data": {"label": "帮助", "style": 0},
                        "action": {"type": 2, "permission": {"type": 2}, "data": "帮助"},
                    },
                ]
            },
        ]
    }
}


@dataclass
class Budget:
    """「会花钱的指令」的闸门：冷却 + 每日上限。"""

    cooldown_seconds: float = 90.0
    daily_limit: int = 10
    last_run: float = 0.0
    day: str = ""
    used_today: int = 0

    def _roll_day(self) -> None:
        """跨天归零。

        **``consume()`` 也必须调用它**：第一版只在 ``check()`` 里翻日，于是
        ``consume(); consume(); check()`` 会因为 ``day`` 仍是空串而被当成新的一天，
        把计数清零（测试逮到的）。生产路径上 ``check()`` 总在 ``consume()`` 之前，
        所以没暴露出来——但这种"只靠调用顺序才正确"的实现不该留着。
        """
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.used_today = 0

    def check(self) -> str:
        """返回空字符串表示可以执行，否则返回拒绝原因。"""
        self._roll_day()
        if self.used_today >= self.daily_limit:
            return f"今天的生成次数已用完（{self.used_today}/{self.daily_limit}），明天再来。"
        wait = self.cooldown_seconds - (time.time() - self.last_run)
        if self.last_run and wait > 0:
            return f"刚生成过，请等 {int(wait) + 1} 秒再试（防止连续点击把额度刷掉）。"
        return ""

    def consume(self) -> None:
        self._roll_day()
        self.last_run = time.time()
        self.used_today += 1


@dataclass
class _ArticleView:
    """把生成结果（dict）包成 :func:`article_fallback_text` 需要的形状。

    生成路径上拿到的是 dict（还没落库），而 ``platform_text`` 处理的是数据库行——
    两者字段同名，用一个轻量包装复用同一段兜底逻辑，避免复制一份。
    """

    payload: dict[str, Any]

    @property
    def title(self) -> str:
        return str(self.payload.get("title") or "")

    @property
    def hook(self) -> str:
        return str(self.payload.get("hook") or "")

    @property
    def sections(self) -> list[Any]:
        return list(self.payload.get("sections") or [])

    @property
    def glossary(self) -> list[Any]:
        return list(self.payload.get("glossary") or [])

    @property
    def takeaways(self) -> list[Any]:
        return list(self.payload.get("takeaways") or [])


async def latest_hot_rewrite(settings: Settings) -> tuple[Any, Any] | None:
    """最新一条**已二创的热点**（热点列表 → AI 改写）。免费读库。"""
    from app.db.database import session_scope
    from app.db.repository import list_rewrites

    async with session_scope(settings) as session:
        rows, _total = await list_rewrites(session, limit=1)
    return rows[0] if rows else None


def hot_platform_text(rewrite: Any, item: Any, platform: str) -> str:
    """组装热点二创的某个平台文案，并标明出处（原标题 + 链接）。

    **一定要标出处**：这条内容来自别人的帖子，和科普文章不同，用户需要知道自己在看什么。
    """
    header = f"【热点二创·{HOT_LABELS.get(platform, platform)}】\n"
    header += f"原帖：{item.title or '(无标题)'}（{item.platform}）\n\n"
    if platform == "douyin":
        parts = [
            f"钩子：{rewrite.douyin_hook or ''}",
            str(rewrite.douyin_script or ""),
            f"结尾：{rewrite.douyin_cta or ''}" if rewrite.douyin_cta else "",
        ]
        body = "\n\n".join(part for part in parts if part.strip())
    elif platform == "weibo":
        parts = [
            str(rewrite.weibo_title or rewrite.weibo_opening or ""),
            str(rewrite.weibo_content or ""),
            " ".join(
                tag if str(tag).startswith("#") else f"#{tag}"
                for tag in (rewrite.weibo_hashtags or [])
            ),
        ]
        body = "\n\n".join(part for part in parts if part.strip())
    else:
        parts = [
            str(rewrite.xiaohongshu_title or ""),
            str(rewrite.xiaohongshu_content or ""),
            " ".join(
                tag if str(tag).startswith("#") else f"#{tag}"
                for tag in (rewrite.xiaohongshu_hashtags or [])
            ),
        ]
        body = "\n\n".join(part for part in parts if part.strip())
    if not body:
        body = "（这一条没有这个平台的二创版本）"
    if rewrite.needs_verification:
        # 需核实的信号在热点二创里尤其重要——原帖本身就是没核实过的传言。
        body += "\n\n⚠️ 该内容标记为需人工核实，发布前请先核对事实。"
    if item.url:
        body += f"\n\n原帖链接：{item.url}"
    return header + body


def images_for_hot(item: Any, limit: int, settings: Settings) -> list[str]:
    """热点条目的**原帖图片**（已下载到本地素材库的那些）。"""
    return images_for((item.media or {}).get("images") or [], limit, settings)


#: 从一段文字里找出第一个 http(s) 链接。
#: **不能用 ``startswith`` 判断** —— 实测用户会把链接夹在句子里
#: （"…贡献下我的开智教程 https://xhslink.cn/o/xxx 去【小红书】逛逛…"），
#: 那样 ``startswith`` 为假，抓图分支整个被跳过，于是"只有文案没有图"。
URL_RE = re.compile(r"https?://[^\s，。；、）】\"'<>]+")


def first_url(text: str) -> str:
    match = URL_RE.search(text or "")
    return match.group(0) if match else ""


#: 平台 → 搜索页模板。用于**没有原始链接**的条目兜底。
#: 实测链接覆盖率：小红书 100%、微博 88%、**抖音只有 45%**——抖音榜单很多是关键词条目，
#: 不是具体帖子，所以拿不到 url。只给标题等于没给，用搜索页保证每条都能点。
SEARCH_URL_TEMPLATES: dict[str, str] = {
    "douyin": "https://www.douyin.com/search/{q}",
    "weibo": "https://s.weibo.com/weibo?q={q}",
    "xiaohongshu": "https://www.xiaohongshu.com/search_result?keyword={q}",
}


#: 这些域名的地址**去掉查询串仍然可用**，而它们的查询串是几百字符的跟踪参数。
#: 实测抖音分享链接带 600+ 字符的 ``mid/u_code/did/share_sign/...``——列表里又长又难读，
#: 还白吃掉 QQ 的消息字符额度。
STRIPPABLE_HOSTS = ("iesdouyin.com", "douyin.com", "xiaohongshu.com", "xhslink.cn")


def tidy_link(url: str) -> str:
    """把分享链接的跟踪参数去掉，只留可用的短地址。"""
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = parsed.netloc.lower()
    if any(host.endswith(candidate) for candidate in STRIPPABLE_HOSTS) and parsed.query:
        # 保留必要参数：小红书笔记详情需要 xsec_token 才能打开。
        keep = [
            pair
            for pair in parsed.query.split("&")
            if pair.split("=", 1)[0] in ("xsec_token", "xsec_source")
        ]
        return urlunparse(parsed._replace(query="&".join(keep)))
    return url


def item_link(item: Any) -> tuple[str, bool]:
    """返回 ``(链接, 是否是搜索页)``。

    有原始链接就用原始的；没有就退回该平台的搜索页（**并标注是搜索页**，
    免得用户以为点进去就是那条帖子）。
    """
    from urllib.parse import quote

    if item.url:
        return tidy_link(str(item.url)), False
    template = SEARCH_URL_TEMPLATES.get(str(item.platform))
    title = (item.title or "").strip()
    if template and title:
        return template.format(q=quote(title[:40])), True
    return "", False


#: 选题清单的短期缓存。同一批题目反复要（比如用户看几眼再挑）不该每次都花钱。
_TOPIC_CACHE: dict[str, Any] = {}


def format_hot_list(items: list[Any], note: str, *, fetched: bool) -> str:
    """把热点条目排成带**序号 + 链接 + 一句摘要**的列表。

    **链接是必须的**（用户明确要求）：只给标题等于没给，看到感兴趣的也没法点进去。

    ⚠️ QQ 有 ``40054010 不允许发送URL`` 这个错误码，所以链接能不能发**必须实测**。
    这条注释留在代码里，是为了下次有人动这个格式时知道该验什么。
    """
    header = (
        f"最新热搜 {len(items)} 条（刚抓取）"
        if fetched
        else f"{note}的热点 {len(items)} 条（读已有数据，未重新采集）"
    )
    lines = [header, ""]
    for index, item in enumerate(items, start=1):
        title = (item.title or "(无标题)").strip()
        heat = (
            f" · 热度 {item.hot_value:,}"
            if isinstance(item.hot_value, int) and item.hot_value
            else ""
        )
        lines.append(f"{index}. {title}")
        lines.append(f"   {item.platform}{heat}")
        link, is_search = item_link(item)
        if link:
            # 链接单独一行：和中文挤在一起时客户端常识别不出可点区域。
            lines.append(f"   {link}" + ("　(搜索页)" if is_search else ""))
        # 标题之外再补一句原文摘要——但**和标题重复就跳过**：
        # 抖音的"标题"本身就是整段文案，不判重会出现两行一模一样的内容。
        description = (item.description or "").strip().replace("\n", " ")
        if description and not title.startswith(description[:24]) and description[:24] not in title:
            lines.append(f"   {description[:60]}{'…' if len(description) > 60 else ''}")
    lines.append("")
    lines.append("想看某条的完整图文或二创，可以用「热点小红书」等指令。")
    return "\n".join(lines)


#: 中文数字 → 阿拉伯数字。用于从"生成三张图片"里取张数。
CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def estimate_image_count(text: str) -> int:
    """从用户话里取"生成几张"。

    模型有时会在参数里给出张数、有时给不出，而**张数错了就是真金白银**
    （¥0.25/张），所以本地也解析一遍作为兜底。
    """
    match = re.search(r"(\d+)\s*张", text or "")
    if match:
        return int(match.group(1))
    for word, value in CN_DIGITS.items():
        if f"{word}张" in (text or ""):
            return value
    return 1


def parse_command(content: str) -> str:
    """把 @机器人 的消息内容规整成指令。

    **要去掉开头的前缀符号。** 指令面板里 ``type=command`` 的项，客户端会加一个 ``/``
    前缀，点击后填进输入框的是 ``/科普小红书``。第一版没处理这个，于是**点面板等于没反应**
    （识别不了 → 回帮助文案），用户还得手动把 ``/`` 删掉——那是机器人的问题，不该让人去适应。

    半角/全角都去掉，顺便容忍多个连续前缀（``//帮助``）。
    """
    text = MENTION_RE.sub(" ", content or "")
    # 事件里的 content 常带前导空格与换行。
    text = " ".join(text.split()).strip()
    return text.lstrip("/／!！").strip()


async def latest_article(settings: Settings) -> KnowledgeArticleRecord | None:
    """最新一篇知识科普（免费读库）。"""
    from sqlalchemy import select

    from app.db.database import session_scope

    async with session_scope(settings) as session:
        return (
            await session.execute(
                select(KnowledgeArticleRecord)
                .order_by(KnowledgeArticleRecord.id.desc())
                .limit(1)
            )
        ).scalars().first()


def platform_text(article: KnowledgeArticleRecord, platform: str) -> str:
    """组装某个平台的文案。"""
    drafts = article.platforms or {}
    node = drafts.get(platform)
    header = f"【{PLATFORM_LABELS.get(platform, platform)}】《{article.title}》\n\n"
    if not isinstance(node, dict):
        # **没有这个平台的版本时，退回文章本体。** 只回一句「没有小红书版本」等于什么都没给
        # ——用户等了 74 秒、付了钱，却拿不到可用的内容（实测踩到过）。
        return header + article_fallback_text(article)
    if platform == "douyin":
        parts = [
            f"钩子：{node.get('hook', '')}",
            str(node.get("script") or ""),
            ("分镜：\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(node.get("scenes") or [], 1)))
            if node.get("scenes")
            else "",
            f"字幕：{node.get('subtitles', '')}" if node.get("subtitles") else "",
            f"结尾：{node.get('cta', '')}" if node.get("cta") else "",
        ]
        return header + "\n\n".join(part for part in parts if part.strip())
    hashtags = " ".join(
        tag if str(tag).startswith("#") else f"#{tag}" for tag in (node.get("hashtags") or [])
    )
    body = "\n\n".join(
        part for part in (str(node.get("content") or ""), hashtags) if part.strip()
    )
    return header + body


def article_fallback_text(record: KnowledgeArticleRecord) -> str:
    """平台文案缺失时的退路：把文章正文本身发出去。

    结尾会说明这次没有生成平台版本，便于用户决定要不要重试——而不是让人以为这就是成品。
    """
    lines: list[str] = []
    if record.hook:
        lines.append(record.hook)
    for index, section in enumerate(record.sections or [], start=1):
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "")
        body = str(section.get("body") or "")
        lines.append(f"{index}. {heading}\n{body}".strip())
    if record.glossary:
        terms = "；".join(
            f"{item.get('term', '')}：{item.get('explanation', '')}"
            for item in record.glossary
            if isinstance(item, dict)
        )
        if terms:
            lines.append(f"术语：{terms}")
    if record.takeaways:
        lines.append("要点：\n" + "\n".join(f"· {item}" for item in record.takeaways))
    lines.append(
        "\n（这次的平台文案没生成成功，上面是文章正文。可以再发一次指令重试。）"
    )
    return "\n\n".join(lines)


def images_for(
    images: list[Any], limit: int, settings: Settings
) -> list[str]:
    """挑出真实存在的本地配图。平台链接会过期，所以只发已下载到素材库的。"""
    root = settings.media_root_path.parent
    picked: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        relative = str(image.get("local_path") or "")
        if relative and (root / relative).is_file():
            picked.append(relative)
        if len(picked) >= limit:
            break
    return picked


async def generate_random_article(settings: Settings) -> tuple[dict[str, Any] | None, str]:
    """随机挑一个标签生成文章。返回 ``(article_dict, error)``。"""
    from app.services.knowledge_service import generate_article, list_tags

    tags = await list_tags(settings)
    if not tags:
        return None, "标签词表是空的，请先到管理后台点一次「刷新标签」。"
    tag = random.choice(tags)
    print(f"  随机选中标签：{tag.name}，开始生成（会花钱）…")
    return await _generate(tag.name, settings)


async def _generate(topic: str, settings: Settings) -> tuple[dict[str, Any] | None, str]:
    """按主题生成一篇文章。**会花钱。**"""
    from app.services.knowledge_service import generate_article

    started = time.time()
    result = await generate_article(topic, settings=settings, image_count=6)
    elapsed = time.time() - started
    if not result.ok:
        return None, f"生成失败：{result.error}"
    print(f"  生成完成：{result.article.get('title')}  ¥{result.estimated_cny}  {elapsed:.0f}s")
    return result.article, ""


HELP_TEXT = (
    "【可用指令】\n"
    "—— 我们自己写的科普文章 ——\n"
    "· 知识科普列表 —— 给我 20 个选题（覆盖大学生/数码/科技/计算机/嵌入式，约 ¥0.01）\n"
    "· 关键词：<题目> —— 按这个题目生成文章（计费）；题目可以直接从上面清单里挑\n"
    "· 知识科普 —— 不给题目，随机挑一个方向生成（计费）\n"
    "· 科普小红书 / 科普微博 / 科普抖音 —— 最新一篇科普的对应平台文案（免费）\n"
    "  （直接打「小红书」「微博」「抖音」也一样）\n\n"
    "—— 热点列表里别人的帖子（AI 改写）——\n"
    "· 热点小红书 / 热点微博 / 热点抖音 —— 最新一条热点二创的对应文案（免费）\n\n"
    "—— 随便说句话也行（自动判断你要干什么）——\n"
    "· 贴一段文字或链接 +「改成小红书文案」—— 改写成文案\n"
    "  · 链接是**小红书笔记**会给文案 + 原笔记配图\n"
    "  · 链接是**抖音视频**会先看懂视频再写文案\n"
    "  · 说「把链接里图片识别出来」—— OCR 图上文字再写文案\n"
    "· 「今天中午的新闻」「今天的新闻」—— 读已抓好的（**免费**）\n"
    "· 「获取新的热门新闻」「重新抓一次热搜」—— 真去抓（计费）\n"
    "· 「生成3张图片」—— **文生图，约 ¥0.25/张**，说几张画几张（上限 6 张）\n\n"
    "· 帮助 —— 显示这条\n\n"
    "回复格式：1 条文字 + 配图。生成类指令有冷却与每日上限。"
)


async def handle(
    channel: QQBotChannel, settings: Settings, budget: Budget, message_id: str, command: str, images: int
) -> None:
    """处理一条指令并回复。"""
    print(f"  指令：{command!r}")
    article: dict[str, Any] | None = None
    error = ""
    #: 已经用掉的被动回复序号。**必须跨多次发送累加**：同一 msg_id 的序号空间是共享的，
    #: 「正在生成」的提示用了 1，正式回复若再从 1 开始就会被判重复（实测踩到过：
    #: 文章生成成功、回复却整条失败，日志报 40054005）。
    sequence = 0

    if command in PLATFORM_COMMANDS:
        record = await latest_article(settings)
        if record is None:
            text = "还没有任何科普文章。先发「知识科普」或「关键词：mysql」生成一篇。"
        else:
            text = platform_text(record, PLATFORM_COMMANDS[command])
            article = {"images": record.images or [], "title": record.title}
    elif command in HOT_PLATFORM_COMMANDS:
        # 从**热点列表**取（别人的帖子被改写），与上面的科普文章是两套内容。
        pair = await latest_hot_rewrite(settings)
        if pair is None:
            text = "热点列表里还没有已二创的内容。等定时采集跑一轮，或到后台手动执行一次。"
        else:
            rewrite, item = pair
            text = hot_platform_text(rewrite, item, HOT_PLATFORM_COMMANDS[command])
            article = {"images": (item.media or {}).get("images") or [], "title": item.title}
    else:
        # 「关键词：xxx」指定主题生成；「知识科普」随机挑一个方向。
        keyword_match = KEYWORD_RE.match(command)
        wants_generation = bool(keyword_match) or command in {"知识科普", "科普", "随机"}
        if not wants_generation and command.strip() in {"关键词", "主题", "keyword"}:
            # 指令面板里那一项填的是「关键词：mysql」，用户可能只打「关键词」。
            # 给一条明确的示例，比丢一份通用帮助有用。
            text = (
                "要指定主题的话，请带上冒号和主题，例如：\n"
                "  关键词：mysql\n"
                "  关键词：Redis 持久化\n"
                "  关键词：算法面试\n\n"
                "不带主题就用「知识科普」，我会随机挑一个方向。"
            )
        elif not wants_generation:
            # **兜底：用 function calling 判断用户想干什么。**
            # 走到这里说明他没按指令格式说话——比如直接贴一段文字或链接说
            # "帮我改成小红书文案"。回一份帮助文案是最没用的反应；让模型选个工具才对。
            # 路由本身约 ¥0.0025，且只有"认不出的 @ 消息"才走这条，不会给每句话都加钱。
            if await route_and_execute(
                command,
                channel=channel,
                settings=settings,
                budget=budget,
                message_id=message_id,
                images=images,
            ):
                return  # 路由已自行回复，不再走下面的通用发送
            text = HELP_TEXT
        else:
            topic = (keyword_match.group(1).strip() if keyword_match else "")
            refusal = budget.check()
            if not topic:
                refusal = refusal  # 随机模式，沿用同一套闸门
            if refusal:
                text = refusal
            else:
                budget.consume()
                label = f"「{topic}」" if topic else "一个随机的方向"
                print(f"  开始生成{label}（会花钱）…")
                notice = await channel.send_rich(
                    "生成中",
                    f"收到，正在为{label}生成文章与配图，约 40-90 秒，稍等。",
                    [],
                    passive_id=message_id,
                )
                sequence += int(notice.detail.get("text_parts") or 0) + int(
                    notice.detail.get("images_sent") or 0
                )
                if topic:
                    generated, error = await _generate(topic, settings)
                else:
                    generated, error = await generate_random_article(settings)
                if generated is None:
                    text = error
                else:
                    article = generated
                    drafts = generated.get("platforms") or {}
                    node = drafts.get("xiaohongshu") if isinstance(drafts, dict) else None
                    if isinstance(node, dict):
                        text = (
                            f"已生成《{generated.get('title')}》\n\n"
                            + str(node.get("content") or "")
                        )
                        hashtags = " ".join(
                            tag if str(tag).startswith("#") else f"#{tag}"
                            for tag in (node.get("hashtags") or [])
                        )
                        if hashtags:
                            text += "\n\n" + hashtags
                    else:
                        # 平台文案没生成出来时**退回文章正文**。只回一句「没有小红书版本」
                        # 等于让用户白等 74 秒并付了钱却拿不到东西（实测踩到过）。
                        text = (
                            f"已生成《{generated.get('title')}》\n\n"
                            + article_fallback_text(_ArticleView(generated))
                        )
                    text += f"\n\n本次消耗约 ¥{generated.get('estimated_cny')}"

                # **文章写完后，再用文生图画 2 张配图**（用户明确要求）。
                # 为什么用文生图而不是文章自带的搜图：搜图是别人笔记的封面，
                # 常常和内容只是"同名不同域"；生成的图才真跟这篇文章对得上。
                # 代价是约 ¥0.25/张、每张约 70-90 秒。
                if generated is not None:
                    await _attach_generated_images(
                        channel, settings, text, count=2, label=label
                    )
                    return  # 图片与文案都已由上面发完

    image_paths: list[str] = []
    if article:
        image_paths = images_for(article.get("images") or [], images, settings)

    outcome = await channel.send_rich(
        "回复",
        text,
        image_paths,
        passive_id=message_id,
        start_seq=sequence,
        keyboard=COMMAND_KEYBOARD,
    )
    sent_text = int(outcome.detail.get("text_parts") or 0)
    sent_images = int(outcome.detail.get("images_sent") or 0)
    print(
        f"  回复：ok={outcome.ok} 文字={sent_text} 图片={sent_images}"
        + (f" 跳过={outcome.detail.get('skipped')}" if outcome.detail.get("skipped") else "")
    )
    if not outcome.ok:
        print(f"  错误：{outcome.error}")


#: 时间段 → (起, 止) 小时。用于「今天中午的新闻」这类**读已有数据**的请求。
#: 边界取整点，宁宽勿窄——漏掉几条比多列几条更让人困惑。
PERIOD_HOURS: dict[str, tuple[int, int]] = {
    "morning": (5, 11),
    "noon": (11, 15),
    "evening": (17, 24),
}
PERIOD_LABELS = {
    "today": "今天",
    "morning": "今天早上",
    "noon": "今天中午",
    "evening": "今天晚上",
    "recent": "最近几小时",
    "all": "库里最新的",
}


async def hot_items_for_period(
    settings: Settings, period: str, count: int
) -> tuple[list[Any], str]:
    """按时间段读**已有**的热点条目。返回 ``(条目, 说明)``。**不花钱。**

    为什么必须有这个函数：用户说「今天中午的最新消息」时想看的是**中午那一轮已经抓好的**
    内容，而不是再花一次采集费。实测原先把这类说法路由去 ``fetch_hot_news``——
    也就是**每次问"中午的新闻"都会白花一次 TikHub 费用**。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db.database import session_scope
    from app.models.hot_content import HotContentRecord

    now = datetime.now()  # 本地时间：用户说的"中午"是他自己的钟点
    since: datetime | None = None
    until: datetime | None = None
    note = PERIOD_LABELS.get(period, period)

    if period in PERIOD_HOURS:
        start, end = PERIOD_HOURS[period]
        since = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=start)
        until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end)
        if since > now:
            # 那个时段今天还没到（比如早上 8 点问"晚上的新闻"），退回最近的。
            since, until, note = None, None, f"{note}（今天还没到，下面是库里最新的）"
    elif period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "recent":
        since = now - timedelta(hours=6)

    async with session_scope(settings) as session:
        statement = select(HotContentRecord)
        if since is not None:
            # created_at 存的是 UTC，本地时间要减掉偏移再比。
            statement = statement.where(
                HotContentRecord.created_at >= since.astimezone(timezone.utc).replace(tzinfo=None)
            )
        if until is not None:
            statement = statement.where(
                HotContentRecord.created_at < until.astimezone(timezone.utc).replace(tzinfo=None)
            )
        statement = statement.order_by(HotContentRecord.id.desc()).limit(max(1, count))
        rows = (await session.execute(statement)).scalars().all()
    return list(rows), note


async def newest_hot_items(settings: Settings, count: int) -> list[Any]:
    """库里**最近入库**的热点条目。

    按 ``id`` 倒序而不是按热度——用户说"新的热门新闻"，要的是**刚抓到的**，
    不是热度最高的老条目。这也是 ``fetch_hot_news`` 与 ``send_latest`` 的区别。
    """
    from sqlalchemy import select

    from app.db.database import session_scope
    from app.models.hot_content import HotContentRecord

    async with session_scope(settings) as session:
        rows = (
            await session.execute(
                select(HotContentRecord)
                .order_by(HotContentRecord.id.desc())
                .limit(max(1, count))
            )
        ).scalars().all()
    return list(rows)


async def route_and_execute(
    message: str,
    *,
    channel: QQBotChannel,
    settings: Settings,
    budget: Budget,
    message_id: str,
    images: int,
) -> bool:
    """不按指令格式说话时，用 function calling 判断意图并执行。**自己回复。**

    返回 ``True`` 表示已经处理并回复过，调用方不要再发通用帮助。

    设计要点：
    * **模型只选工具、不写文案** —— 文案交给既有管线，质量与成本仍在原来的地方受控；
    * **计费工具复用同一道额度闸门**，模型无权绕过；
    * 路由失败或模型选不出工具时**返回 False**，让调用方回帮助文案——
      硬猜比不猜更糟（猜错会做错事、还花了钱）。
    """
    from app.services.ai.agent import (
        ToolCall,
        convert_text_to_note,
        route_intent,
    )

    intent = await route_intent(message, settings=settings)
    if intent.error:
        # 路由本身失败（网络/额度）时**照实说**，不要静默回帮助文案——
        # 那会让人以为"机器人没听懂"，而其实是调用挂了。
        print(f"  意图识别失败：{intent.error}")
        await channel.send_rich(
            "回复",
            f"我没能判断你的意思（{intent.error[:80]}）。可以先发「帮助」看指令。",
            [],
            passive_id=message_id,
            keyboard=COMMAND_KEYBOARD,
        )
        return True

    if intent.call is None:
        # **贴了链接就是足够强的信号，不该因为模型犹豫就回帮助文案。**
        # 实测：用户发「标题 + 抖音链接 + 帮我整理成小红书文案」，路由没选出工具，
        # 于是收到一份帮助文案——用户完全不知道发生了什么。
        # 链接 + 任何请求，最合理的默认就是"处理这个链接"。
        link = first_url(message)
        if link:
            # 链接 + 提到"图" → 生图（¥0.25/张）；否则按"改写链接"处理。
            # 这两种意图用户都会用"图"字以外的说法，所以只做最明显的区分。
            wants_image = any(
                word in message for word in ("生成", "画", "配图", "图片", "封面图")
            )
            if wants_image:
                print(f"  路由没选工具，但消息里有链接且提到图片，按「文生图」处理")
                call = ToolCall(
                    name="generate_images",
                    arguments={"subject": message, "count": estimate_image_count(message)},
                )
            else:
                print(f"  路由没选工具，但消息里有链接，默认按「改写链接」处理：{link[:60]}")
                call = ToolCall(name="convert_text_to_note", arguments={"text": message})
        else:
            # 模型认为没有合适的工具。它常会写一句得体的解释或反问，直接用那句话。
            reply = intent.message or HELP_TEXT
            print(f"  没选工具，回原话（花费 ¥{intent.usage_cny:.4f}）")
            await channel.send_rich(
                "回复", reply, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
            )
            return True
    else:
        call = intent.call

    print(f"  识别为工具：{call.name}  参数={list(call.arguments)}  路由花费 ¥{intent.usage_cny:.4f}")

    if call.name == "show_help":
        await channel.send_rich(
            "回复", HELP_TEXT, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
        )
        return True

    if call.name == "send_latest":
        platform = str(call.arguments.get("platform") or "xiaohongshu")
        source = str(call.arguments.get("source") or "knowledge")
        if source == "hot":
            pair = await latest_hot_rewrite(settings)
            if pair is None:
                text = "热点列表里还没有已二创的内容。"
                payload = []
            else:
                rewrite, item = pair
                text = hot_platform_text(rewrite, item, platform)
                payload = (item.media or {}).get("images") or []
        else:
            record = await latest_article(settings)
            if record is None:
                text = "还没有任何科普文章。"
                payload = []
            else:
                text = platform_text(record, platform)
                payload = record.images or []
        await channel.send_rich(
            "回复",
            text,
            images_for(payload, images, settings),
            passive_id=message_id,
            keyboard=COMMAND_KEYBOARD,
        )
        return True

    if call.name == "list_knowledge_topics":
        # 缓存 10 分钟：同一个话题反复要清单不该每次都花钱。
        cached = _TOPIC_CACHE.get("topics")
        age = time.time() - float(_TOPIC_CACHE.get("at") or 0)
        if cached and age < 600:
            topics = list(cached)
            print(f"  用缓存的选题清单（{int(age)}s 前生成）")
        else:
            refusal = budget.check()
            if refusal:
                await channel.send_rich("回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
                return True
            budget.consume()
            from app.services.ai.agent import generate_topic_list

            topics = await generate_topic_list(settings=settings)
            if topics:
                _TOPIC_CACHE["topics"] = topics
                _TOPIC_CACHE["at"] = time.time()
                print(f"  生成了 {len(topics)} 个选题")
        if not topics:
            text = "没能生成选题清单，稍后再试一次。"
        else:
            lines = [
                f"科普选题 {len(topics)} 个（挑一个发我「关键词：<题目>」即可写成文章）",
                "",
            ]
            for index, topic in enumerate(topics, start=1):
                lines.append(f"{index}. {topic}")
            lines.append("")
            lines.append("示例：关键词：" + topics[0])
            text = "\n".join(lines)
        await channel.send_rich(
            "回复", text, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
        )
        return True

    if call.name == "generate_knowledge_article":
        topic = str(call.arguments.get("topic") or "").strip()
        if not topic:
            await channel.send_rich(
                "回复", "你想让我写什么主题？例如「关键词：mysql 索引」。", [],
                passive_id=message_id, keyboard=COMMAND_KEYBOARD,
            )
            return True
        refusal = budget.check()
        if refusal:
            await channel.send_rich(
                "回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
            )
            return True
        budget.consume()
        notice = await channel.send_rich(
            "生成中", f"收到，正在为「{topic}」生成文章与配图，约 40-90 秒。", [],
            passive_id=message_id,
        )
        sequence = int(notice.detail.get("text_parts") or 0) + int(
            notice.detail.get("images_sent") or 0
        )
        generated, error = await _generate(topic, settings)
        if generated is None:
            text = error
            payload = []
        else:
            drafts = generated.get("platforms") or {}
            node = drafts.get("xiaohongshu") if isinstance(drafts, dict) else None
            if isinstance(node, dict):
                text = f"已生成《{generated.get('title')}》\n\n{node.get('content') or ''}"
            else:
                text = (
                    f"已生成《{generated.get('title')}》\n\n"
                    + article_fallback_text(_ArticleView(generated))
                )
            text += f"\n\n本次消耗约 ¥{generated.get('estimated_cny')}"
            payload = generated.get("images") or []
        await channel.send_rich(
            "回复",
            text,
            images_for(payload, images, settings),
            passive_id=message_id,
            start_seq=sequence,
            keyboard=COMMAND_KEYBOARD,
        )
        return True

    if call.name == "ocr_link_images":
        url = str(call.arguments.get("url") or "").strip()
        platform = str(call.arguments.get("platform") or "xiaohongshu")
        if not url:
            await channel.send_rich("回复", "你要我识别哪个链接？", [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
            return True
        refusal = budget.check()
        if refusal:
            await channel.send_rich("回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
            return True
        budget.consume()
        label = {"xiaohongshu": "小红书", "weibo": "微博", "douyin": "抖音"}.get(platform, platform)
        notice = await channel.send_rich(
            "识别中",
            f"收到。正在取原笔记配图并做 OCR，然后据此写{label}文案，约 30-60 秒。",
            [],
            passive_id=message_id,
        )
        sequence = int(notice.detail.get("text_parts") or 0) + int(
            notice.detail.get("images_sent") or 0
        )

        from app.services.ai.vision import ocr_images
        from app.services.tikhub.note_link import fetch_xhs_note_images, is_xiaohongshu_link

        images_ocr: list[str] = []
        note_error = ""
        # **识别要全部，发送才受限额。** 这两个数不能混：
        # ``--images`` 是"每条回复最多发几张"（受 QQ 被动回复条数限制），
        # 而 OCR 应当覆盖链接里所有图。第一版把两者都用 ``images``，
        # 结果一条 7 图的笔记只识别了 1-2 张（而且那 2 张还是同图不同尺寸）。
        ocr_want = max(images, int(getattr(settings, "ocr_max_images", 9)))
        if is_xiaohongshu_link(url):
            images_ocr, note_report = await fetch_xhs_note_images(
                url, settings=settings, limit=ocr_want
            )
            note_error = str(note_report.get("error") or "")
        else:
            from app.services.ai.page_images import fetch_page_images

            grabbed = await fetch_page_images(url, settings=settings, limit=ocr_want)
            images_ocr, note_error = grabbed.paths, grabbed.error

        if not images_ocr:
            await channel.send_rich(
                "回复",
                f"没拿到可识别的图片（{note_error or '链接里没有图'}）。",
                [], passive_id=message_id, start_seq=sequence, keyboard=COMMAND_KEYBOARD,
            )
            return True

        # **先拼成一张网格图。** 好处有两个：OCR 一次看全（便于确认读了几张），
        # 而且只占 1 条 QQ 消息——被动回复一次只有 3-4 条额度，7 张图根本发不完。
        # 拼图失败或开关关掉时退回逐张 OCR 的老路。
        stitch_error = ""
        ocr_inputs = images_ocr
        stitched_path = ""
        if bool(getattr(settings, "ocr_stitch_images", True)) and len(images_ocr) > 1:
            from app.services.ai.image_stitch import stitch_images

            stitched = stitch_images(images_ocr, settings=settings)
            if stitched.ok:
                stitched_path = stitched.path
                ocr_inputs = [stitched.path]
                print(f"  已拼接 {stitched.count} 张为 {stitched.width}×{stitched.height}")
            else:
                stitch_error = stitched.error
                print(f"  拼图失败({stitched.error})，改为逐张 OCR")

        vision = await ocr_images(ocr_inputs, settings=settings)
        if not vision.ok:
            await channel.send_rich(
                "回复",
                f"OCR 失败：{vision.error[:120]}",
                [], passive_id=message_id, start_seq=sequence, keyboard=COMMAND_KEYBOARD,
            )
            return True

        print(f"  OCR 完成：{len(vision.text)} 字，tokens={vision.prompt_tokens}+{vision.completion_tokens}")
        # **用识别出来的文字生成文案** —— 这才是"识别图片并生成新文案"的完整含义。
        conversion = await convert_text_to_note(
            f"（以下内容来自图片 OCR 识别）\n\n{vision.text}", platform=platform, settings=settings
        )
        if conversion.ok:
            recognized = len(images_ocr)
            how = "已拼成 1 张网格图识别" if stitched_path else f"逐张识别 {recognized} 张"
            text = (
                f"【{label}文案】\n\n{conversion.text}\n\n"
                f"（{how}，OCR {vision.prompt_tokens + vision.completion_tokens} tokens）"
                f"本次消耗约 ¥{conversion.usage_cny:.3f}"
            )
            if vision.error:
                # OCR 被截断时**必须说出来**，否则用户只会觉得"怎么少了内容"。
                text += f"\n⚠️ {vision.error}"
            elif stitch_error:
                text += f"\n（拼图失败：{stitch_error[:60]}）"
        else:
            text = f"OCR 成功但改写失败：{conversion.error}\n\n识别到的文字：\n{vision.text[:400]}"
        await channel.send_rich(
            "回复", text, [], passive_id=message_id, start_seq=sequence,
            keyboard=COMMAND_KEYBOARD,
        )
        # **图片用主动消息发。** 被动回复一次 @ 只有 3-4 条额度，7 张图发不完；
        # 主动消息的限制是频控（单群 20/qpm、每天 1000 条），够用。
        # 拼图成功就只发那 1 张网格图（1 条消息搞定），否则发全部原图。
        # 文案已经由被动回复送达，所以即使这里失败，用户也不会一无所获。
        outgoing = [stitched_path] if stitched_path else images_ocr
        if outgoing:
            print(f"  用主动消息补发 {len(outgoing)} 张图…")
            sent = await channel.send_rich("", "", outgoing, images_only=True)
            if not sent.ok:
                print(f"  主动发图失败：{sent.error}")
        return True

    if call.name == "understand_link_video":
        url = str(call.arguments.get("url") or "").strip()
        if not url:
            await channel.send_rich("回复", "你要我识别哪个链接里的视频？", [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
            return True
        refusal = budget.check()
        if refusal:
            await channel.send_rich("回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
            return True
        budget.consume()
        notice = await channel.send_rich(
            "识别中", "收到，正在取视频并识别，可能要点时间（视频越长越慢）。", [],
            passive_id=message_id,
        )
        sequence = int(notice.detail.get("text_parts") or 0) + int(
            notice.detail.get("images_sent") or 0
        )

        from app.services.ai.vision import understand_video
        from app.services.tikhub.note_link import collect_video_urls, fetch_note_payload, is_xiaohongshu_link

        if not is_xiaohongshu_link(url):
            text = "视频识别目前只支持小红书笔记链接。"
        else:
            payload, error = await fetch_note_payload(url, settings=settings)
            videos = collect_video_urls(payload) if payload else []
            if not videos:
                # **如实说明**：可能是图文笔记，也可能返回结构里没有可公开访问的视频地址。
                text = (
                    f"这条笔记里没找到可用的视频地址（{error or '可能是图文笔记'}）。"
                    "如果它确实是视频笔记，那是笔记详情里没给出可公开访问的地址。"
                )
            else:
                vision = await understand_video(videos[0], settings=settings)
                if vision.ok:
                    text = (
                        f"【视频识别结果】\n\n{vision.text}\n\n"
                        f"（{vision.model}，{vision.prompt_tokens + vision.completion_tokens} tokens）"
                    )
                else:
                    text = f"视频识别失败：{vision.error[:150]}"
        await channel.send_rich(
            "回复", text, [], passive_id=message_id, start_seq=sequence, keyboard=COMMAND_KEYBOARD
        )
        return True

    if call.name == "read_hot_news":
        # **不花钱**：只读库里已抓好的。用户说「今天中午的新闻」走上这条，
        # 而不是重新采集——否则每问一次就白花一次 TikHub 费用。
        try:
            count = int(call.arguments.get("count") or 8)
        except (TypeError, ValueError):
            count = 8
        count = max(1, min(15, count))
        period = str(call.arguments.get("period") or "all")
        items, note = await hot_items_for_period(settings, period, count)
        if not items:
            text = (
                f"{note}没有已抓取的条目。"
                "如果想现在去抓，发「重新抓一次最新热搜」（会产生采集费用）。"
            )
        else:
            text = format_hot_list(items, note, fetched=False)
        await channel.send_rich(
            "回复", text, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
        )
        return True

    if call.name == "fetch_hot_news":
        try:
            count = int(call.arguments.get("count") or 8)
        except (TypeError, ValueError):
            count = 8
        count = max(1, min(15, count))
        refusal = budget.check()
        if refusal:
            await channel.send_rich("回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
            return True
        budget.consume()
        notice = await channel.send_rich(
            "采集",
            "收到，正在抓取最新热搜（微博/抖音/小红书），约 20-40 秒。",
            [],
            passive_id=message_id,
        )
        sequence = int(notice.detail.get("text_parts") or 0) + int(
            notice.detail.get("images_sent") or 0
        )

        from app.services.pipeline.runner import run_pipeline

        print("  触发一次采集（花钱）…")
        run = await run_pipeline(stages="fetch", settings=settings, trigger="qq")
        fetch_step = (run.steps or [{}])[0] if run.steps else {}
        detail = getattr(fetch_step, "detail", None) or {}
        error = getattr(fetch_step, "error", "") or detail.get("error") or ""

        # **只报新增的。** ``inserted`` 是本轮真正新入库的条数；库里已有的老条目
        # 再列一遍没有意义（用户要的是"新的"）。
        items = await newest_hot_items(settings, count)
        if not items:
            text = (
                "这一轮没有抓到新的条目。"
                + (f"（{error[:100]}）" if error else "可能是各平台榜单暂时没更新。")
            )
        else:
            text = format_hot_list(items, "", fetched=True)
        await channel.send_rich(
            "回复", text, [], passive_id=message_id, start_seq=sequence,
            keyboard=COMMAND_KEYBOARD,
        )
        return True

    if call.name == "generate_images":
        subject = str(call.arguments.get("subject") or "").strip()
        try:
            count = int(call.arguments.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        # **上限 6 张**：¥0.25/张，6 张就是 ¥1.5。模型偶尔会把"3张"听错，
        # 硬上限比事后追钱容易。
        count = max(1, min(6, count))
        if not subject:
            await channel.send_rich(
                "回复", "你想让我画什么？可以给个链接或一段描述。", [],
                passive_id=message_id, keyboard=COMMAND_KEYBOARD,
            )
            return True
        refusal = budget.check()
        if refusal:
            await channel.send_rich("回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD)
            return True
        budget.consume()

        notice = await channel.send_rich(
            "生成图片",
            f"收到，准备生成 {count} 张图（约 ¥{0.25 * count:.2f}，每张约 70 秒）。"
            "先理解内容，再逐张生成，请稍等。",
            [],
            passive_id=message_id,
        )
        sequence = int(notice.detail.get("text_parts") or 0) + int(
            notice.detail.get("images_sent") or 0
        )

        # **先生成小红书文案，再用文案去生图。**
        # 之前直接拿视频理解结果当素材，模型容易给不出提示词（实测退回了兜底提示词，
        # 画出来的图和内容基本无关）。而且用文案当素材更合理：**图要和你要发的帖子对得上**。
        material = subject
        link = first_url(subject)
        if link or len(subject) > 20:
            conversion = await convert_text_to_note(subject, platform="xiaohongshu", settings=settings)
            if conversion.ok:
                material = conversion.text
                print(f"  已生成文案（{len(material)} 字），据此写生图提示词")
            else:
                print(f"  文案生成失败（{conversion.error[:60]}），退回用原始素材")

        from app.services.ai.agent import image_prompt_from
        from app.services.ai.image_gen import QwenImageClient, download_generated

        prompt = await image_prompt_from(material, settings=settings)
        print(f"  生图提示词：{prompt[:80]}")

        client = QwenImageClient(settings)
        saved: list[str] = []
        failures: list[str] = []
        try:
            for index in range(1, count + 1):
                result = await client.edit_image(prompt=prompt, reference_paths=[])
                if not result.ok:
                    failures.append(result.error or "未知错误")
                    continue
                paths, errors = await download_generated(result.urls, settings=settings)
                saved.extend(paths)
                failures.extend(errors)
                print(f"  第 {index}/{count} 张完成（{result.elapsed_ms / 1000:.0f}s）")
                # **每生成完一张立刻发。** 之前是等全部生成完再一起发，2 张就要 4 分钟，
                # 逼近被动回复的 5 分钟窗口；而且用户在这几分钟里看不到任何进展。
                # 图片走**主动消息**（不带 msg_id），不受那个窗口限制。
                if paths:
                    sent = await channel.send_rich("", "", paths, images_only=True)
                    if not sent.ok:
                        print(f"  立即发送失败：{sent.error}")
        finally:
            await client.aclose()

        text = (
            f"画好了 {len(saved)}/{count} 张，约 ¥{0.25 * count:.2f}。\n"
            f"文案：{material[:60]}…\n提示词：{prompt[:80]}"
            if saved
            else f"没能生成图片：{failures[0][:120] if failures else '未知原因'}"
        )
        # 收尾文字也走**主动消息**：这个指令本来就慢（可能 4 分钟以上），
        # 用被动回复会撞 5 分钟窗口（错误码 40034128）。
        await channel.send_rich("回复", text, [], keyboard=COMMAND_KEYBOARD)
        return True

    if call.name == "convert_text_to_note":
        source = str(call.arguments.get("text") or "").strip()
        platform = str(call.arguments.get("platform") or "xiaohongshu")
        if not source:
            await channel.send_rich(
                "回复", "你要我改写哪段内容？把文字或链接发给我就行。", [],
                passive_id=message_id, keyboard=COMMAND_KEYBOARD,
            )
            return True
        refusal = budget.check()
        if refusal:
            await channel.send_rich(
                "回复", refusal, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
            )
            return True
        budget.consume()
        label = {"xiaohongshu": "小红书", "weibo": "微博", "douyin": "抖音"}.get(platform, platform)
        notice = await channel.send_rich(
            "改写中", f"收到，正在把这段内容改写成{label}文案，约 20-40 秒。", [],
            passive_id=message_id,
        )
        sequence = int(notice.detail.get("text_parts") or 0) + int(
            notice.detail.get("images_sent") or 0
        )
        conversion = await convert_text_to_note(source, platform=platform, settings=settings)
        if conversion.ok:
            text = f"【{label}文案】\n\n{conversion.text}\n\n本次消耗约 ¥{conversion.usage_cny:.3f}"
        else:
            text = f"改写失败：{conversion.error}"

        # **如果是链接，顺手把原页面的配图也发过去。** 用户要的是"图文"，只有文案不够。
        # 抓图失败不影响文案——分开报，不因为图挂了就把已经生成的文案丢掉。
        #
        # 小红书链接要走 TikHub：它的分享短链会重定向到登录页，匿名抓取拿不到图
        # （实测 HTML 里 <img> 数量为 0）。其他网页走普通抓取。**付费动作**。
        page_images: list[str] = []
        # **优先用链接流程已经取到的图。** 抖音图文那一步已经下载好并拼成一张网格图了，
        # 这里若再去"抓网页"只会失败——实测用户就看到"原页面配图没抓到"，
        # 而图其实就在本地素材库里。
        if conversion.media_paths:
            page_images = conversion.media_paths
            text += f"\n\n（附原笔记配图 {len(page_images)} 张）"
        link = first_url(source)
        if link and not page_images:
            from app.services.tikhub.note_link import fetch_xhs_note_images, is_xiaohongshu_link

            if is_xiaohongshu_link(link):
                grabbed_images, note_report = await fetch_xhs_note_images(
                    link, settings=settings, limit=images
                )
                page_images = grabbed_images
                if page_images:
                    text += f"\n\n（附原笔记配图 {len(page_images)} 张，取自 TikHub）"
                else:
                    text += f"\n\n（原笔记配图没抓到：{note_report.get('error') or '未知'}）"
            else:
                from app.services.ai.page_images import fetch_page_images

                grabbed = await fetch_page_images(link, settings=settings, limit=images)
                page_images = grabbed.paths
                if page_images:
                    text += f"\n\n（附原页面配图 {len(page_images)} 张）"
                elif grabbed.error:
                    text += f"\n\n（原页面配图没抓到：{grabbed.error[:60]}）"

        await channel.send_rich(
            "回复", text, page_images, passive_id=message_id, start_seq=sequence,
            keyboard=COMMAND_KEYBOARD,
        )
        return True

    # 模型选了一个我们知道但这里没实现的工具——如实说明，不要假装什么都没发生。
    print(f"  工具 {call.name} 尚未接入")
    await channel.send_rich(
        "回复", f"我理解你想做「{call.name}」，但这个能力还没接上。", [],
        passive_id=message_id, keyboard=COMMAND_KEYBOARD,
    )
    return True


#: 这些异常说明**基础设施**（数据库/后端）不可用，而不是用户说错了什么。
INFRA_ERRORS = (ConnectionRefusedError, ConnectionResetError, TimeoutError, OSError)


async def _report_failure(settings: Settings, message_id: str, exc: Exception) -> None:
    """把失败原因如实回给用户。**沉默是最糟的反应。**

    实测：数据库挂掉时机器人只把异常写进日志、对用户一声不吭，
    用户连 @ 了几次都不知道发生了什么，只会以为"机器人死了"。
    """
    kind = type(exc).__name__
    if isinstance(exc, INFRA_ERRORS):
        text = (
            "我这边连不上后端/数据库，这条指令没能执行完。\n"
            "**不是你的指令有问题** —— 稍等一会儿再试，或发「帮助」看看我能做什么。\n"
            f"（{kind}）"
        )
    else:
        text = (
            "这条指令执行时出了错，我没能完成。\n"
            f"（{kind}: {str(exc)[:80]}）"
        )
    try:
        await QQBotChannel(settings).send_rich(
            "回复", text, [], passive_id=message_id, keyboard=COMMAND_KEYBOARD
        )
    except Exception as inner:  # noqa: BLE001 - 连说明都发不出去时只能记日志
        print(f"  连故障说明都发不出去：{type(inner).__name__}: {inner}")


def _db_host_port(database_url: str) -> tuple[str, int]:
    """从 ``database_url`` 解析出主机与端口。Settings 没有单独暴露这两个字段。"""
    from urllib.parse import urlparse

    parsed = urlparse(database_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 5432


async def _health_loop(settings: Settings, interval: float = 300.0) -> None:
    """每 5 分钟探一次数据库与后端，**只在状态变化时**告警。

    只在"变了"的时候发：一直发"还是坏的"会变成噪音，用户会屏蔽掉。
    """
    import httpx

    previous: bool | None = None
    while True:
        await asyncio.sleep(interval)
        problems: list[str] = []
        host, port = _db_host_port(settings.database_url)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            problems.append(f"数据库 {host}:{port}（{type(exc).__name__}）")
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                response = await client.get("http://127.0.0.1:8000/api/system/stats")
            if response.status_code >= 500:
                problems.append(f"后端返回 {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"后端 8000（{type(exc).__name__}）")

        healthy = not problems
        if previous is None:
            print(f"  健康检查：{'正常' if healthy else '异常 —— ' + '；'.join(problems)}")
        elif healthy != previous:
            if healthy:
                await _send_alert(settings, "服务已恢复", "数据库和后端都能连上了，指令可以正常使用。")
            else:
                await _send_alert(
                    settings,
                    "服务异常",
                    "我这边连不上：" + "；".join(problems) + "\n"
                    "这段时间里需要读数据的指令会失败（「帮助」仍可用）。",
                )
        previous = healthy


async def _send_alert(settings: Settings, title: str, body: str) -> None:
    """主动往群里发告警。不占被动回复额度。"""
    try:
        await QQBotChannel(settings).send(f"[{title}]", body)
        print(f"  已发告警：{title}")
    except Exception as exc:  # noqa: BLE001
        print(f"  告警发送失败：{type(exc).__name__}: {exc}")


async def _attach_generated_images(
    channel: QQBotChannel,
    settings: Settings,
    text: str,
    *,
    count: int = 2,
    label: str = "",
) -> None:
    """先发文案，再用**文生图**生成 ``count`` 张配图并逐张发出。

    为什么文案先发：文章生成（40-90s）+ 2 张图（每张 70-90s）合计可能 4-5 分钟，
    **逼近被动回复的 5 分钟窗口**。先把文案用主动消息发出去（不受窗口限制），
    用户立刻拿到主要内容，图随后补上。

    图也走主动消息，且**生成一张发一张**——这样第一张不至于等第二张。
    """
    from app.services.ai.agent import image_prompt_from
    from app.services.ai.image_gen import QwenImageClient, download_generated

    # 文案先走主动消息。
    await channel.send_rich("回复", text, [], keyboard=COMMAND_KEYBOARD)

    prompt = await image_prompt_from(text, settings=settings)
    print(f"  生图提示词：{prompt[:70]}")
    client = QwenImageClient(settings)
    saved = 0
    try:
        for index in range(1, count + 1):
            result = await client.edit_image(prompt=prompt, reference_paths=[])
            if not result.ok:
                print(f"  第 {index}/{count} 张失败：{result.error}")
                continue
            paths, errors = await download_generated(result.urls, settings=settings)
            for error in errors:
                print(f"  下载失败：{error}")
            if paths:
                saved += len(paths)
                print(f"  第 {index}/{count} 张完成（{result.elapsed_ms / 1000:.0f}s）")
                sent = await channel.send_rich("", "", paths, images_only=True)
                if not sent.ok:
                    print(f"  发图失败：{sent.error}")
    finally:
        await client.aclose()
    if saved:
        await channel.send_rich(
            "回复",
            f"{label}配图 {saved} 张已发出（文生图，约 ¥{0.25 * saved:.2f}）。",
            [],
            keyboard=COMMAND_KEYBOARD,
        )


async def _heartbeat(websocket: Any, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await websocket.send(json.dumps({"op": OP_HEARTBEAT, "d": None}))
        except Exception:  # noqa: BLE001 - 主循环会看到连接已关闭
            return


async def _access_token(settings: Settings) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={"appId": settings.qq_app_id, "clientSecret": settings.qq_app_secret},
        )
        body = response.json()
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError(f"无法获取 access token：{str(body)[:200]}")
    return str(token)


async def _session(
    websockets: Any,
    url: str,
    token: str,
    settings: Settings,
    budget: Budget,
    images: int,
) -> None:
    """一次网关会话：连接、鉴权、收事件、按指令回复。"""
    async with websockets.connect(
        url,
        additional_headers=HEADERS,
        ping_interval=20,
        # **必须显式禁用代理。** ``websockets`` 通过 ``urllib.getproxies()`` 读 Windows
        # 注册表的系统代理（实测拿到 ``https://127.0.0.1:7890``），于是 WebSocket 会绕到
        # 本机 Clash 上；那条链路会让机器人"显示已就绪却收不到任何事件"。
        # 项目里其他地方一律 ``trust_env=False``，这里同理——直连。
        proxy=None,
    ) as websocket:
        hello = json.loads(await websocket.recv())
        interval = float(hello.get("d", {}).get("heartbeat_interval") or 40000) / 1000
        await websocket.send(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": INTENT_GROUP_AND_C2C,
                        "shard": [0, 1],
                        "properties": {
                            "$os": "windows",
                            "$browser": "socialhot",
                            "$device": "socialhot",
                        },
                    },
                }
            )
        )
        heartbeat = asyncio.create_task(_heartbeat(websocket, interval))
        try:
            while True:
                event = json.loads(await websocket.recv())
                if event.get("op") != OP_DISPATCH:
                    continue
                kind = event.get("t")
                if kind == "READY":
                    print("机器人已就绪，在群里 @我 并发指令即可（发「帮助」看指令列表）")
                    continue
                if kind not in GROUP_MESSAGE_EVENTS:
                    continue

                data = event.get("d") or {}
                message_id = str(data.get("id") or "")
                group = str(data.get("group_openid") or "")
                author = (data.get("author") or {}).get("member_openid") or "?"
                raw_content = str(data.get("content") or "")
                command = parse_command(raw_content)
                print()
                print(
                    f"[{time.strftime('%H:%M:%S')}] 收到 {kind}  group={group[:8]}… 来自 {str(author)[:8]}…"
                )
                if kind == "GROUP_MESSAGE_CREATE" and not MENTION_RE.search(raw_content):
                    # 普通群消息事件会**把群里所有消息**都推过来。没 @ 机器人的就不该插嘴，
                    # 否则它会在群里对每句话都回一遍。
                    print("  这条没有 @机器人，忽略")
                    continue
                if not message_id:
                    print("  事件里没有 msg_id，无法被动回复，跳过")
                    continue

                active = settings
                if group and group != settings.qq_group_openid:
                    # 被 @ 的群与配置不同时改用它，而不是把回复发到别的群。
                    active = settings.model_copy(update={"qq_group_openid": group})
                    print(f"  注意：群与配置不同，本次用 {group}")

                try:
                    await handle(
                        QQBotChannel(active), active, budget, message_id, command, images
                    )
                except Exception as exc:  # noqa: BLE001 - 一条指令失败不该让服务退出
                    print(f"  处理指令时异常：{type(exc).__name__}: {exc}")
                    # **故障要说人话，不能沉默。** 之前数据库挂掉时，机器人只把异常
                    # 写进日志、对用户一声不吭——实测用户 @ 了好几次、完全不知道发生了什么，
                    # 只会以为"机器人死了"。
                    await _report_failure(active, message_id, exc)
        finally:
            heartbeat.cancel()


async def main() -> int:
    parser = argparse.ArgumentParser(description="常驻 QQ 机器人：按 @ 的指令推送内容")
    parser.add_argument("--images", type=int, default=3, help="每条回复最多几张图（默认 3）")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱环境")
    parser.add_argument("--cooldown", type=float, default=90.0, help="生成类指令的冷却秒数")
    parser.add_argument("--daily-limit", type=int, default=10, help="生成类指令的每日上限")
    args = parser.parse_args()

    import websockets

    settings = get_settings()
    if not (settings.qq_enabled and settings.qq_app_id and settings.qq_app_secret):
        print("QQ 未配置：需要 QQ_ENABLED=true、QQ_APP_ID、QQ_APP_SECRET")
        return 1
    if not settings.qq_group_openid:
        print("QQ_GROUP_OPENID 未配置。可以先跑 scripts/watch_qq_events.py 取群 openid。")
        return 1

    budget = Budget(cooldown_seconds=args.cooldown, daily_limit=args.daily_limit)
    base = SANDBOX if args.sandbox else PRODUCTION
    print("=" * 64)
    print("常驻 QQ 机器人")
    print(f"  目标群     : {settings.qq_group_openid[:12]}…")
    print(f"  每条回复   : 1 条文字 + 最多 {args.images} 张图")
    # 从实际定义推导，避免又出现"横幅漏列了某组指令"这种不一致。
    all_commands = (
        list(PLATFORM_COMMANDS) + list(HOT_PLATFORM_COMMANDS) + ["知识科普", "关键词：<主题>", "帮助"]
    )
    print(f"  指令       : {'、'.join(all_commands)}")
    print(f"  生成闸门   : 冷却 {args.cooldown:.0f}s，每日 {args.daily_limit} 次")
    print("=" * 64)

    attempt = 0
    health_task: asyncio.Task[None] | None = None
    while attempt < MAX_RECONNECTS:
        attempt += 1
        try:
            token = await _access_token(settings)
            import httpx

            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                response = await client.get(
                    f"{base}/gateway",
                    headers={"Authorization": f"QQBot {token}", **HEADERS},
                )
                url = response.json().get("url")
            if not url:
                print("取网关地址失败，10 秒后重试")
                await asyncio.sleep(10)
                continue
            if attempt == 1:
                print(f"网关：{url}（{'沙箱' if args.sandbox else '生产'}）\n")
            # 健康检查**只启动一次**，不随重连重复起（否则每断线一次就多一个探针）。
            if health_task is None:
                health_task = asyncio.create_task(_health_loop(settings))
                print("健康检查已启动（每 5 分钟一次，仅在状态变化时告警）\n")
            await _session(websockets, url, token, settings, budget, args.images)
            print("连接正常结束")
            return 0
        except KeyboardInterrupt:
            print("\n已停止。")
            return 0
        except Exception as exc:  # noqa: BLE001 - 网络类异常都要自动恢复
            wait = min(30, 3 * attempt)
            print(f"连接断开（{type(exc).__name__}: {exc}），{wait}s 后重连（第 {attempt} 次）")
            await asyncio.sleep(wait)
    print("重连次数用尽，退出。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
