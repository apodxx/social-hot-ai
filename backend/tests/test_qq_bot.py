"""常驻 QQ 机器人的指令解析与额度闸门。

不连网关、不发送任何消息：只测"收到什么文本 → 走哪条分支"这类逻辑，
以及"会花钱的指令"的两道闸（冷却、每日上限）。
网关收发本身靠人工在群里验证——那部分没有可离线断言的东西。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import qq_bot  # noqa: E402


# ------------------------------------------------------------------ 指令解析
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/科普小红书", "科普小红书"),
        ("／热点微博", "热点微博"),  # 全角斜杠
        ("//帮助", "帮助"),
        ("!帮助", "帮助"),
        ("/知识科普", "知识科普"),
        ("/关键词：mysql", "关键词：mysql"),
        ("<@D0F1E2A80ADFCE7447691C7105532D67> /热点抖音", "热点抖音"),
        ("/ 帮助", "帮助"),  # 斜杠后有空格
    ],
)
def test_panel_style_slash_prefix_is_stripped(raw, expected):
    """**回归测试：指令面板点出来的指令带 `/` 前缀。**

    客户端给 ``type=command`` 的项自动加 ``/``，填进输入框的是 ``/科普小红书``。
    第一版没去掉它 → **点面板等于没反应**，用户还得手动删那个斜杠。
    机器人该容忍前缀，而不是让人去适应机器。
    """
    assert qq_bot.parse_command(raw) == expected


def test_stripping_does_not_break_normal_commands():
    """去前缀不能误伤：正常指令、空串、只有斜杠的情况都要稳。"""
    assert qq_bot.parse_command("小红书") == "小红书"
    assert qq_bot.parse_command("") == ""
    assert qq_bot.parse_command("/") == ""
    assert qq_bot.parse_command("///") == ""
    # 「关键词：mysql」里没有前缀，不能被当成前缀切掉内容。
    assert qq_bot.parse_command("关键词：mysql") == "关键词：mysql"


def test_mention_and_whitespace_are_stripped():
    assert qq_bot.parse_command(" 小红书") == "小红书"
    assert qq_bot.parse_command("<@!12345> 微博") == "微博"
    assert qq_bot.parse_command("抖音\n") == "抖音"
    assert qq_bot.parse_command("") == ""
    assert qq_bot.parse_command("<@!999>   帮助  ") == "帮助"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("关键词：mysql", "mysql"),
        ("关键词:mysql", "mysql"),
        ("关键词：  mysql  ", "mysql"),
        ("主题：Redis", "Redis"),
        ("keyword: docker", "docker"),
        ("KEYWORD：MySQL 索引", "MySQL 索引"),
        ("<@!123> 关键词：B+树", "B+树"),
    ],
)
def test_keyword_command_accepts_both_colons_and_aliases(raw, expected):
    """用户会怎么写就都得认：全角/半角冒号、中英文别名、前面带 @ 标记、大小写。"""
    command = qq_bot.parse_command(raw)
    match = qq_bot.KEYWORD_RE.match(command)
    assert match is not None, command
    assert match.group(1).strip() == expected


def test_non_keyword_commands_do_not_match():
    """「小红书」这类平台指令不能被当成主题。"""
    for command in ("小红书", "微博", "抖音", "知识科普", "帮助", "随便说点什么"):
        assert qq_bot.KEYWORD_RE.match(command) is None, command


def test_platform_commands_map_to_the_draft_keys():
    """指令名对应 knowledge_articles.platforms 里的键，改错就会静默回「没有这个版本」。

    允许同时存在带/不带「科普」前缀两种写法（见下面的面板一致性测试），
    所以这里断言的是**映射关系**而不是精确的键集合。
    """
    for command, key in qq_bot.PLATFORM_COMMANDS.items():
        assert key in {"xiaohongshu", "weibo", "douyin"}, (command, key)
    assert qq_bot.PLATFORM_COMMANDS["小红书"] == "xiaohongshu"
    assert qq_bot.PLATFORM_COMMANDS["微博"] == "weibo"
    assert qq_bot.PLATFORM_COMMANDS["抖音"] == "douyin"


# ------------------------------------------------------------------ 额度闸门
def test_budget_allows_first_call_then_enforces_cooldown():
    budget = qq_bot.Budget(cooldown_seconds=90, daily_limit=10)
    assert budget.check() == "", "第一次应当放行"
    budget.consume()
    refusal = budget.check()
    assert "等" in refusal and "秒" in refusal, refusal


def test_budget_enforces_the_daily_limit():
    budget = qq_bot.Budget(cooldown_seconds=0, daily_limit=2)
    budget.consume()
    budget.consume()
    budget.last_run = 0
    refusal = budget.check()
    assert "用完" in refusal and "2/2" in refusal, refusal


def test_budget_resets_on_a_new_day():
    """跨天要归零——否则额度用完一次就永久用不了。"""
    budget = qq_bot.Budget(cooldown_seconds=0, daily_limit=1)
    budget.consume()
    budget.last_run = 0
    assert budget.check() != "", "当天已用完"
    budget.day = "1970-01-01"  # 模拟昨天用过
    assert budget.check() == "", "新的一天应当重新放行"
    assert budget.used_today == 0


def test_mention_regex_handles_hexadecimal_bot_ids():
    """**回归测试：`\\d+` 匹配不了十六进制的机器人 ID。**

    实测事件内容是 ``<@D0F1E2A80ADFCE7447691C7105532D67> 知识科普`` ——
    ID 是十六进制。第一版正则写成 ``<@!?\\d+>``，只认数字，于是 @ 标记整段留下、
    指令变成 ``<@D0F1…> 知识科普``，谁也认不出来。

    症状和"事件根本没送达"一模一样（机器人毫无反应），所以值得钉住。
    """
    real = "<@D0F1E2A80ADFCE7447691C7105532D67> 知识科普"
    assert qq_bot.parse_command(real) == "知识科普"
    assert qq_bot.MENTION_RE.search(real), "必须能识别出这是一条 @ 消息"
    # 老式带感叹号的数字 ID 也要继续支持。
    assert qq_bot.parse_command("<@!12345> 微博") == "微博"
    assert qq_bot.parse_command("<@abc123> 帮助") == "帮助"
    # 没有 @ 标记时不该误判。
    assert not qq_bot.MENTION_RE.search("知识科普")


def test_both_group_message_event_types_are_handled():
    """**回归测试：群消息有两种事件类型，只认一种就等于对 @ 没反应。**

    ``GROUP_AT_MESSAGE_CREATE``（平台判定为 @）与 ``GROUP_MESSAGE_CREATE``（普通群消息，
    @ 标记以文本形式出现）。实测本机器人收到的是**后者**，而第一版只处理前者。
    """
    assert "GROUP_AT_MESSAGE_CREATE" in qq_bot.GROUP_MESSAGE_EVENTS
    assert "GROUP_MESSAGE_CREATE" in qq_bot.GROUP_MESSAGE_EVENTS


def test_the_two_content_systems_have_distinct_commands():
    """**回归测试：两套内容必须有不会混淆的指令名。**

    热点列表里别人的帖子被改写（hot_contents + ai_rewrites）和我们自己写的科普文章
    （knowledge_articles）是完全不同的东西，界面上混在一起会弄不清自己在看什么。
    所以后者加「热点」前缀，且两套指令名**不能重叠**。
    """
    assert not (set(qq_bot.PLATFORM_COMMANDS) & set(qq_bot.HOT_PLATFORM_COMMANDS))
    assert set(qq_bot.HOT_PLATFORM_COMMANDS) == {"热点小红书", "热点微博", "热点抖音"}
    assert set(qq_bot.HOT_PLATFORM_COMMANDS.values()) == {"xiaohongshu", "weibo", "douyin"}


def test_the_panel_label_the_help_text_and_the_command_all_agree():
    """**回归测试："按钮写着 X、帮助说 Y、照着 X 打却不认"。**

    真出现过：按钮标签是「科普小红书」，帮助文案写「小红书」，而能识别的指令只有
    「小红书」——照按钮打反而不认。现在三条线统一：按钮标签、帮助文案、可识别指令
    都用同一组名字，同时保留不带前缀的简写。
    """
    rows = qq_bot.COMMAND_KEYBOARD["content"]["rows"]
    buttons = [b for row in rows for b in row["buttons"]]
    for button in buttons:
        data = button["action"]["data"]
        label = button["render_data"]["label"]
        assert data in qq_bot.HELP_TEXT or data in {"帮助"}, (
            f"按钮 {label!r} 的指令 {data!r} 没写进帮助文案"
        )
        # 每个按钮的指令本身必须能被识别（否则点了没反应）。
        assert (
            data in qq_bot.PLATFORM_COMMANDS
            or data in qq_bot.HOT_PLATFORM_COMMANDS
            or data in {"知识科普", "帮助"}
        ), f"按钮指令 {data!r} 不被识别"

    # 带「科普」前缀与不带前缀都认，两种写法都能用。
    for short, long in (("小红书", "科普小红书"), ("微博", "科普微博"), ("抖音", "科普抖音")):
        assert qq_bot.PLATFORM_COMMANDS[short] == qq_bot.PLATFORM_COMMANDS[long]
        assert short in qq_bot.HELP_TEXT, short
        assert long in qq_bot.HELP_TEXT, long


def test_platform_command_matching_is_exact():
    """`小红书` 不能被当成 `热点小红书` 的简写，反之亦然——否则又混了。"""
    command = "热点小红书"
    assert command in qq_bot.HOT_PLATFORM_COMMANDS
    assert command not in qq_bot.PLATFORM_COMMANDS
    assert "小红书" in qq_bot.PLATFORM_COMMANDS
    assert "小红书" not in qq_bot.HOT_PLATFORM_COMMANDS


def test_the_keyboard_has_both_groups_and_respects_the_label_limit():
    """按钮面板要能区分两组，且每个 label ≤ 10 字符（平台限制）。"""
    rows = qq_bot.COMMAND_KEYBOARD["content"]["rows"]
    labels = [b["render_data"]["label"] for row in rows for b in row["buttons"]]
    assert all(len(label) <= 10 for label in labels), labels
    # 两组各有三个平台按钮。
    assert {"科普小红书", "科普微博", "科普抖音"} <= set(labels)
    assert {"热点小红书", "热点微博", "热点抖音"} <= set(labels)
    # 每个按钮的 data 必须是代码认识的真实指令，而不是好看但没人认得的文字。
    known = (
        set(qq_bot.PLATFORM_COMMANDS)
        | set(qq_bot.HOT_PLATFORM_COMMANDS)
        | {"知识科普", "帮助"}
    )
    for row in rows:
        for button in row["buttons"]:
            assert button["action"]["data"] in known, button["action"]["data"]


def test_hot_platform_text_marks_the_source():
    """热点二创**必须标明出处**——它来自别人的帖子，和科普文章不同。"""
    class _Rewrite:
        xiaohongshu_title = "标题"
        xiaohongshu_content = "正文"
        xiaohongshu_hashtags = ["编程"]
        weibo_title = ""
        weibo_opening = ""
        weibo_content = "微博正文"
        weibo_hashtags = []
        douyin_hook = ""
        douyin_script = ""
        douyin_cta = ""
        needs_verification = True

    class _Item:
        title = "某条热点"
        platform = "weibo"
        url = "https://example.com/x"
        media = {}

    text = qq_bot.hot_platform_text(_Rewrite(), _Item(), "xiaohongshu")
    assert "热点二创" in text, "要标出这是热点二创"
    assert "某条热点" in text, "要标出原帖标题"
    assert "https://example.com/x" in text, "要给出原帖链接"
    assert "需人工核实" in text, "原帖是未核实的传言，这个信号必须保留"


def test_fetch_hot_news_is_distinct_from_send_latest():
    """**「抓新的」和「发已有的」是两个不同的工具，不能混。**

    前者会真的花钱（TikHub 采集 3 个平台 ≈ $0.023），后者只读库。
    如果模型把"获取新的热门新闻"路由到 send_latest，用户就永远拿不到新内容；
    反过来把"发最新的那篇"路由到 fetch_hot_news，则每次都会白花钱。
    """
    from app.services.ai.agent import TOOLS, find_tool

    fetch = find_tool("fetch_hot_news")
    latest = find_tool("send_latest")
    assert fetch is not None and latest is not None
    assert fetch.billed is True, "抓取要花钱，必须过额度闸门"
    assert latest.billed is False, "读库不该标成计费"
    # 描述里必须点明"现在就去抓"与"已经抓好的"的区别，否则模型只能靠猜。
    assert "抓" in fetch.description
    assert "已经" in latest.description or "已生成" in latest.description
    names = [spec.name for spec in TOOLS]
    assert len(names) == len(set(names)), "工具名不能重复"


def test_reading_by_time_is_free_and_fetching_is_billed():
    """**回归测试：问「今天中午的新闻」不该重新花钱采集。**

    实测踩到过：用户说「今天中午的最新消息」被路由到 ``fetch_hot_news``，
    于是**每问一次"中午的新闻"就白花一次 TikHub 采集费**（3 个平台 ≈ $0.023）。

    区分点在描述里必须写明：
      * ``read_hot_news`` —— 读已抓好的，**不花钱**，提到时间段就用它；
      * ``fetch_hot_news`` —— 真的去抓，**花钱**，只在明确说"现在去抓"时用。
    """
    from app.services.ai.agent import find_tool

    read = find_tool("read_hot_news")
    fetch = find_tool("fetch_hot_news")
    assert read is not None, "必须有按时间段读已有数据的工具"
    assert fetch is not None
    assert read.billed is False, "读库不能标成计费"
    assert fetch.billed is True, "采集必须标成计费（要走额度闸门）"
    # 描述里必须明确"时间段 → 读"与"现在去抓 → 采集"的分工。
    assert "时间段" in read.description
    assert "今天中午" in read.description
    assert "现在" in fetch.description and "费用" in fetch.description
    # 时间段参数要覆盖用户会说的那些说法。
    period = read.parameters["properties"]["period"]["enum"]
    for want in ("today", "morning", "noon", "evening"):
        assert want in period, want


def test_period_windows_cover_the_whole_day():
    """时间段划分要覆盖全天，且中午/早上不重叠——重叠会让同一批条目被算两次。"""
    from scripts.qq_bot import PERIOD_HOURS

    morning = PERIOD_HOURS["morning"]
    noon = PERIOD_HOURS["noon"]
    evening = PERIOD_HOURS["evening"]
    assert morning[1] <= noon[0], "早上与中午不能重叠"
    assert noon[1] <= evening[0], "中午与晚上不能重叠"
    assert morning[0] < morning[1] and noon[0] < noon[1] and evening[0] < evening[1]


def test_links_are_tidied_and_search_fallback_is_marked():
    """**回归测试：列表必须带可点链接，且链接不能带几百字符的跟踪参数。**

    实测两条：
      * 抖音分享链接带 600+ 字符的 ``mid/u_code/did/share_sign`` 等参数，
        清理前 3 条内容 = 1734 字 = **3 条 QQ 消息**，清理后 408 字 = **1 条**；
      * 抖音榜单只有 45% 的条目有原始链接（很多是关键词条目），
        没有的要用平台搜索页兜底，否则用户看得到标题却点不进去。
    """
    from scripts.qq_bot import item_link, tidy_link

    long_douyin = (
        "https://www.iesdouyin.com/share/video/7525477523179490569/"
        "?region=US&mid=7525477540599270194&u_code=0&did=MS4wLjABAAAA"
        "&iid=MS4wLjABAAAAGeiaOvvhXvDlXDsyjYpxM2OvG19leU_h5Z5kQbZNiEjF991NoYoZ92r6QfZHPrPc"
        "&share_sign=dYI_yLQw5Yh2iDo2UNszaVrZeaRsC10IofqPA8PPiqA-&ts=1789862463"
    )
    tidied = tidy_link(long_douyin)
    assert tidied == "https://www.iesdouyin.com/share/video/7525477523179490569/"
    assert "?" not in tidied, "查询串该整个去掉"
    # 真实链接 600+ 字符，去掉参数后只剩 58 —— 这里用比例断言，别写死长度。
    assert len(tidied) < len(long_douyin) / 3, f"{len(tidied)} vs {len(long_douyin)}"

    # 小红书的 xsec_token 是打开笔记必需的，**不能**一起删掉。
    xhs = "https://www.xiaohongshu.com/explore/abc?xsec_token=KEEP&foo=drop"
    assert "xsec_token=KEEP" in tidy_link(xhs)
    assert "foo=drop" not in tidy_link(xhs)
    # 不认识的域名原样保留，别乱动别人的链接。
    assert tidy_link("https://example.com/a?b=1") == "https://example.com/a?b=1"

    class _Item:
        def __init__(self, url, platform="douyin", title="某条"):
            self.url, self.platform, self.title = url, platform, title

    link, is_search = item_link(_Item(long_douyin))
    assert link.endswith("/") and not is_search, "有原始链接时不该用搜索页"
    link, is_search = item_link(_Item(None))
    assert is_search and "douyin.com/search" in link, "没链接时要用搜索页兜底"
    assert "某条" not in link or "%" in link, "标题要 URL 编码后进搜索页"


def test_hot_list_has_a_link_for_every_item():
    """列表里**每一条**都要有链接——这是用户的明确要求。"""
    from scripts.qq_bot import format_hot_list

    class _Item:
        def __init__(self, index, url):
            self.title = f"标题{index}"
            self.description = ""
            self.platform = "douyin"
            self.hot_value = None
            self.url = url

    items = [_Item(1, "https://www.iesdouyin.com/share/video/1/?mid=x"), _Item(2, None)]
    body = format_hot_list(items, "今天", fetched=False)
    assert body.count("http") == 2, "两条都该有链接"
    assert "(搜索页)" in body, "兜底的搜索页要标注出来，免得被当成原帖"


def test_topic_list_is_distinct_from_article_generation():
    """**「出题目」和「写文章」是两件事，费用差一个数量级。**

    ``list_knowledge_topics`` 只出 20 个题目（约 ¥0.01），
    ``generate_knowledge_article`` 会真的写一整篇（约 ¥0.05-0.09）。
    混错了要么用户拿不到想要的东西，要么每次都白花一整篇的钱。
    """
    from app.services.ai.agent import find_tool

    topics = find_tool("list_knowledge_topics")
    article = find_tool("generate_knowledge_article")
    assert topics is not None and article is not None
    assert topics.billed is True, "生成清单要花钱（虽然少）"
    assert "题目" in topics.description and "不写文章" in topics.description
    assert "topic" in article.parameters["properties"]


def test_topic_list_output_tokens_must_be_generous():
    """**额度必须给足**：实测 2048 时推理模型把额度全用在思考上、正文为空。

    出 20 条清单的思考特别长，所以代码里取的额度不能低于 8192。
    这个测试盯住"别把它改小"。
    """
    import inspect

    from app.services.ai import agent as agent_module

    source = inspect.getsource(agent_module.generate_topic_list)
    assert "8192" in source, "额度下限被改小了？那会重现「清单为空」"
    assert "budget_tokens" in source


def test_help_text_lists_every_command():
    """帮助里必须提到所有指令，否则用户不知道能用什么。"""
    for token in ("小红书", "微博", "抖音", "关键词", "知识科普", "帮助"):
        assert token in qq_bot.HELP_TEXT, token
