"""用 **function calling** 把"用户随便说一句话"变成"该执行哪个动作"。

解决的是这个场景：用户 @机器人 时**不按指令格式说话** —— 贴一段文字、甩一个链接、
说"帮我把这个改成小红书文案"。机器人不该只会回一份帮助文案，而应该**判断他想干什么**。

设计要点：

* **模型只负责选工具与抽参数，不负责写文案。** 文案仍由各条既有管线生成
  （``promo_service`` / ``knowledge_service`` …），这样质量与成本都在原来的地方受控。
* **工具表就是能力清单**，所以"能做什么"在一处定义、也在一处可查。
* **计费动作复用同一道闸门**（``Budget``），模型无权绕过。
* 模型给不出工具时**如实回退**到帮助文案，而不是硬猜——猜错比不猜更糟。

    python scripts/try_intent.py "帮我把我贴的这段话改成小红书文案"
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings

logger = logging.getLogger(__name__)

#: 供模型判断的系统提示。**刻意写短**：这里只做路由，长提示既贵又容易让模型自作主张发挥。
ROUTER_SYSTEM_PROMPT = """你在为一个中文热点/内容机器人做意图识别。

用户会 @机器人 说一句自然语言，可能附带一段文本或链接。你的任务是**选择要调用的工具**，
并抽取参数。规则：

1. 只从给定工具里选。没有合适的工具时**不要调用任何工具**。
2. **不要自己撰写文案**——文案由工具负责生成。
3. 用户贴了一段文本/链接并想让机器人处理它时，把那段文本原样放进 `text` 参数
   （**不要改写、不要摘要**）。
4. 用户说的主题不明确时不要猜，宁可不调用工具。
5. 计费工具（生成类）在用户明确要求"生成/写一篇"时才调用。
"""


@dataclass
class ToolSpec:
    """一个可供模型调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    #: 是否花钱。计费工具在执行前要过额度闸门。
    billed: bool = False


