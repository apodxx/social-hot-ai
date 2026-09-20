"""Application settings.

Every value comes from the environment (or the project-root ``.env``); nothing
is hardcoded here beyond safe defaults. Phase 1 only consumes the TikHub and
hot-fetch settings, but the later phases' keys are declared now so a single
``.env.example`` describes the whole system.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/app/core -> backend/app -> backend -> social-hot-ai
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = PROJECT_ROOT / "backend"


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case env variables."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ TikHub
    tikhub_api_key: str = ""
    tikhub_base_url: str = "https://api.tikhub.io"
    tikhub_timeout_seconds: float = 30.0
    tikhub_max_retries: int = 3
    tikhub_backoff_base_seconds: float = 0.5

    # --------------------------------------------------------------- Hot fetch
    hot_limit_per_platform: int = 50
    hot_fetch_times: str = "08:00,12:00,18:00"

    # ----------------------------------------------------------------- Dedup
    #: Title-similarity ratio at or above which two titles are the same topic.
    dedup_title_similarity: float = 0.82
    #: Only items newer than this window are compared (bounded work per run).
    dedup_window_hours: int = 48
    #: Hard cap on items pulled into one clustering pass.
    dedup_max_compare: int = 400

    # ------------------------------------------------- Phase 3: AI analysis
    #: Items per DeepSeek request. Batching is the main cost lever: 30 items in
    #: three calls instead of thirty.
    analysis_batch_size: int = 10
    #: Cap on items sent to DeepSeek after the free rule filter.
    analysis_max_candidates: int = 30
    #: Final selection size (spec: 5-10 items reach rewriting).
    analysis_max_selected: int = 10
    #: A recommendation below this confidence is not selected.
    analysis_min_confidence: float = 0.5
    #: One extra (cheap) call that merges semantically identical topics across
    #: platforms. Off by default: it is an extra billed request per run.
    analysis_semantic_merge: bool = False
    #: Account profile JSON; empty means ``<project root>/account_profile.json``.
    analysis_profile_path: str = ""
    #: Re-analyse items already analysed within this many hours (0 = never).
    analysis_reuse_hours: int = 24

    # -------------------------------------------------- Phase 4: AI rewriting
    #: Creative work needs more freedom than analysis; 0.7 is deliberately higher
    #: than the 0.2 used for judging.
    rewrite_temperature: float = 0.7
    #: Three platform versions plus hashtags do not fit in 3000: a real run hit
    #: the cap and the JSON was cut off mid-structure, so the ceiling is 6000.
    rewrite_max_tokens: int = 6000
    #: 科普文章的输出上限。**不能复用 REWRITE_MAX_TOKENS**：文章有 3-7 节、每节 200-500 字，
    #: 再加术语表与延伸阅读，6000 会被截断成不合法的 JSON（实测踩到：报错里 JSON 断在
    #: `"audie`）。单独给一个更宽的上限。
    article_max_tokens: int = 8000
    #: 0 means "whatever Phase 3 selected", capped by ANALYSIS_MAX_SELECTED.
    rewrite_max_items: int = 0
    #: Similarity against the source above which output counts as a mechanical
    #: rewrite (spec section 十八) and is regenerated once, then flagged.
    rewrite_similarity_limit: float = 0.6
    #: How long an existing rewrite is reused (0 = never rewrite again).
    rewrite_reuse_hours: int = 24
    #: One stricter retry when the first attempt looks copied.
    rewrite_retry_on_copy: bool = True

    # --------------------------------------------------- Phase 5: scheduling
    #: Start the APScheduler instance with the app. Tests turn this off.
    scheduler_enabled: bool = True
    #: Timezone for the cron triggers; HOT_FETCH_TIMES is local wall-clock time.
    scheduler_timezone: str = "Asia/Shanghai"
    #: A task still marked ``running`` after this long is treated as dead and
    #: failed on sight, so one crashed run cannot block every later run.
    task_stale_minutes: int = 30

    # --------------------------------- Section 十六: detail fetching (billed)
    #: Fetch the detail behind each selected item before rewriting. **One billed
    #: TikHub call per item**, so it is off by default; enable it when the
    #: rewriting quality matters more than the call budget.
    detail_fetch_enabled: bool = False
    #: Cap on detail fetches per run (each is a billed TikHub call).
    detail_max_items: int = 10

    # ----------------------------------------------------------------- Logging
    log_level: str = "INFO"

    # --------------------------------------------------------------- HTTP server
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # ----------------------------------------------------- Phase 8: MCP server
    #: Mount the MCP endpoint at ``/mcp/`` inside this process. One service decides
    #: availability: start the backend and the tools exist, stop it and they are gone.
    mcp_enabled: bool = True
    #: When false the spending tools are not registered at all — an unregistered tool
    #: costs no context and cannot be called by accident. The human approval gate in
    #: the MCP client is the primary protection; this is the lock for other clients.
    mcp_allow_billed: bool = True

    # ------------------------------------------- Phase 9: keyword search + media
    #: How many search hits to keep per platform per keyword.
    search_limit_per_platform: int = 20
    #: Platforms a bare search covers when the caller does not choose.
    search_platforms: str = "xiaohongshu,douyin,weibo"

    #: Download images into the local material library.
    #:
    #: On by default because the provider URLs are **signed and expire**: a row that
    #: only stores the link is unusable a few hours later. Everything is capped, and a
    #: failure to fetch is recorded rather than raised.
    media_download_enabled: bool = True
    #: Where downloads live, relative to the project root.
    media_root: str = "media"
    #: Most images taken from any single item (a real note had 7).
    media_max_images_per_item: int = 9
    #: Per-file ceiling. Provider images run to a few hundred KB; this stops a
    #: mislabelled video or a redirect to something huge.
    media_max_bytes_per_file: int = 8 * 1024 * 1024
    #: Fetch timeout for one image.
    media_timeout_seconds: float = 20.0
    #: Wall-clock budget for one download batch, so a slow CDN cannot stall a run.
    media_batch_seconds: float = 120.0

    # ------------------------------------------- Phase 10: domain focus
    #: The topics this account publishes about. Used to *rank* what gets analysed, so
    #: the paid step sees technology content instead of whatever is trending — the hot
    #: boards are dominated by entertainment, and selection used to be pure heat order.
    #:
    #: Matching is free and deterministic (code, not tokens). It can only reorder and
    #: remove: a board with no technology items yields nothing, which is why
    #: ``WATCH_KEYWORDS`` (paid searches) exists.
    interest_keywords: str = (
        "科技,数码,人工智能,AI,大模型,机器学习,编程,代码,程序员,开发者,开源,GitHub,"
        "计算机,软件,算法,数据库,前端,后端,Python,Java,JavaScript,Linux,考研,大学生,"
        "学生,毕业,毕业季,求职,就业,实习,校园,面试"
    )
    #: Anything matching one of these is pushed down (and can be excluded outright).
    interest_negative_keywords: str = "明星,八卦,恋情,绯闻,离婚,出轨,综艺,选秀,球赛,彩票"
    #: When true, items matching no interest keyword are dropped *before* the paid
    #: analysis rather than merely ranked lower. This is the money-saving switch:
    #: it is the difference between paying to analyse 30 entertainment items and
    #: paying to analyse the 30 most relevant ones in the window.
    interest_only: bool = True

    #: Keywords searched on a schedule so domain content arrives even when it never
    #: trends. **Each keyword costs one billed search per platform per run.** With the
    #: defaults below that is 3 keywords x 2 platforms = 6 billed calls per run.
    watch_search_enabled: bool = True
    watch_keywords: str = "AI工具,编程学习,数码好物"
    #: Kept short by default on cost grounds; add weibo to widen it.
    watch_search_platforms: str = "xiaohongshu,douyin"
    watch_search_limit: int = 10
    #: Download images for watch results (they are the 图文 material).
    watch_download_media: bool = True

    # ------------------------------------- Phase 11: image generation (Qwen)
    #: Alibaba Model Studio (DashScope). The workspace-scoped endpoint, NOT the shared
    #: dashscope.aliyuncs.com one — the model, endpoint and key must be the same region.
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    qwen_image_model: str = "qwen-image-3.0-pro"

    # ------------------------------------------------------------------ 视觉理解
    #: OCR 用的是**私有部署端点**，和标准 dashscope.aliyuncs.com 不是同一个域名
    #: （形如 ``https://ws-xxxx.cn-beijing.maas.aliyuncs.com``）。所以必须单独配，
    #: 不能复用 dashscope_base_url。实测三个模型名 qwen3.5-ocr / qwen-vl-ocr /
    #: qwen-vl-ocr-latest 在该端点都可用。
    ocr_base_url: str = ""
    ocr_model: str = "qwen3.5-ocr"
    #: 全模态模型（可理解视频）。走标准域名。
    omni_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    omni_model: str = "qwen3.8-omni-flash"
    #: 单次 OCR 最多识别几张图——每张约 1000 图片 token，多了既慢又贵。
    ocr_max_images: int = 4
    #: 是否先把多张图拼成**一张网格图**再 OCR / 发送。
    #: 好处：只占 1 条 QQ 消息（被动回复一次只有 3-4 条额度，7 张图发不完），
    #: 且 OCR 一次就能看全，便于确认"到底读了几张"。
    #: 代价：拼图按长边缩放，格子里的字会比原图小；**密集小字的图建议关掉**。
    ocr_stitch_images: bool = True

    #: Image generation costs **per image, not per token** — measured from the published
    #: price list: 1K output $0.03438, 2K output $0.068761 (China Beijing). A single image
    #: is therefore ~15x a whole text rewrite, which is why nothing generates images
    #: automatically and every call sits behind a confirmation.
    image_gen_enabled: bool = True
    #: Kept at 1024x1024: billing tiers on the OUTPUT pixel area, and 1024*1024 is inside
    #: the 1K tier. DashScope separates width and height with ``*`` (OpenAI mode uses `x`).
    image_gen_size: str = "1024*1024"
    #: Images per request (n). 1 keeps the cost predictable; the model allows up to 6.
    image_gen_n: int = 1
    #: Hard ceiling per run, so a bug cannot fan out into dozens of paid images.
    image_gen_max_per_run: int = 2
    #: Generation is slow (and the model is rate limited to 5 requests/minute).
    image_gen_timeout_seconds: float = 300.0
    #: Never send the original photo's subject verbatim into a *new* post without the
    #: operator's intent; this records that the output is a redirectable derivative.
    image_gen_watermark: bool = False

    # ------------------------------------------------- Declared for later phases
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    #: Confirmed against GET /models for this key: deepseek-flash, deepseek-v4-pro.
    #: ``deepseek-chat`` no longer exists and would 404.
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout_seconds: float = 120.0
    deepseek_max_retries: int = 2
    deepseek_temperature: float = 0.2
    deepseek_max_tokens: int = 4000
    # Local project cluster (see README "数据库"); the bundled 5432 service is
    # left untouched because its credentials belong to the machine owner.
    database_url: str = "postgresql+asyncpg://postgres@127.0.0.1:55432/social_hot_ai"

    # -------------------------------------------------- Phase 6: notification
    notification_enabled: bool = False
    #: One channel: ``log`` (verification only), ``wechat``, or ``qq``.
    notification_channel: str = "log"
    #: Comma-separated list; when set it wins over the single-channel form (§24).
    notification_channels: str = ""
    #: Message ceiling before splitting (§28). WeChat robots accept 4096 bytes.
    notification_max_chars: int = 1500
    #: How many rewrites one digest covers.
    notification_max_items: int = 10

    # 企业微信 group robot (official API).
    wechat_enabled: bool = False
    wechat_webhook_url: str = ""
    wechat_msgtype: str = "markdown"

    # QQ 开放平台 Bot (official API only).
    qq_enabled: bool = False
    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_bot_token: str = ""
    qq_target_openid: str = ""
    qq_channel_id: str = ""
    qq_sandbox: bool = False
    #: 群聊目标（推荐）。文字子频道发消息要求机器人常驻 WebSocket，HTTP 方式发不出去，
    #: 所以群聊与单聊才是可用的两个场景。群 openid 从机器人收到的事件里取。
    qq_group_openid: str = ""
    #: 一条推送里最多附几张图。**QQ 主动消息按条计频**（单关系 20/qpm、1000 条/天），
    #: 而富媒体接口一次只能带一个 file_info，所以每张图就是一条消息。
    qq_max_images: int = 6
    #: 上传超时（官方建议 ≥5 秒；图片通常几百 KB，给足余量）。
    qq_upload_timeout_seconds: float = 60.0

    @field_validator("hot_limit_per_platform")
    @classmethod
    def _check_limit(cls, value: int) -> int:
        if not 1 <= value <= 200:
            raise ValueError("HOT_LIMIT_PER_PLATFORM must be between 1 and 200")
        return value

    @field_validator("tikhub_max_retries")
    @classmethod
    def _check_retries(cls, value: int) -> int:
        if not 0 <= value <= 10:
            raise ValueError("TIKHUB_MAX_RETRIES must be between 0 and 10")
        return value

    @field_validator("hot_fetch_times")
    @classmethod
    def _check_fetch_times(cls, value: str) -> str:
        """Reject an impossible time here, not at scheduler start.

        Without this, ``HOT_FETCH_TIMES=25:00`` was accepted, then
        ``CronTrigger(hour=25)`` raised while the scheduler started — the exception
        was caught and logged, and the result was a scheduler that silently never
        ran. A bad value must fail where it is written.
        """
        for chunk in value.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            hour, separator, minute = chunk.partition(":")
            if not separator or not hour.isdigit() or not minute.isdigit():
                raise ValueError(f"invalid time {chunk!r}; expected HH:MM")
            if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
                raise ValueError(f"invalid time {chunk!r}; hour is 00-23 and minute is 00-59")
        return value

    @field_validator(
        "analysis_max_candidates",
        "analysis_max_selected",
        "hot_limit_per_platform",
        "search_limit_per_platform",
        "media_max_images_per_item",
        "watch_search_limit",
        "image_gen_max_per_run",
    )
    @classmethod
    def _check_positive_counts(cls, value: int, info) -> int:
        """Reject zero: it silently disables a feature instead of configuring it.

        ``MEDIA_MAX_IMAGES_PER_ITEM=0`` is the clearest example — it does not read as
        "off", it reads as a number, and the result is that images quietly stop being
        captured. Anything that can be switched off has a boolean for that.
        """
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value

    @field_validator("image_gen_n")
    @classmethod
    def _check_image_count(cls, value: int, info) -> int:
        """Locked to 1 by policy: **every image is billed separately**.

        The provider allows up to 6, and each one costs another $0.03438 — so a larger
        ``n`` is a 6x bill, not a convenience. The operator's stated policy is one image
        per call, and this makes that a guarantee rather than a default that any request
        could override. Relax it here (and in the API/MCP signatures) if that changes.
        """
        if value != 1:
            raise ValueError(
                f"{info.field_name} must be 1: 图片按张计费，每次一张是既定策略。"
                "要一次生成多张，请同时修改这里与接口参数，并确认费用。"
            )
        return value

    @field_validator("image_gen_size")
    @classmethod
    def _check_size_tier(cls, value: str, info) -> str:
        """Keep generation inside the 1K billing tier.

        Billing follows the OUTPUT pixel area: ≤ 2,250,000 px is the 1K tier at
        $0.03438, above it is the 2K tier at $0.068761 — exactly double. Silently
        accepting "2048*2048" would therefore double every image's price, so the tier is
        enforced rather than merely documented.
        """
        text = (value or "").strip().lower()
        width, separator, height = text.partition("*")
        if separator != "*" or not width.isdigit() or not height.isdigit():
            raise ValueError(
                f"{info.field_name} must look like 1024*1024 (DashScope uses '*', "
                "the OpenAI-compatible mode uses 'x' — mixing them is a 400)"
            )
        area = int(width) * int(height)
        if area < 512 * 512 or area > 2048 * 2048:
            raise ValueError(f"{info.field_name} must be between 512*512 and 2048*2048")
        if area > 2_250_000:
            raise ValueError(
                f"{info.field_name}={value} is {area} px, above the 1K tier (2,250,000) — "
                "it would be billed as 2K at $0.068761/image, double the 1K price. "
                "既定的策略是 1K；要改成 2K 请同时确认费用。"
            )
        return text

    @field_validator("media_max_bytes_per_file")
    @classmethod
    def _check_byte_cap(cls, value: int, info) -> int:
        """A cap below 1 KiB cannot hold an image and is certainly a mistake."""
        if value < 1024:
            raise ValueError(f"{info.field_name} must be at least 1024 bytes")
        return value

    @field_validator("media_timeout_seconds", "media_batch_seconds")
    @classmethod
    def _check_positive_seconds(cls, value: float, info) -> float:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than 0")
        return value

    @property
    def hot_fetch_time_pairs(self) -> list[tuple[int, int]]:
        """``HOT_FETCH_TIMES`` parsed into ``(hour, minute)`` pairs."""
        pairs: list[tuple[int, int]] = []
        for chunk in self.hot_fetch_times.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            hour, _, minute = chunk.partition(":")
            pairs.append((int(hour), int(minute or 0)))
        return pairs

    @property
    def tikhub_configured(self) -> bool:
        """True when a non-placeholder API key is present."""
        key = self.tikhub_api_key.strip()
        return bool(key) and key != "YOUR_API_KEY"

    @property
    def deepseek_configured(self) -> bool:
        """True when a DeepSeek key and model name are both present."""
        return bool(self.deepseek_api_key.strip()) and bool(self.deepseek_model.strip())

    @property
    def account_profile_file(self) -> Path:
        """The account-profile JSON: configured path, else the project root."""
        if self.analysis_profile_path.strip():
            return Path(self.analysis_profile_path.strip()).expanduser()
        return PROJECT_ROOT / "account_profile.json"

    @property
    def media_root_path(self) -> Path:
        """Absolute path of the local material library."""
        root = Path(self.media_root)
        return root if root.is_absolute() else PROJECT_ROOT / root

    @property
    def interest_keyword_list(self) -> list[str]:
        """Interest keywords, longest first so "人工智能" wins over "人工"."""
        raw = [part.strip() for part in (self.interest_keywords or "").split(",") if part.strip()]
        # Longest-first matters: a shorter keyword matching inside a longer one would
        # otherwise produce misleading "matched" tags.
        return sorted(dict.fromkeys(raw), key=len, reverse=True)

    @property
    def interest_negative_list(self) -> list[str]:
        raw = [
            part.strip() for part in (self.interest_negative_keywords or "").split(",") if part.strip()
        ]
        return sorted(dict.fromkeys(raw), key=len, reverse=True)

    @property
    def watch_keyword_list(self) -> list[str]:
        raw = [part.strip() for part in (self.watch_keywords or "").split(",") if part.strip()]
        return list(dict.fromkeys(raw))

    @property
    def watch_platform_list(self) -> list[str]:
        wanted = [
            part.strip() for part in (self.watch_search_platforms or "").split(",") if part.strip()
        ]
        known = ["xiaohongshu", "douyin", "weibo"]
        return [name for name in known if name in wanted] or ["xiaohongshu"]

    @property
    def watch_billed_calls_per_run(self) -> int:
        """How many billed TikHub calls one watch stage costs.

        Surfaced in the startup log, the task record and the settings page: a recurring
        charge the operator cannot see is a charge they cannot control.
        """
        if not self.watch_search_enabled:
            return 0
        return len(self.watch_keyword_list) * len(self.watch_platform_list)

    @property
    def search_platform_list(self) -> list[str]:
        """Platforms a bare keyword search covers, in a stable order."""
        wanted = [part.strip() for part in (self.search_platforms or "").split(",") if part.strip()]
        known = ["xiaohongshu", "douyin", "weibo"]
        return [name for name in known if name in wanted] or known

    @property
    def dashscope_configured(self) -> bool:
        """True when an image generation key and endpoint are present."""
        key = (self.dashscope_api_key or "").strip()
        return bool(key) and not key.startswith("your-") and bool(self.dashscope_base_url)

    @property
    def image_gen_endpoint(self) -> str:
        """The DashScope multimodal-generation URL on the configured workspace host."""
        return (
            f"{self.dashscope_base_url.rstrip('/')}"
            "/api/v1/services/aigc/multimodal-generation/generation"
        )

    @property
    def image_gen_cost_usd_per_image(self) -> float:
        """Published price for one output image at the configured tier.

        1K output is $0.03438 and 2K is $0.068761 (China Beijing). The tier is decided by
        the OUTPUT pixel area, so the configured size is what determines the price — this
        is why the estimate can be exact rather than a guess.
        """
        width, _, height = (self.image_gen_size or "").partition("*")
        try:
            area = int(width) * int(height)
        except ValueError:
            area = 1024 * 1024
        return 0.068761 if area > 2_250_000 else 0.03438

    @property
    def image_gen_cost_cny_per_image(self) -> float:
        """Same price in CNY at the rate the project has been quoting (~7.3)."""
        return round(self.image_gen_cost_usd_per_image * 7.3, 4)

    @property
    def notification_channel_names(self) -> list[str]:
        """Channels to try, in order.

        ``NOTIFICATION_CHANNELS=a,b`` (the spec's future form) wins over the single
        ``NOTIFICATION_CHANNEL``; names are lower-cased and de-duplicated.
        """
        raw = self.notification_channels.strip() or self.notification_channel.strip()
        names = [part.strip().lower() for part in raw.split(",") if part.strip()]
        seen: list[str] = []
        for name in names:
            if name not in seen:
                seen.append(name)
        return seen


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