@dataclass
class ToolCall:
    """模型决定要做的事。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""


#: **能力清单。** 加能力就在这里加一项，模型自动就能用上。
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="convert_text_to_note",
        description=(
            "把用户提供的一段文本、项目说明或网页链接，改写成指定平台的自媒体文案。"
            "当用户贴了一段内容并想让它变成帖子/文案/推广语时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "用户提供的那段原始文本或链接，原样传入，不要改写。",
                },
                "platform": {
                    "type": "string",
                    "enum": ["xiaohongshu", "weibo", "douyin", "all"],
                    "description": "目标平台。用户没指定时用 xiaohongshu。",
                },
            },
            "required": ["text"],
        },
        billed=True,
    ),
    ToolSpec(
        name="list_knowledge_topics",
        description=(
            "给出**一批科普选题**（20 个，覆盖大学生/数码/科技/计算机/嵌入式等），"
            "让用户挑一个。用户说「知识科普列表」「有什么题目」「给点选题」「讲什么好」时用它。"
            "计费很小（约 ¥0.01），但与「生成文章」不同——**它只出题目，不写文章**。"
        ),
        parameters={"type": "object", "properties": {}},
        billed=True,
    ),
    ToolSpec(
        name="generate_knowledge_article",
        description=(
            "按一个主题原创一篇面向大学生的计算机类科普文章（含配图与三平台文案）。"
            "用户明确要求「写一篇」「生成一篇」「科普一下」某个主题时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "文章主题，例如 mysql 索引、红黑树。"}
            },
            "required": ["topic"],
        },
        billed=True,
    ),
    ToolSpec(
        name="ocr_link_images",
        description=(
            "识别链接（尤其是小红书笔记链接）里**图片上的文字**，可据此生成文案。"
            "用户说「把链接里的图片识别出来」「提取图里的文字」「看图写文案」时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "用户给的链接，原样传入。"},
                "platform": {
                    "type": "string",
                    "enum": ["xiaohongshu", "weibo", "douyin"],
                    "description": "目标平台。用户没指定时用 xiaohongshu。",
                },
            },
            "required": ["url"],
        },
        billed=True,
    ),
    ToolSpec(
        name="understand_link_video",
        description=(
            "理解链接里的**视频**（画面 + 字幕 + 讲了什么）。"
            "用户说「把链接里的视频识别出来」「这个视频讲什么」时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "用户给的链接，原样传入。"}
            },
            "required": ["url"],
        },
        billed=True,
    ),
    ToolSpec(
        name="read_hot_news",
        description=(
            "查看**已经抓取过**的热点/热搜，**不花钱、不新增采集**。"
            "用户提到**具体时间段**（「今天中午」「今天早上」「今天的」「最近几小时」）"
            "或问「有哪些」时用它。\n"
            "**但用户说「获取」「抓」「更新」「重新抓」时不要用这个** —— "
            "那是要拿新的，用 fetch_hot_news。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "morning", "noon", "evening", "recent", "all"],
                    "description": (
                        "时间段。今天中午=noon，今天早上=morning，今天晚上=evening，"
                        "最近几小时=recent，笼统的「今天/最新的」=today，没提时间=all。"
                    ),
                },
                "count": {"type": "integer", "description": "列几条，默认 8，最多 15。"},
            },
        },
    ),
    ToolSpec(
        name="fetch_hot_news",
        description=(
            "**现在真的去抓一次**新的热搜榜（微博、抖音、小红书）——**会产生采集费用**。"
            "用户说「**获取最新的**」「抓一下最新的」「更新一下热搜」「重新抓」"
            "「有什么最新热点」时用它 —— **只要出现「获取/抓/更新/最新」这类"
            "要新内容的字眼，就是它**。\n"
            "只有当用户明确指某个**过去的时间段**（今天中午、今天早上）或问「有哪些」时，"
            "才用 read_hot_news 去读已有的。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "列几条，默认 8，最多 15。",
                }
            },
        },
        billed=True,
    ),
    ToolSpec(
        name="generate_images",
        description=(
            "用**文生图**生成图片（不需要参考图）。**计费，约 ¥0.25/张，很贵。**"
            "用户说「生成N张图片」「配图」「画一张封面」时用它。"
            "**张数必须从用户话里取**（说「3张」就填 3），用户没说就不要猜、填 1。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "要画什么。用户给了链接就把链接原样放进来（会先理解链接内容再画）；"
                        "只给了文字描述就用那段文字。"
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": "生成几张。**从用户话里取**：「生成3张图片」→ 3。没说填 1。",
                },
            },
            "required": ["subject"],
        },
        billed=True,
    ),
    ToolSpec(
        name="send_latest",
        description=(
            "把**已经生成好**的内容直接发出来（不重新生成、不花钱）。"
            "用户说「发最新的」「刚才那篇」「把上次那篇发我」这类话时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["knowledge", "hot"],
                    "description": "knowledge=我们自己写的科普文章；hot=热点列表里别人的帖子被改写的。",
                },
                "platform": {
                    "type": "string",
                    "enum": ["xiaohongshu", "weibo", "douyin"],
                    "description": "目标平台。",
                },
            },
            "required": ["source", "platform"],
        },
    ),
    ToolSpec(
        name="show_help",
        description="用户问你能做什么、不知道怎么用、或意图无法判断时用它。",
        parameters={"type": "object", "properties": {}},
    ),
)


def tools_payload(specs: tuple[ToolSpec, ...] = TOOLS) -> list[dict[str, Any]]:
    """转成 OpenAI 的 tools 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]


def find_tool(name: str) -> ToolSpec | None:
    return next((spec for spec in TOOLS if spec.name == name), None)


@dataclass
class Intent:
    """路由结果。"""

    call: ToolCall | None = None
    #: 模型没有选工具时的自由文本回复（通常是它在反问或解释）。
    message: str = ""
    usage_cny: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.call is not None


async def route_intent(
    text: str,
    *,
    settings: Settings,
    client: Any | None = None,
) -> Intent:
    """让模型判断这句话想干什么。

    **只做一次调用，不做多轮 ReAct。** 这个场景下"选一个工具"就够了，多轮会让
    每句话的延迟和成本都翻倍，而用户在群里等着回消息。
    """
    from app.services.ai.deepseek import DeepSeekClient, DeepSeekError

    if not text.strip():
        return Intent(error="没有内容可以判断")

    owned = client is None
    active = client or DeepSeekClient(settings)
    try:
        result = await active.chat(
            [
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=tools_payload(),
            temperature=0.0,
            max_tokens=600,
        )
    except DeepSeekError as exc:
        return Intent(error=f"{type(exc).__name__}: {exc}")
    finally:
        if owned:
            await active.aclose()

    usage_cny = result.usage.estimated_cny()
    if not result.tool_calls:
        # 模型认为没有合适的工具。把它的原话带回去——它可能是在反问用户。
        return Intent(message=(result.content or "").strip(), usage_cny=usage_cny)

    raw = result.tool_calls[0]
    spec = find_tool(str(raw.get("name") or ""))
    if spec is None:
        # 模型编了个不存在的工具。这是模型的问题，不该当成"没有意图"。
        logger.warning("router chose an unknown tool: %r", raw.get("name"))
        return Intent(
            message=f"我理解你想做「{raw.get('name')}」，但我没有这个能力。",
            usage_cny=usage_cny,
        )
    if raw.get("parse_error"):
        logger.warning("router produced invalid arguments: %s", raw["parse_error"])
        return Intent(
            message="我没能解析出你要的参数，能把要求说得更具体一点吗？",
            usage_cny=usage_cny,
        )
    return Intent(
        call=ToolCall(
            name=spec.name,
            arguments=raw.get("arguments") or {},
            raw_arguments=str(raw.get("raw_arguments") or ""),
        ),
        message=(result.content or "").strip(),
        usage_cny=usage_cny,
    )


#: 「把一段文字改成平台文案」的提示。要点：
#: * **不许编造原文里没有的事实**——用户贴的可能是别人的项目说明，
#:   编数字会让他发出去被打脸；
#: * 说人话，别写成产品说明书；
#: * 明确要求它只输出文案本身（这条回复会直接进 QQ 消息，不能带解释和前言）。
CONVERT_SYSTEM_PROMPT = """你是中文自媒体文案编辑。把用户给的原始材料改写成指定平台的图文/口播文案。

**第一原则：原文的具体信息就是价值，一个字都不许抽象掉。**
- 型号、价格、数字、人名、工具名、步骤名、书名、参数——**原样保留**。
- 原文是清单/榜单/分档表（如"3500以下：A 2718元、B 3119元"），
  **就照着列出来**，不要改写成"按预算挑就行"这种空话。
- 原文在讲一件具体的事，就讲那件事；**不要升格成通用人生建议**。
- 判断标准：读者看完能否拿到原文里那些**可执行、可核对的东西**。
  只剩"要轻便""要续航"这类谁都能说的话，就是写失败了。

**排版要求（带序号、通俗易懂）：**

1. **用序号把内容拆成条目**：
   - 原文本身是清单 → 每条一个真实条目（一个型号 / 一个价位段 / 一个步骤）；
   - 原文是叙述 → 按要点拆，但要点里必须带原文的具体信息。
   - 小红书/微博用 `1. 2. 3.`；抖音口播稿同样按序号分段。
2. 每条 1-3 行，先给这条的关键信息，再补一句为什么/怎么用。
3. **通俗易懂**：术语第一次出现时顺手解释半句（"轻薄本，可以理解成主打轻便好带的那类"）。
4. 条目数跟着原文走：原文有 8 个型号就列 8 条，**不要硬压成 5 条**。
5. 开头第一句就要抓人，不要"今天给大家介绍"这类套话。
6. 结尾附 3-5 个平台话题标签，用 # 开头。

硬性要求：

1. **只使用材料里出现的事实。** 不补充、不推测、不编数字或结论。
2. 直接输出文案正文，**不要任何前言、解释或 Markdown 标记**（不要 `**加粗**`）。
3. 篇幅：小红书 250-600 字（原文信息多就写长些），微博 120-300 字，抖音 200-450 字。
"""

PLATFORM_NAMES = {
    "xiaohongshu": "小红书",
    "weibo": "微博",
    "douyin": "抖音",
}


#: 从一段文字里找出第一个 http(s) 链接。
#: 用户会把链接夹在句子里（"标题… https://… 帮我整理成文案"），所以**不能用
#: ``startswith`` 判断**——那样链接分支会被整个跳过，症状是"没有识别到任何内容"。
URL_PATTERN = re.compile(r"https?://[^\s，。；、）】\"'<>]+")


def first_url(text: str) -> str:
    match = URL_PATTERN.search(text or "")
    return match.group(0) if match else ""


#: 把素材变成**生图提示词**。要点：
#: * 只描述**画面**（主体、场景、光线、构图、色调），不要把正文照抄进去；
#: * **不要出现文字**——文生图画出来的汉字基本都是乱码；
#: * 竖版构图，给标题留位置（小红书封面习惯）。
IMAGE_PROMPT_SYSTEM = """你把一段素材改写成**文生图提示词**，用来画小红书封面图。

规则：
1. 只描述画面：主体、场景、道具、光线、色调、构图。**不要复述素材内容**。
2. **绝对不要在画面里放任何文字或字母**——文生图写汉字一定是乱码。
3. 竖版构图（3:4），画面上方留出空白区域，方便后期加标题。
4. 风格：干净、明亮、真实感，适合大学生群体，不要夸张的科幻或赛博风格。
5. 直接输出一段 60-120 字的提示词，**不要任何解释、不要分行**。
"""


async def image_prompt_from(
    material: str, *, settings: Settings, client: Any | None = None
) -> str:
    """把素材（正文/视频理解结果/用户描述）压成一条生图提示词。**一次便宜的调用。**"""
    from app.services.ai.deepseek import DeepSeekClient, DeepSeekError

    trimmed = (material or "").strip()[:1500] or "大学生的电子设备"
    owned = client is None
    active = client or DeepSeekClient(settings)
    messages = [
        {"role": "system", "content": IMAGE_PROMPT_SYSTEM},
        {"role": "user", "content": trimmed},
    ]
    prompt = ""
    try:
        result = await active.chat(
            messages, json_mode=False, temperature=0.7, max_tokens=1024
        )
        prompt = " ".join((result.content or "").split())
        if not prompt:
            # **空正文要重试。** 实测就是这里返回空、退回了兜底提示词，
            # 于是画出来的图和内容基本无关——而用户只看到"一张泛泛的图"，
            # 完全不知道提示词那一步失败了。
            logger.warning(
                "image prompt empty (finish_reason=%r); retrying once", result.finish_reason
            )
            retried = await active.chat(
                messages
                + [{"role": "user", "content": "上一版没有输出。请直接给出提示词正文。"}],
                json_mode=False,
                temperature=0.9,
                max_tokens=1024,
            )
            prompt = " ".join((retried.content or "").split())
    except DeepSeekError as exc:
        logger.warning("image prompt generation failed: %s", exc)
    finally:
        if owned:
            await active.aclose()
    # 兜底：两次都没给出提示词时，至少别把正文直接丢给画图模型。
    return prompt or "一张干净简洁的小红书封面图，竖版构图，画面上方留白，不要出现任何文字"


#: 生成选题清单。要点：
#: * 题目要**具体到能直接写文章**（"MySQL 索引为什么用 B+树" 而不是 "数据库"）；
#: * 覆盖用户点名的领域，且**跨领域打散**，不要连着几条都是同一类；
#: * 面向大学生，偏"能讲明白一个概念"的题目，不要标题党。
TOPIC_LIST_SYSTEM = """你为一个面向大学生的计算机科普账号出选题。

请出 **20 个** 选题，要求：

1. **覆盖这些领域，且分布均衡**：大学生活与学习、数码产品、科技趋势、
   计算机基础、编程语言、嵌入式与硬件、AI 与算法。
2. **每个题目都要具体到能直接写成一篇文章**，
   例如「MySQL 索引为什么用 B+树」而不是「数据库」；
   「为什么单片机上不能随便用 malloc」而不是「嵌入式」。
3. 角度要**有钩子**：回答一个"为什么"、破除一个误解、或讲清一个日常现象背后的原理。
4. 面向计算机大类的大学生，别太浅也别太学术。
5. 不要编号以外的解释文字，不要分组标题。

输出格式：每行一个，`1. 题目` 到 `20. 题目`，题目控制在 20 字以内。
"""


async def generate_topic_list(
    *, settings: Settings, client: Any | None = None
) -> list[str]:
    """生成 20 个科普选题。**一次便宜的调用（约 ¥0.01）。**"""
    from app.services.ai.deepseek import DeepSeekClient, DeepSeekError

    owned = client is None
    active = client or DeepSeekClient(settings)
    messages = [
        {"role": "system", "content": TOPIC_LIST_SYSTEM},
        {"role": "user", "content": "请给出这 20 个选题。"},
    ]
    raw = ""
    # **额度必须给足。** ``deepseek-flash`` 是推理模型，思考 token 也计入 completion；
    # 实测 max_tokens=2048 时它把额度全用在思考上、正文一个字都没有
    # （finish_reason='length'、len(content)=0）。出 20 条清单这种任务思考特别长。
    budget_tokens = max(8192, settings.article_max_tokens)
    try:
        result = await active.chat(
            messages, json_mode=False, temperature=0.9, max_tokens=budget_tokens
        )
        raw = result.content or ""
        if not raw.strip():
            # 空了就再试一次，并明确要求"少思考、直接列"。
            logger.warning(
                "topic list empty (finish_reason=%r, completion=%d); retrying",
                result.finish_reason,
                result.usage.completion_tokens,
            )
            retried = await active.chat(
                messages
                + [
                    {
                        "role": "user",
                        "content": "上一版没有输出正文。直接列出 20 个题目，不要思考和解释。",
                    }
                ],
                json_mode=False,
                temperature=0.95,
                max_tokens=budget_tokens,
            )
            raw = retried.content or ""
    except DeepSeekError as exc:
        logger.warning("topic list generation failed: %s", exc)
    finally:
        if owned:
            await active.aclose()

    # 解析出题目：容忍模型加粗/编号样式不一致。
    topics: list[str] = []
    for line in raw.splitlines():
        cleaned = re.sub(r"^\s*(?:\d+[.、)）]|[-*])\s*", "", line.strip())
        cleaned = cleaned.strip(" 　*#")
        if 4 <= len(cleaned) <= 40:
            topics.append(cleaned)
    return topics[:20]


@dataclass
class Conversion:
    """一次「文本 → 平台文案」的结果。"""

    text: str = ""
    platform: str = "xiaohongshu"
    usage_cny: float = 0.0
    error: str = ""
    #: 从链接流程顺带取到的图片，随结果带回去。
    media_paths: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


async def convert_text_to_note(
    source: str,
    *,
    platform: str = "xiaohongshu",
    settings: Settings,
    client: Any | None = None,
) -> Conversion:
    """把任意文本/链接改写成平台文案。**计费**（一次 DeepSeek 调用）。"""
    from app.services.ai.deepseek import DeepSeekClient, DeepSeekError

    material = (source or "").strip()
    if not material:
        return Conversion(error="没有可改写的原文")
    wanted = platform if platform in PLATFORM_NAMES else "xiaohongshu"

    from app.services.ai.promo_service import load_readme
    from app.services.tikhub.video_link import fetch_douyin_material, is_douyin_link

    # **先从整段文本里把链接找出来。**
    # 不能对整段材料用 ``startswith`` / ``urlparse``：用户实际会发
    # 「标题… https://www.iesdouyin.com/share/video/xxx 帮我整理成小红书文案」——
    # 那样 ``urlparse(整段).netloc`` 是空的，链接分支整个被跳过，
    # 表现就是"没有识别到任何内容"，而看起来像视频功能坏了。
    # （qq_bot.py 里已经修过同一类问题，这里当时漏了。）
    link = first_url(material)
    # 把链接从材料里摘掉，剩下的当补充说明——否则模型会把 URL 也当正文改写。
    context = material.replace(link, " ").strip() if link else material
    collected_media: list[str] = []

    # **视频链接要先"看懂视频"，否则文案只能靠标题瞎编。**
    # 实测过的坑：用户丢一个抖音视频链接进来，只拿到标题
    # 「无广分享！准大一们看过来！电子设备怎么选」，生成的文案就只是把这几个词
    # 换个说法重复一遍，用户反馈"一点用没有"。
    # 抖音分享页抓不到视频地址（HTML 里 0 个 mp4），所以走 TikHub 取地址再送全模态模型。
    if link and is_douyin_link(link):
        # **自动判断视频还是图文**（看 TikHub 返回里有什么，不靠 URL 猜）。
        # 实测用户发过一条 ``/share/note/`` 的图文笔记，被当成视频处理，
        # 收到"没有找到可播放的视频地址（可能是图文）"——明明是图文却要用户自己猜。
        material_result = await fetch_douyin_material(link, settings=settings)
        if not material_result.ok:
            return Conversion(error=material_result.error)
        material = material_result.text
        # **把链接流程取到的图带回去。** 不带上就会被调用方丢掉，
        # 然后它用普通网页抓图去抓一遍（对抖音无效），用户看到"配图没抓到"。
        collected_media = list(material_result.media_paths)
        if context:
            material += f"\n\n用户还补充说：{context}"
        logger.info(
            "douyin material ready: kind=%s %d chars, %d image(s)",
            material_result.kind,
            len(material),
            len(collected_media),
        )

    # 链接要先把正文抓下来，否则模型只能对着 URL 干猜。
    # 复用 ``load_readme`` —— 它已经处理了重定向、大小上限与"抓到的是 HTML 登录页"
    # 这些情况（项目里踩过：把登录页当正文送去改写，白花一次钱）。
    # 它是同步的，放到线程里跑，别把事件循环堵住。
    elif link:
        try:
            fetched, _kind, _name = await asyncio.to_thread(load_readme, url=link)
        except ValueError as exc:
            return Conversion(error=f"链接内容用不了：{exc}")
        except Exception as exc:  # noqa: BLE001 - 网络类异常也要如实回给用户
            return Conversion(
                error=f"链接抓取失败（{type(exc).__name__}），你可以把正文直接贴给我。"
            )
        if not fetched.strip():
            return Conversion(error="链接里没抓到正文，你可以把正文直接贴给我。")
        material = f"（来源：{material}）\n\n{fetched}"

    owned = client is None
    active = client or DeepSeekClient(settings)
    try:
        result = await active.chat(
            [
                {"role": "system", "content": CONVERT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"目标平台：{PLATFORM_NAMES[wanted]}\n\n原始材料：\n{material}",
                },
            ],
            json_mode=False,
            temperature=0.8,
            # **必须给足。** ``deepseek-flash`` 是推理模型，它的**思考 token 也计入
            # completion_tokens**。实测 max_tokens=1200 时出现过
            # ``finish_reason='length'`` + ``completion=1200`` + **content 为空**：
            # 思考把额度吃光，一个字正文都没留下。而同样参数下一次又会成功
            # （思考较短、finish_reason='stop'）——所以这是概率性的，不能靠调参碰运气。
            max_tokens=4096,
        )
    except DeepSeekError as exc:
        return Conversion(error=f"{type(exc).__name__}: {exc}")
    finally:
        if owned:
            await active.aclose()

    body = (result.content or "").strip()
    if not body:
        # **空正文就重试一次，而不是拿 reasoning_content 顶替。**
        # 曾经用思考内容兜底，结果把模型的内心独白当成答复发了出去 ——
        # 用户实测收到三段 "Hmm, ambiguous… I'll go with…" 的英文思考过程。
        # 思考不是答复；空内容该重试。
        logger.warning(
            "conversion returned empty content (finish_reason=%r); retrying once",
            result.finish_reason,
        )
        retry_messages = [
            {"role": "system", "content": CONVERT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"目标平台：{PLATFORM_NAMES[wanted]}\n\n原始材料：\n{material}",
            },
            {
                "role": "user",
                "content": "上一版没有输出任何正文。请直接给出文案正文，不要任何解释。",
            },
        ]
        reused = client is not None
        second = client or DeepSeekClient(settings)
        try:
            retried = await second.chat(
                retry_messages, json_mode=False, temperature=0.9, max_tokens=4096
            )
        except DeepSeekError as exc:
            return Conversion(error=f"重试也失败：{type(exc).__name__}: {exc}")
        finally:
            if not reused:
                await second.aclose()
        retried_body = (retried.content or "").strip()
        if retried_body:
            return Conversion(
                text=retried_body,
                platform=wanted,
                usage_cny=result.usage.estimated_cny() + retried.usage.estimated_cny(),
            )
        # 两次都空：如实报原因（撞额度 vs 模型什么都没说，是两种不同的问题）。
        if result.finish_reason == "length":
            return Conversion(
                error=(
                    f"模型把 {result.usage.completion_tokens} tokens 的额度全用在思考上了，"
                    "没输出正文。请再发一次（或把原文缩短一点）。"
                ),
                usage_cny=result.usage.estimated_cny(),
            )
        return Conversion(
            error=f"模型两次都没有输出内容（finish_reason={result.finish_reason or '未知'}）。",
            usage_cny=result.usage.estimated_cny(),
        )

    return Conversion(
        text=body,
        platform=wanted,
        usage_cny=result.usage.estimated_cny(),
        media_paths=collected_media,
    )
