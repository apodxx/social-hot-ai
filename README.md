# SocialHot AI

全网热点采集 + AI 二次创作：通过 TikHub 采集小红书 / 微博 / 抖音的热点与关键词搜索结果，
去重入库，用 DeepSeek 分析筛选，改写成三平台文案，通知人工审核，并通过 MCP 暴露给 AI 客户端。

> **系统不会自动发布任何内容。** 管线到"生成草稿 + 通知"为止，发布由人复制文案后自行完成。

> ## 🔑 密钥不进仓库
>
> `.env` 由 `.gitignore` 排除，仓库里只有 `.env.example` 空模板。你需要自己填四家凭据：
> `TIKHUB_API_KEY`、`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`QQ_APP_SECRET`。
>
> 换一台电脑部署时**手工把 `.env` 拷过去**即可，步骤见文末
> 「[密钥与部署](#密钥与部署)」——那里也说明了为什么**不**把加密后的 `.env` 提交进来
> （加密省不掉手工搬运，却要多装一个工具）。
>
> 顺带一提：GitHub 的 push protection **会主动拦截**含明文密钥的推送，所以"先提交看看"
> 这条路本身也走不通。

---

## 快速开始

```bash
cd backend

# 1) 虚拟环境（本机 Python 3.10.18，见下方"与规格的偏差"）
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2) 配置
copy ..\.env.example ..\.env
# 编辑 ..\.env，填入 TIKHUB_API_KEY

# 3) 启动
.venv\Scripts\python -m app.main
```

接口：

```bash
# 三个平台各最多 HOT_LIMIT_PER_PLATFORM 条（默认 50）
curl "http://127.0.0.1:8000/api/hot"

# 只取一个平台 / 只要少量 / 附上原始数据
curl "http://127.0.0.1:8000/api/hot?platforms=weibo&limit=5&include_raw=true"

# 依赖状态（?probe=false 可完全不打网络）
curl "http://127.0.0.1:8000/api/system/status"
```

响应形状（与验收标准一致，另有 `errors` / `counts` / `fetched_at` 附加字段）：

```json
{
  "success": true,
  "data": { "xiaohongshu": [], "weibo": [], "douyin": [] },
  "errors": {},
  "counts": { "xiaohongshu": 0, "weibo": 0, "douyin": 0 },
  "limit_per_platform": 50,
  "fetched_at": "2026-09-18T08:30:00+00:00"
}
```

---

## 实际使用的 TikHub 接口

全部取自官方 OpenAPI 文档（`GET https://api.tikhub.io/openapi.json`，**无需 key、不计费**，
共 1047 个路径），**没有一个是猜的**：

| 平台 | 接口 | 必填参数 | 说明 |
|---|---|---|---|
| 微博 | `GET /api/v1/weibo/web_v2/fetch_hot_search_summary` | 无 | 官方摘要写明"完整热搜榜单(50条)"，一次调用拿满 |
| 微博（备用） | `GET /api/v1/weibo/app/fetch_hot_search` | 无 | `category/page/count/region_name`，主接口失败时使用 |
| 抖音 | `GET /api/v1/douyin/app/v3/fetch_hot_search_list` | 无 | `board_type` 默认 `"0"` |
| 抖音（备用） | `GET /api/v1/douyin/billboard/fetch_hot_total_list` | `page,page_size,type` | `type` 取值来自官方示例 `snapshot`/`range` |
| 小红书 | `GET /api/v1/xiaohongshu/web_v3/fetch_homefeed` | 无 | ⚠️ **首页推荐，不是热榜**（见下） |

### ⚠️ 小红书没有热榜接口

TikHub 当前公开的 **45 个小红书接口我全部枚举过**，没有任何"热榜/趋势榜"接口。
最接近的三个：

- `app_v2/get_creator_hot_inspiration_feed` —— 创作者中心的热点灵感，面向运营后台，非公开榜单
- `app_v2/get_topic_feed` —— 话题下的笔记列表，需要话题 ID
- `web_v3/fetch_homefeed` —— 首页推荐流 ← **Phase 1 采用这个**

因此有两点必须明确，而不是藏起来：

1. 它是**推荐流，不是排行榜**。`rank` 字段是"信息流位置"，不表示热度排名。
2. `num` 参数**上限 40**（官方 schema：`"maximum": 40`）。所以请求 50 条时**必须发第二次分页调用**，
   这会产生额外计费；**每多一页都会在日志里显式打印**。

---

## 成本

- **每一次数据接口调用都计费。** `GET /api/hot` 一次 = 3 次调用（三个平台各一次）；
  小红书要 50 条时是 4 次。
- 失败重试**也会计费**，所以重试次数被 `TIKHUB_MAX_RETRIES` 限制（默认 3，上限 10）。
- `GET /api/system/status` 的探针走的是**账户元数据接口**，不是数据接口。
- ⚠️ **note**：本机 DSH 上另有一道"TikHub 计费闸门"，但它**只拦截 MCP 工具调用**
  （`mcp__tikhub-*`）。本项目用 httpx 直连 REST，**不经过那道闸门** —— 费用保护依赖
  `TIKHUB_MAX_RETRIES`、`HOT_LIMIT_PER_PLATFORM` 和人工确认。

---

## Phase 1 实测结果（真实调用，非构造数据）

用真实 key 跑通，共 **4 次计费调用**（微博 1、抖音 1、小红书 1，另 1 次微博信封抓取）。
原始响应存档在 `backend/tests/fixtures/raw/`，因此下表每一项都可**离线复现**，不必再花钱：

```bash
python scripts/summarize_response.py tests/fixtures/raw/api_hot_response.json
```

### 字段可用性（对真实捕获数据统计）

| 字段 | 微博 (52) | 抖音 (50) | 小红书 (40) |
|---|---|---|---|
| `title` | 52 | 50 | 40 |
| `description`（正文） | **0** | **0** | **0** |
| `author` / `author_id` | 0 | 0 | 40 / 40 |
| `url` | 52（`keyword_url` 相对路径补全） | 0 | 40（由真实笔记 id 推导） |
| `publish_time` | 0 | 49（`event_time`） | 0 |
| `rank` | 52（列表顺序） | 50（列表顺序） | 40（信息流位置） |
| `hot_value` | **0**（`heat` 全为空字符串） | 50 | 0 |
| `likes` | 0 | 0 | 40 |
| `comments` / `shares` / `collects` | 0 | 0 | 0 |
| `cover_url` | 0 | 49（`word_cover.url_list`） | 40（`cover.url_default`） |
| `video_url` | 0 | 0 | 0 |

**三个平台的列表接口都不返回正文。** 这正是规格里"获取详细内容"必须存在的原因 ——
Phase 3/4 不可能只靠列表接口做分析和二创。

### 真实数据带来的三个修正

1. **微博的标题字段是 `keyword`**，不是 `word`/`title`。第一版适配器因此产出 **0 条**，
   是真实验证当场抓出来的 —— 这也是坚持"必须跑真 API"的价值。
2. **微博 `rank` 不唯一**：真实数据里两个条目 rank 都是 0（多个置顶项，`is_top=true`）；
   抖音的 `position` 同样按榜单分区重启。因此 `rank` 统一取**列表顺序**，
   provider 自己的 rank/position 保留在 `raw_data` 里。
3. **抖音封面字段是 `word_cover.url_list`（数组）**，发布时间是 `event_time`。

### 已知限制

- **小红书单次最多 40 条**（`num` 的官方上限就是 40），且分页游标字段名尚未确认，
  所以一次运行最多 40 条。要凑满 50 需先确认真实游标字段（再多 1 次调用）。
- 小红书**没有热榜接口**，`rank` 是信息流位置，**不是热度排名**。
- 微博 `heat` 字段存在但全为空，热度只能从 rank 间接体现。
- 抖音榜单首条 `hot_value` 为 0（疑似置顶位），Phase 2 的"数据异常"过滤规则需要处理它。

---

## Phase 2：数据库、去重与话题聚合

### 数据库

用 PostgreSQL 16。本机**没有 Docker、也没有 psql 客户端**，但有一个正在运行的 5432 实例（`scram-sha-256`，需要密码）。
为了不动你的凭据，Phase 2 用 PostgreSQL 自带的 `initdb` 在项目里建了一个**独立集群**：

| | |
|---|---|
| 数据目录 | `social-hot-ai/.pgdata`（已 gitignore） |
| 端口 | **55432**，只监听 `127.0.0.1` |
| 认证 | `trust`（仅本地开发） |
| 数据库 | `social_hot_ai` |
| 你原有的 5432 服务 | **未做任何改动** |

```bash
# 启动 / 停止（按你的安装位置调整路径）
"D:\Program Files\PostgreSQL\16\bin\pg_ctl.exe" -D .pgdata -l pg-server.log start
"D:\Program Files\PostgreSQL\16\bin\pg_ctl.exe" -D .pgdata stop

# 迁移
cd backend && .venv\Scripts\python -m alembic upgrade head
```

> 踩过的坑：**日志文件不能放在数据目录里**，否则崩溃恢复时会因共享冲突卡在 `starting up`。

### 表结构

- **`hot_contents`** —— 规格列出的字段全部包含，另加两列**派生去重键**
  `title_normalized` / `url_normalized`（每次 upsert 重算，不会与 `title`/`url` 漂移）与 `topic_group_id`。
- **`topic_groups`** —— `topic` / `topic_normalized` / `summary` / `platforms` / 时间戳。
  `summary` 在 Phase 2 **保持为空**（由 Phase 3 填），规格里展示的 `related_contents` 由成员数**推导**、不存列，避免漂移。

### 四层去重

| 层 | 规则 | 处理 |
|---|---|---|
| 1 | `platform + platform_content_id` | **刷新**（不是丢弃） |
| 2 | URL（剥离追踪参数后） | 丢弃 |
| 3 | 标题相似度（**限同一平台内**） | 丢弃 |
| 4 | 跨平台话题聚合 | **全部保留**，聚成 `TopicGroup` |

两个决定关系到数据正确性，值得单独说明：

1. **第 1 层是"刷新"而非"丢弃"。** 热度榜每次运行 rank/hot_value 都在变；已知条目若被当重复丢掉，
   库里的数值就永远冻结在首次抓到的值。第一版正是这么写的，被 `test_second_run_is_idempotent` 抓了出来。
2. **第 3 层必须限定在同一平台内。** 微博与抖音上的同名条目是**两条不同内容、同一个话题** —— 正是第 4 层的聚合对象。
   跨平台去重会删掉其中一个平台的数据点，第 4 层就无物可聚。

### 新增接口

| 方法 | 路径 | 计费 |
|---|---|---|
| POST | `/api/hot/collect` | ⚠️ **会计费**（每平台至少 1 次调用） |
| GET | `/api/hot/stored` | 免费 |
| GET | `/api/hot/topics` · `/api/hot/topics/{id}` | 免费 |

**零费用**跑通整条 Phase 2 路径：

```bash
cd backend
.venv\Scripts\python scripts\load_fixtures_into_db.py --reset
```

### 实测结果（真实 PostgreSQL + 真实捕获数据）

```
weibo: 52 raw -> 52 normalised
douyin: 50 raw -> 50 normalised
xiaohongshu: 40 raw -> 40 normalised
dedup: 142 in, 142 kept, 0 removed
upsert: 142 inserted, 0 updated
hot_contents=142   by_platform={'douyin': 50, 'weibo': 52, 'xiaohongshu': 40}
```

### 已知限制：第 4 层是词面匹配，不是语义匹配

实测这份快照的跨平台标题相似度：超过 0.25 的**只有 7 对**，最高 **0.348**，远低于 0.82 的聚合阈值。两个方向都要看清楚：

- **漏聚合**：抖音「中国海警正告菲方停止侵权挑衅」与微博「中国海警回应菲船只碰撞我海警艇」
  **是同一个真实事件**，相似度却只有 0.345 → 不会聚合。
- **误聚合风险**：得分最高的那对（0.348）是抖音「JackeyLove：世界赛我们来了」与微博「我们来了 刘雯」—— **毫无关系**。

所以**降低阈值换来的是误聚合，而不是更多正确聚合**。真正需要的是语义匹配，而那正是 Phase 3 DeepSeek 分析的职责。
Phase 2 保持词面匹配，并把 `topic_groups` 作为 Phase 3 语义合并的输入。

想看第 4 层在真实库上生效（注入**明确标注为合成**的孪生条目）：

```bash
.venv\Scripts\python scripts\load_fixtures_into_db.py --reset --demo-cross-platform
# -> topic_groups: 2，均为跨平台，各 2 个成员
```

### 一次真实的数据完整性事故（已修复）

入库的部分中文是乱码，**只有检查真实存储的字节才会发现**：
`0xe9 0xa1 0xba` 这类 Latin-1 码点，就是 UTF-8 字节被按 Latin-1 解码的结果。

- **根因**：Phase 1 我用 PowerShell `Invoke-WebRequest` 抓 `/api/hot` 的响应，
  PS 5.1 在 `Content-Type` 没有 charset 时按 ISO-8859-1 解码 → 中文全部损坏。
- **影响范围**：`douyin.json`、`xiaohongshu.json`、`api_hot_response.json`（`weibo.json` 走 httpx，完好）。
- **修复**：Latin-1 对 0x00–0xFF 是双射，**无损可逆**，无需重新调用 →
  `scripts/repair_fixture_encoding.py --apply` 修复了 **521 处**。
- **防回归**：`test_fixtures_are_not_encoding_mangled`（修复前确实失败，修复后通过）。
- **规则**：抓 TikHub 响应**必须走 httpx**（`scripts/discover_raw.py`），不要用 PowerShell。

---

## Phase 3：DeepSeek 热点分析与筛选

流程严格按规格 §十二（规则过滤）、§十四（分析）、§十五（筛选）、§三十五（成本控制）：

```
142 条已入库
  ↓ 规则初筛（免费，不花 token）
141 条
  ↓ 平台均衡取候选（每平台各 10 条）
30 条
  ↓ DeepSeek，按批调用（3 批 × 10 条）
结构化 JSON（每条一个对象）
  ↓ 筛选：recommended 且 confidence ≥ 0.5 且非重复
2 条入选 → 交给 Phase 4 二创
```

### 成本控制（实测数字）

| 手段 | 说明 |
|---|---|
| 规则初筛在前 | 广告/营销/空内容/异常计数/低质量标题在**花 token 之前**被剔除 |
| 批量调用 | 30 条 = **3 次**请求，而不是 30 次（`ANALYSIS_BATCH_SIZE=10`）|
| 提示词不带 `raw_data` | 只发 index/platform/title/hot_value/rank，原始 JSON 是留给调试的，不该付费重读 |
| 分析结果复用 | `ANALYSIS_REUSE_HOURS=24` 内重复运行**不会再花一次钱** |
| 平台均衡取样 | 30 条里每平台各 10 条；否则抖音 300 条榜单会挤掉另外两个平台 |

**实测：30 条 = 3 次调用 = 12,299 tokens（prompt 3,060 / completion 9,239）≈ ¥0.08。**

### 实测输出样例（真实 DeepSeek 返回，存于 `ai_analyses`）

- 微博「GEO终于有标准了」→ `confidence 0.82`、`needs_verification=true`
  - summary：**"原内容称 GEO 终于有了标准；GEO 在圈内多指生成式引擎优化，但具体标准发布方与内容目前无法确认。"**
  - content_angle："用大白话科普 GEO 是什么、对普通人和博主意味着什么。"
- 微博「制造业技术要自己掌握」→ `confidence 0.60`、`needs_verification=true`
- 被拒示例：抖音「美军一架F16战斗机坠毁」→ `recommended=false`、`confidence 0.25`、
  account_fit："部分相关，可做战机科技科普，但军事新闻敏感需谨慎处理。"

**规格 §19 的"区分原内容声称与目前可确认"在真实输出里生效了** —— 30 条中有 **21 条**被标记
`needs_verification=true`，这与热榜以新闻事件为主的事实相符。

### 账号定位

`account_profile.json`（项目根目录，可随时编辑，每次运行重新读取）：领域、受众、风格、语气、
偏好话题、禁止项。它会渲染进提示词，因此"是否适合当前账号"是按**你的**定位判断的，而不是通用标准。
文件缺失只会告警（分析退化为通用判断）；**文件格式错误会明确报错（HTTP 422），不会静默降级**。

### 新增接口

| 方法 | 路径 | 计费 |
|---|---|---|
| POST | `/api/analysis/run` | ⚠️ **消耗 DeepSeek token** |
| GET | `/api/analysis`（`?selected_only=true`）| 免费 |
| GET | `/api/analysis/{hot_content_id}` | 免费 |
| GET | `/api/analysis/profile/current` | 免费 |

`/api/system/status` 现在也报 DeepSeek：探针走 `GET /models`（**不计 token**），
并校验"配置的模型确实在该账号的模型列表里"。

### 表 `ai_analyses`

规格列出的字段全部包含（`summary` / `why_hot` / `content_angle` / `recommended` / `confidence` /
`needs_verification` / `topic_group_id`），另加四类补充列，都有明确用途：

- `topic` / `discussion_points` / `account_fit` / `is_duplicate` —— §14 要求模型回答的问题，不落库就等于没回答
- `selected` —— "5~10 条进入二创"这一步的结果
- `model` / `prompt_tokens` / `completion_tokens` / `raw_response` —— AI 花费必须可逐行审计，
  错误判断也必须能对着原始回答排查
- `hot_content_id` 唯一：重新分析是**更新**而不是堆积版本

`topic_groups.summary` 在这一阶段被填充（Phase 2 刻意留空）：取该话题组内**置信度最高**成员的结论，
不另外生成句子。

### 语义话题合并（默认关闭）

Phase 2 的第 4 层是词面匹配，实测最高跨平台相似度只有 0.348（阈值 0.82），因此
「中国海警正告菲方停止侵权挑衅」与「中国海警回应菲船只碰撞我海警艇」这类**同一事件的不同措辞不会被聚合**。
`ANALYSIS_SEMANTIC_MERGE=true` 会多花**一次廉价调用**，让模型判断入选条目里哪些是同一事件，
然后复用 Phase 2 的持久化路径重新分组。默认关闭，因为它是一次额外计费请求。

### 又一次 PowerShell 编码事故（同一个坑，第二次）

我用 PowerShell 抓 `/api/analysis/run` 的响应来查看结果，输出里的中文又是 `\u00e7\u00bb\u0088`
这种 Latin-1 码点。**这次不是应用的问题**：直接查数据库确认 **30 行里 0 行乱码**，
标题码点正确（`0x5236` = 制）。是 `Invoke-WebRequest` 在解码响应时又按 ISO-8859-1 处理了 UTF-8。

**因此规则升级为硬性要求：任何捕获/查看 TikHub 或本 API 响应的动作都必须用 Python（httpx），
不要用 PowerShell。** 数据库与 API 返回的数据本身是 UTF-8 正确的。

### 已知限制

- **入选率低是正常的**：这份快照的热榜以社会/娱乐新闻为主，而账号定位是 AI/科技，30 条里只有 2 条入选。
  这是"定位匹配"在起作用，不是筛选失灵。
- **模型名必须先查**：这个账号只有 `deepseek-flash` 与 `deepseek-v4-pro`，`deepseek-chat` 会 404。
  `/api/system/status` 会替你校验，配置错误会直接报 `error` 而不是静默失败。
- **`estimated_cny` 是估算**，按公开单价折算；权威数字以 DeepSeek 账单为准，接口同时返回原始 token 数。

---

## Phase 4：AI 二创

### 产出（规格 §17 的六项全部覆盖）

| 部分 | 字段 |
|---|---|
| 通用 | `summary`（发生了什么）、`why_hot`（为什么受关注）、`angle`（二创方向）|
| 小红书 | 标题 + 正文 + **结尾互动** + 话题标签 |
| 微博 | **开头** + 正文 + 话题标签 |
| 抖音 | 3秒 Hook + 30秒口播脚本 + 画面建议 + 字幕 + 结尾 CTA |

> 规格 §21 的 `ai_rewrites` 列清单**漏了** `xiaohongshu_ending`、`weibo_opening`、`douyin_subtitles`，
> 而 §17 明确要求这三项；也没有 §20 要求的 `status`。按"需求优先"补齐，均已在
> `app/models/ai_rewrite.py` 的模块注释里标注为增补列。

### 实测输出（真实 DeepSeek，存于 `ai_rewrites`）

两种平台语体确实不同，且注意最后一条——模型**主动承认**输入只有标题：

- 小红书标题：「GEO 是啥？搞懂再刷手机」／正文用口语分段讲清 SEO 与 GEO 的区别，
  结尾互动「你平时找信息，是打开 App 搜，还是直接问 AI？评论区聊聊～」
- 微博开头：「GEO 这个词，今天突然冲上热搜。」——短句直给
- 抖音 hook：「GEO 这个词你还没搞懂，它已经上热搜了。」+ 双人分屏画面建议 + 字幕要点
- 第二条的抖音脚本开口就是：**「这条热搜只有标题，没有正文，所以今天咱不编细节，只聊三层。」**

### §18 反机械改写：**由代码执行，不只是提示词**

`check_mechanical_copy()` 做的是"在没有原文正文的前提下**能测**的那部分"：

| 检查 | 触发条件 | 标记 |
|---|---|---|
| 标题是否根本没改 | 生成标题与原标题相似度 ≥ 0.95 | `title_not_rewritten` |
| 三个平台是不是同一段话 | 小红书正文与微博正文相似度 ≥ 0.90 | `platform_versions_identical` |
| 是不是占位内容 | 各平台正文低于最小长度 | `too_short:*` |

命中任一项 → **带上失败原因自动重写一次**；若第二次仍不合格，保留两次中**问题更少**的那次，
并强制 `NEEDS_REVIEW`。实测 `copy_similarity` 为 0.73 / 0.64，均未触发。

> 诚实说明其**能力边界**：这些平台的输入是**热词、没有正文**，所以真正的"抄没抄"无法判定。
> 等 §16 详情抓取落地后，同一个函数才能升级为真正的文本重合比对。

### §19/§20 状态链：**模型不决定 `status`**

`compute_status()` 由代码决定发布就绪状态，且有一条硬规则：

> **Phase 3 的分析若标记了 `needs_verification`，二创永远不能是 `READY_TO_PUBLISH`。**

因为"事实还没被人工确认"这件事不会因为换了个表达方式就消失。实测 2 条全部为 `NEEDS_REVIEW`，
`verification_note` 里保留了"原内容声称／目前可确认"的区分。存储的 `needs_verification` 是
**有效值**（模型说法 OR 分析结论），与 `status` 语义一致，避免下游通知出现自相矛盾。

### ⚠️ §16「获取详细内容」尚未实现（本阶段最重要的缺失）

规格的流程是「筛选 → **获取详细内容** → 二创」。我把三个平台的可行性查清了（见下表），
但**没有实现抓取**，因为响应结构必须各做一次真实抓取才能确认，而我不想写无法验证的解析代码：

| 平台 | 可行性 | 端点 | 参数 | 我们是否已有 |
|---|---|---|---|---|
| 小红书 | ✅ 可以 | `web_v3/fetch_note_detail` | `note_id` + `xsec_token` | **两者都有**（id 即条目 id，`xsec_token` 在 `raw_data` 里）|
| 抖音 | ⚠️ 语义不同 | `index/fetch_hot_detail` | `topic_name` | 有（用标题）—— 但它是"话题指数详情"，不是帖子正文 |
| 微博 | ❌ 没有帖子 id | 只能 `web_v2/fetch_realtime_search` | `query` | 热词是关键词，只能"搜相关帖子当素材" |

代价：每入选条 1 次 TikHub 调用。要落地需要 **3 次真实抓取**（每平台 1 次）确认结构，
与我 Phase 1 的做法一致。**在此之前，二创的输入只有标题与热度**——模型被要求不许编造细节，
并在输出里体现这一点（见上面的抖音脚本）。

### 成本（实测）

| 运行 | 调用 | tokens | 估算 |
|---|---|---|---|
| 第一次 | 2 | 6,852 | ¥0.044 |
| 补跑失败项 | 1 | 2,556 | ¥0.015 |
| **合计** | **3** | **9,408** | **≈ ¥0.06** |

`REWRITE_REUSE_HOURS=24` 内重复运行不再花钱（有测试证明第二次 0 次调用）。

### 又一次环境陷阱：本机代理让"本地调用"返回 502

`POST /api/rewrite/run` 第一次返回 **502、0 字节、服务端毫无日志**。用最小请求定位后确认：

| 方式 | 结果 |
|---|---|
| httpx 默认（`trust_env=True`）| **502，请求根本没到服务器** |
| httpx `trust_env=False` | 200 |
| urllib | 200 |

原因：`os.environ` 里没有代理变量，但 Windows 注册表里 `ProxyEnable=1`、`ProxyServer=127.0.0.1:7890`
（本机代理），httpx 通过 `urllib.getproxies()` 读到了它，而该代理**不转发 localhost**。

- 应用自身的 client 只访问外网（走代理正常），**无需修改**。
- **验证本地接口时必须 `trust_env=False`**，与"抓响应必须用 Python"是同一条纪律的延伸。

### 一次配置 bug：`REWRITE_MAX_TOKENS=3000` 把 JSON 截断了

第一次运行的 2 条里有 1 条失败，报 `unparseable JSON`，错误信息里能看到 JSON 停在
`"xiaohongshu":{"title":` —— 三个平台版本 + 标签根本装不进 3000 token，模型撞上限、输出被腰斩。

修复两处：默认值提到 **6000**；并且**显式检测 `finish_reason == "length"`**，报
"output was truncated at REWRITE_MAX_TOKENS…" 这种可操作的错误，而不是让下游看到莫名的解析失败
（重试同一个上限只会再截断一次）。

### 已知限制

- **没有正文就只能做框架层创作**：模型不会编造数字/人名/机构，代价是内容偏"角度与框架"而非"事实报道"。
- **入选数量决定成本**：要二创 10 条就是 10 次调用。上限由 `ANALYSIS_MAX_SELECTED` 控制。

---

## Phase 5：定时任务（APScheduler）

### 调度

`HOT_FETCH_TIMES=08:00,12:00,18:00` → 每个时间点注册一个 cron job，时区 `SCHEDULER_TIMEZONE`（默认 `Asia/Shanghai`）。
三个参数是为"无人值守"准备的：

| 参数 | 作用 |
|---|---|
| `max_instances=1` | 慢运行不会被下一次触发叠加 |
| `coalesce=True` | 进程停机期间错过的触发只补跑**一次**，不是每个错过的时刻各跑一次 |
| `misfire_grace_time=300` | 错过不超过 5 分钟仍然执行 |

实测（真实启动）：

```
job hot-pipeline-18:00: next=2026-09-18T18:00:00+08:00  trigger=cron[hour='18', minute='0']
job hot-pipeline-08:00: next=2026-09-19T08:00:00+08:00
job hot-pipeline-12:00: next=2026-09-19T12:00:00+08:00
```

### 完整 Pipeline（规格 §22）

```
fetch → analyze → detail → rewrite → notify
```

- `fetch` / `analyze` / `rewrite` 已实现（**计费**）
- **`detail` 与 `notify` 未实现**，运行报告里明确写为 `skipped` 并附原因 —— **不假装跑过**

### 步骤级容错（这是本阶段的核心要求）

| 设计 | 为什么 |
|---|---|
| **每个阶段用独立的短会话** | 规格要求"一个步骤失败不能毁掉已完成的工作"。一个长事务会在失败时全部回滚，所以每阶段各自提交，失败时任务状态为 `partial`，已完成的结果落库 |
| 阶段异常全被捕获，**永不向外抛出** | 调度器不会被一次失败打垮 |
| `steps` 列记录每阶段的 status / 耗时 / 是否计费 / 错误 | "哪一步失败了、花了多少" —— 只查数据库就能回答 |

### 任务记录与防重复执行

`tasks` 表（§21 字段全含，另加 `trigger` / `steps` / `summary` / `duration_ms`）。实测一次真实运行的行：

```
数据库：hot_contents=142  ai_analyses=60  selected_analyses=4  ai_rewrites=4  ready_to_publish=0
最近任务：task 1   manual   success   58,072ms   14,588 tokens
          ├ analyze   success   billed   37,876ms
          └ rewrite   success   billed   20,190ms
```

**"这次运行花了多少"由任务行自己回答**，不需要去翻日志。

- **运行中的任务会阻止新的运行**（返回 `skipped` 并说明是哪个任务在跑），且不会新建任务行
- 超过 `TASK_STALE_MINUTES`（默认 30）仍标记 `running` 的任务会被判定为崩溃并标记失败 ——
  **否则一次崩溃会让调度器永久静默失效**
- 两条路径都有测试覆盖（`test_a_running_task_blocks_a_second_run` /
  `test_a_stale_running_task_is_failed_and_does_not_block`）

### 手动执行接口（§23）

| 方法 | 路径 | 计费 |
|---|---|---|
| POST | `/api/pipeline/run?stages=fetch,analyze,rewrite` | ⚠️ 取决于所选阶段 |
| GET | `/api/pipeline/tasks?status=` | 免费 |
| GET | `/api/pipeline/schedule` | 免费（含 next_run_time）|

### ⚠️ 一次我预测错误的成本（如实记录）

我在验证前说这次是"零费用"，**实际花了 14,588 tokens ≈ ¥0.09**。

原因：此前只有 **30/142** 条被分析过，`rows_needing_analysis` 正确返回了**尚未分析的 112 条**，
于是又分析了其中 30 条并选中 2 条去做二创。**系统行为是对的，是我"都已经分析过了"的假设错了**。
教训：`ANALYSIS_REUSE_HOURS` 只复用**已分析过**的条目，它不等于"运行一定免费"。

### 累计 AI 花费（本会话）

| 运行 | tokens |
|---|---|
| Phase 3 首次分析 | 12,299 |
| Phase 4 二创 + 补跑 | 6,852 + 2,556 |
| Phase 5 实跑（analyze + rewrite）| 14,588 |
| **合计** | **36,295 ≈ ¥0.22** |

---

## §16 详情抓取（已实现，**默认关闭**）

### 三个平台是三种不同的"详情"，不能互换

| 平台 | 端点 | 参数 | 语义 |
|---|---|---|---|
| 小红书 | `web_v3/fetch_note_detail` | `note_id` + `xsec_token` | **真正的笔记详情**；两个参数我们都有（id 是条目 id，`xsec_token` 在 `raw_data` 里）|
| 抖音 | `index/fetch_hot_detail` | `topic_name` | 话题指数详情：实测给出 **50 条相关作品**（标题/作者/点赞）+ `trend_item` 话题指数趋势 |
| 微博 | `web_v2/fetch_realtime_search` | `query` | 热词**没有帖子 id**，只能搜索；返回的帖文作为素材 |

成本：**每条 1 次计费调用**，因此 `DETAIL_FETCH_ENABLED=false` 默认关闭；
开启后管线报告里会给出 `billed_calls`。

### 一次真实抓取暴露了我第一版的 bug

第一版用"payload 里最长的字符串"当正文兜底。真实抓取显示**最长的字符串是 TikHub 的 215 字缓存说明**
（`“This response is cached and accessible via…”`）—— 它会被当作素材喂给 AI。
现在 `details.py` 里**每一个字段都来自真实响应**，并在模块注释里写明了这个教训。

### §16 的收益：实测同一条目的前后对比

同一条微博热词「GEO终于有标准了」，抓正文前 vs 后：

| | 摘要结论 |
|---|---|
| **只凭标题** | 「…发布方是谁、具体内容是什么，目前无法确认」|
| **抓到正文后** | 「…**该热搜下大量帖文与AI无关，疑似蹭标签**」|

二创也随之质变：

> 小红书正文：「今天刷到 #GEO终于有标准了# 挂在热搜上，点进去一看……好家伙，一半是海边日落、梭子蟹炒年糕，还有机器人毕业上岗，跟 AI 八竿子打不着。」
>
> 抖音脚本：「GEO 冲上热搜，我点进去想学点东西，结果刷出来的是海边日落、梭子蟹炒年糕，还有个哥们花 29 块 9 买婚服。跟 AI 一点关系没有，纯纯蹭标签。」

**模型用抓到的素材推翻了原始前提** —— 只看标题永远做不到这一点。
`verification_note` 也变得更具体：明确写出"抓到的帖文里没有标准发布方、文件名或条款内容"，
这正是 §19 要求的事实区分，而现在它有真实材料支撑。

---

## Phase 6 通知（已实现）

### 通道

| 通道 | 状态 |
|---|---|
| `log` | ✅ 只写日志。用于**零凭据验证**整条通知链路（格式、拆分、回退、管线阶段）|
| `wechat` | 企业微信机器人 webhook（官方 API）。请求形状、`errcode` 判定、拆分、传输失败均以 respx 验证 |
| `qq` | **QQ 开放平台 Bot（官方 API）—— 已真实验证**（文本、图片分片上传、被动回复、**主动推送**全部实测通过） |

**QQ 通道的验证结论（Phase 12 之后实测）**：

#### ⚠️ 四个"看起来像别的问题"的坑

**① `GROUP_MESSAGE_CREATE` 与 `GROUP_AT_MESSAGE_CREATE` 是两种事件。**
群消息有两种事件类型：平台判定为"@机器人"时发 `GROUP_AT_MESSAGE_CREATE`；**群里的普通
消息**发 `GROUP_MESSAGE_CREATE`（@ 标记以文本形式出现在 `content` 里）。
**实测本机器人收到的是后者**，而最早的代码只处理前者 → **机器人对 @ 完全没反应**。

这个 bug 骗了很久，因为**症状和"事件根本没送达"一模一样**：毫无反应、日志一行没有。
而当时的诊断脚本只打印 `GROUP_AT_MESSAGE_CREATE`、其它事件静默忽略，于是两种完全不同的
故障显示成同一个现象。**改成打印所有事件类型后，一轮就定位了**——诊断工具不该假设你知道
问题是什么。

代价与对策：订阅普通群消息意味着**群里每条消息都会推过来**，所以代码里加了
"没 @ 机器人就忽略"，否则它会对每句话插嘴。

**② 机器人 ID 是十六进制的，`\d+` 匹配不了。**
@ 标记形如 `<@D0F1E2A80ADFCE7447691C7105532D67>`，而第一版正则写成 `<@!?\d+>`
（只认数字）→ 标记整段留下、指令变成 `<@D0F1…> 知识科普`，谁也认不出来。
正确写法是 `<@!?[0-9A-Za-z]+>`。

**③ WebSocket 会被 Windows 注册表的系统代理劫持。**
项目里处处 `trust_env=False` 绕开本机代理，但 **`websockets` 走的是另一条路**：它通过
`urllib.getproxies()` **读注册表**的 Internet Settings。实测即使 `HTTP_PROXY` 等环境变量
全空，`urllib.getproxies()` 仍返回 `{'https': 'http://127.0.0.1:7890'}`，于是 WebSocket
被路由到本机 Clash——**握手能成功（打印"已就绪"），但事件流不通**。

修复：`websockets.connect(..., proxy=None)`（三个用网关的脚本都加了）。验证方法是看进程的
TCP 连接：应当直连腾讯 IP，**不应出现 `127.0.0.1:7890`**。这也解释了之前那次
`ConnectionClosedError: no close frame received or sent`——很可能是代理掐掉了长连接。

**④ 自定义菜单不必去控制台配。**
发消息接口的 `keyboard` 字段可以给消息挂**可点击的指令按钮**（`action.type=2`）。
**实测它能挂在普通文本消息上**（`msg_type=0` + `keyboard` → 200），所以不需要 Markdown
模板权限（那会撞 `304036`/`304127`）。好处是**按钮定义跟着代码走**，换机器部署不会丢，
而控制台的指令面板要人工重配。注意 `label` 上限 10 字符，且 `enter`（点一下直接发送）
**仅单聊可用**——群里是"填入输入框，还需按发送"。

#### 其它实测结论

- **文本、图片（分片上传 4 步）、被动回复、主动推送都实测通过**。早期标注的
  "未经真实验证" 已过期。

#### function calling：随便说一句话也能办事

QQ 机器人接了一层**意图识别**（`app/services/ai/agent.py`）：认不出的 @ 消息不再回一份
帮助文案，而是让模型**选工具**。例如贴一段文字说"帮我改成小红书文案"，会走
`convert_text_to_note`。

设计上刻意的取舍：

- **模型只选工具、不写文案。** 文案交给既有管线生成，质量与成本仍在原来的地方受控。
- **计费工具复用同一道额度闸门**，模型无权绕过。
- **选不出工具时回退到帮助文案**，不硬猜——猜错比不猜更糟。
- 只有"认不出的消息"才走路由（约 ¥0.0025），明确指令不会多花这个钱。

#### ⚠️ `deepseek-flash` 是推理模型，思考 token 会吃光额度

实测：`max_tokens=1200` 时出现过 `finish_reason='length'`、`completion_tokens=1200`、
而 **`content` 是空的**——思考把额度全用光，一个字的正文都没留下。同样参数下一次又会成功
（思考较短），**所以是概率性的，不能靠调参碰运气**。

修法两条：**额度给足**（4096），以及**把"空正文"当成明确失败**并区分原因
（撞额度 vs 模型没说话）。意外收获：额度调大后**更便宜**了（¥0.0100 → ¥0.0020），
因为模型不再撞天花板空转。

#### ⚠️ 小红书链接的配图必须走 TikHub，且两个端点行为不同

小红书分享短链会重定向到 `www.xiaohongshu.com/login?...`，匿名抓取只拿到登录页
（实测 HTML 里 `<img>` 数量为 **0**）。但**重定向后的地址里同时带着 `note_id` 与
`xsec_token`**，拿这两个调 TikHub 即可。

**两个端点的返回完全不同**（同一个笔记实测）：

| 端点 | 返回长度 | 含 `xhscdn` 图片地址 |
|---|---|---|
| `web_v3/fetch_note_detail` | 8303 字符 | **0 个** |
| `app_v2/get_image_note_detail` | 21308 字符 | **3 个** ✓ |

名字里那个 `image` 是关键。另外**收集图片地址时判据要放宽**：第一版要求键名含
`url`/`image`/`pic`，实测会漏——地址也可能藏在 `info_list` 之类看不出用途的键下面。
现在只要"http 开头 + 域名是 xhscdn"就收。

#### ⚠️ 判断"有没有链接"不能用 `startswith`

实测用户会把链接夹在句子里：

```
专升本326分上岸公办，贡献下我的开智教程 https://xhslink.cn/o/xxx 去【小红书】逛逛…帮我转成文案
```

`source.startswith(("http://","https://"))` 为**假** → 抓图分支整个被跳过 →
用户看到的症状是"**只有文案没有图片**"，而看起来像抓图功能坏了。
必须用正则从任意位置找链接。
- **文字与图片无法合并在一条消息里**：`msg_type` 决定哪个内容字段生效（0→`content`、
  7→`media`），所以「图文」= 1 条文本 + N 条图片。`msg_seq` 在**同一 `msg_id` 内必须递增**，
  且序号空间**跨多次发送共享**——分段发送时忘传 `start_seq` 会让第二段从 1 重来并报
  `40054005 消息被去重`。
- **实测发现一个文档没写的边界**：图片（`msg_type=7`）之后再发文字（`msg_type=0`）会被
  判重复。两组数据：文字+2 图（共 3 条）时第 4 条失败；文字+1 图（共 2 条）时第 3 条失败。
  两次失败的都是"图片之后的那条文字"。所以**一次回复只用一条文本 + 若干图片**，
  不要分多段文本。
- **文字子频道用 HTTP 发不出去**：官方要求机器人保持 WebSocket 在线，所以只用群聊与单聊。
- **主动消息**：`40034105 主动消息失败, 无权限` 出现过，在完成**个人认证**并允许客户端
  主动发送后变为可用。**但具体是哪一步生效的无法确认**——`/v2/users/@me` 返回
  `404 不支持的调用`，查不到机器人权限状态，只能靠实际发送探测。
  主动消息的限制是**频控**（单关系 20/qpm、每群每天 1000 条），一次推送条数要留意。
- **`group_openid` 在控制台看不到**：从 `GROUP_ADD_ROBOT` 事件取（`scripts/watch_qq_events.py`）。
  `GET /v2/users/@me/groups` 在该机器人上返回 404。

### 规格要求与实现

| 要求 | 实现 |
|---|---|
| §24 只用官方渠道 | 只有企业微信机器人 webhook 与 QQ 开放平台 Bot；**不含个人号协议/逆向** |
| §25 消息格式 | `format_digest()`：原标题 / 热点原因 / 二创方向 / 三平台内容 / 原始链接 / 来源作者，逐条编号（①②③…）|
| §19 核验提示 | `⚠️ 该内容需要人工核实` + `verification_note` |
| §20 状态 | `NEEDS_REVIEW（未通过自动审核，请勿直接发布）` + 风险标记 |
| §26 失败回退 | 微信失败**自动尝试 QQ**；任何通道抛异常都被捕获并记录，**永不中断管线** |
| §28 长消息 | `split_message()` 按段落拆分并加 `（i/n）` 标记。**实测 5666 字 → 自动拆成 4 条** |
| §27 测试接口 | `POST /api/notification/test` 发送规格指定的测试文案 |

### 实测（真实运行，零费用）

```
notify  success  6ms  enabled=True ok=True rendered_chars=5666
        channels=[{"channel":"log","ok":true,"parts":4,"detail":{"note":"logged only; no external delivery"}}]
```

### 一个我读错的配置语义

`REWRITE_REUSE_HOURS=0` 的语义是"**永不重新改写**"（只处理从未二创过的条目），
不是"强制重写"。我在验证 §16 时就因为读错它而让 rewrite 阶段 `considered=0`。
`ANALYSIS_REUSE_HOURS` / `REWRITE_REUSE_HOURS` 的 `0` 都表示"不要再做"，**不是**"立刻重做"。

### 累计 AI 花费（本会话）

| 运行 | tokens |
|---|---|
| Phase 3 首次分析 | 12,299 |
| Phase 4 二创 + 补跑 | 6,852 + 2,556 |
| Phase 5 实跑 | 14,588 |
| §16 强制的正文感知重写 | 3,965 |
| **合计** | **40,260 ≈ ¥0.24** |

TikHub 侧：4 次数据调用 + 3 次详情抓取 + 1 次详情阶段实跑 = **8 次计费调用**，另有若干免费元数据调用。
Phase 9 又加了 3 次搜索探测 + 1 次抖音重跑 + 1 次三平台搜索 = **每次约 $0.0078**（实测，见 Phase 9）。

---

## Phase 7：Vue 后台管理界面

### 一条命令打开

```bash
cd backend
.venv\Scripts\python -m app.main
# 浏览器打开 http://127.0.0.1:8000/
```

前端由 **FastAPI 自己托管**（`frontend/dist`），与 API **同源**：只有一个地址，不需要
CORS 配置，也不需要第二个服务。`npm run dev` 只用于调 UI（Vite 把 `/api` 代理到 8000）。

先构建一次：

```bash
cd frontend
npm install
npm run build
```

**构建产物必须在进程启动前存在**：`_mount_frontend()` 在 `create_app()` 时判断
`frontend/dist` 是否存在。没有产物时后端照常提供纯 API，并在启动日志里写明
（`frontend build not found ... API only`），不会静默 404。

### 技术栈与页面

Vue 3（`<script setup>`）+ Vite 6 + Element Plus（中文 locale）+ Pinia + Vue Router + Axios。

| 路由 | 页面 | 做什么 |
|---|---|---|
| `/` | 总览 | 今日采集、各平台条数、AI 分析/入选/待审核计数、依赖组件状态、定时任务与下次运行时间、最近任务、带成本提示的流水线触发 |
| `/hot` | 热点列表 | 平台/关键词/AI推荐/入选 四种筛选，一页同时显示分析结论与二创状态；抽屉展示完整分析与风险标记 |
| `/topics` `/topics/:id` | 话题聚合 / 话题详情 | 去重第 4 层的聚类结果与成员条目 |
| `/rewrites` `/rewrites/:id` | 二创审核 | 三平台文案的完整审核页，按平台一键复制 |
| `/tasks` | 任务记录 | 每次运行的阶段结果、耗时、错误、汇总 JSON |
| `/settings` | 设置 | 环境变量分组编辑、账号定位、通知通道状态与测试、系统状态 |

### 三条贯穿 UI 的安全约束

1. **花钱必须二次确认。** 任何调用 TikHub 或 DeepSeek 的按钮（采集/AI分析/详情/二创/
   流水线）都先弹出 `CostConfirmDialog`，逐项列出费用来源，必须再点一次"确认"才发出请求。
   只读页面（热点列表、二创审核、任务、设置）**永不产生费用**，页面文案明说这一点。
   通知测试走同一个对话框，但标注为"不会产生费用"——**不把免费操作也说成收费**。
2. **系统不自动发布。** UI 只提供"复制文案"。二创审核页顶部写明"复制后请自行到平台发布，
   系统不会代为发布"。`READY_TO_PUBLISH` 只是一个标签，不触发任何外部写操作。
3. **密钥只写不读。** 设置页的密钥输入框是密码框，占位符只显示掩码（`sk-c374…dac4`）；
   **留空表示保持原值**，所以改一个相邻配置不需要重新粘贴密钥，也不可能误删密钥。
   保存后的响应明确给出 `restart_required`：环境变量在进程启动时读取，**账号定位例外**
   ——它每次运行重新读取，保存即生效。

### 新增接口（都不花钱）

| 接口 | 说明 |
|---|---|
| `GET /api/system/stats` | 总览页全部数字，一次查询返回（§29） |
| `GET /api/settings` | 可编辑配置，密钥**只返回掩码** |
| `PUT /api/settings` | 就地改写 `.env`（保留注释、顺序、未知键；原子替换） |
| `PUT /api/settings/profile` | 保存账号定位并立即生效（清缓存后回读验证） |
| `GET /api/hot/stored` | 新增 `q` / `recommended` / `selected` 筛选，并内嵌该条的 `analysis` 与 `rewrite` |

`/api/hot/stored` 用**一次 join** 带上分析与二创，列表页因此不需要"每行一个请求"。
`recommended=false` 只匹配**明确标记为不推荐**的分析：未分析的条目该列是 NULL 而不是
False，把它算进"未推荐"会让筛选结果撒谎。

### ⚠️ 我引入的一个会让服务起不来的 bug（已修复，并补了回归测试）

`_mount_frontend()` 结尾有两行从 `create_app()` 复制过来的语句：

```python
    app.state.settings = settings   # settings 不在这个函数的作用域里
    return app
```

`dist` **不存在**时函数会在前面的 `if not FRONTEND_DIST.is_dir(): return` 提前返回，
这两行永远不会执行——所以**整套 267 个测试全绿**。而 `npm run build` 一跑完，
`dist` 出现，`create_app()` 立刻 `NameError`，**后端完全无法启动**：

```
File "app/main.py", line 134, in _mount_frontend
    app.state.settings = settings
NameError: name 'settings' is not defined
```

修复是删掉这两行。更重要的是补上 `tests/test_frontend_serving.py`（8 个用例，用
`tmp_path` 造假 `dist`），覆盖 `create_app()` 能启动、`/` 返回外壳、哈希资源可取、
深链接回落到外壳、**未知 `/api/*` 返回 JSON 404 而不是外壳 HTML**、缺失资源 404、
`..` 穿越取不到 dist 之外的文件。**"测试全绿"只证明被测的代码路径是对的**：
这条路径在构建产物出现前根本没被执行过。

### 同一阶段里被新测试抓出来的另外三个问题

| 问题 | 症状 | 修复 |
|---|---|---|
| `dashboard_stats()` 少了一个括号 | `await session.execute(...).scalar_one()` 是"在协程上调用 `.scalar_one()`"→ `AttributeError`。**总览页 500** | 写成 `(await session.execute(...)).scalar_one()`；新测试直接抓到 |
| `validate_updates()` 校验太弱 | 整数字段只做 `int()`，`ANALYSIS_MAX_SELECTED=0` 通过校验 → 一次"一条都不选"的运行 | 改为**构造 `Settings` 来校验**：schema 成为唯一校验源，新增规则自动生效 |
| `write_env_file(path=ENV_PATH)` 默认参数 | 默认值在 `def` 时求值，测试无法重定向 → **测试会改写真实 `.env`（里面有真密钥）** | 改为 `env_file_path()` 每次调用求值；接口测试用 `tmp_path` 重定向，实测真实 `.env` 未被触碰 |

另外把 `probe=false` 时的组件状态从 `not_configured` 改成 `skipped`：密钥**已配置**、
只是没探测，报成"未配置"会让运维去配一个已经配好的东西。前端据此显示"未检测"。

### ⚠️ 一个靠"看页面"才发现的 bug：二创的 token 从未落库

上面的 bug 都是测试抓到的，这一个不是——**测试全绿，运行级 token 总数也对**，
是打开二创详情页时看到 `token 0/0` 才发现的：

```
id=5   status=NEEDS_REVIEW  tokens={'prompt': 0, 'completion': 0}  sim=0.61
id=11  status=NEEDS_REVIEW  tokens={'prompt': 0, 'completion': 0}  sim=0.74
analysis id=5  tokens={'prompt': 100, 'completion': 181}   # 分析行是正常的
```

原因：`run_rewriting()` 结束时把 `client.total_usage` 记到了**运行级**结果里，
但每条 `ai_rewrites` 行从来没有被写入这两个列。结果是**最贵的那次调用（二创）
恰好不可逐行审计**，而 README 上一阶段刚承诺"AI 花费需可逐行审计"。

修复：`RewriteResult` 增加两个内部字段，每次调用后按 `completion.usage` **精确记账**
（分析是"一批多条"所以必须摊分；二创是"一条一次调用"，不需要摊分，这是精确值）；
§18 触发重试时把**两次都算进该行**（钱确实花了）；`upsert_rewrites()` 写入这两列。

为它补了两个测试，并**先验证测试能抓到**（把写库那行注释掉，测试报
`assert (0, 0) == (500, 800)`，然后恢复）。已存的 4 行历史数据仍是 0：单行分摊无法
事后还原，**不做假数据回填**，新运行开始有真实数字。

### 用无头浏览器截图做 UI 验证（本机自带 Chrome，不装任何依赖）

HTTP 200 只能证明"文件发出去了"，证明不了"页面渲染出来了"——Vue 的模板里写错一个
标识符不会让构建失败，只会静默渲染成空。所以每个页面都用无头 Chrome 截图后**真的看**：

```bash
chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --window-size=1500,1200 --virtual-time-budget=9000 \
  --screenshot=out.png http://127.0.0.1:8000/settings
```

注意 `--no-sandbox`：不加时截图进程静默退出、不产出文件、也不报错（我第一次就踩了）。
截图确认的内容：总览的统计卡与成本提示、热点列表的中文与 emoji（无乱码）、
二创详情页的"需人工核实"提示 + 三平台文案 + 复制按钮、设置页的密钥掩码
（`已配置：sk-c37…dac4（留空保持不变）`）、话题页的空状态说明。

### ⚠️ 另外两个只有"看截图 + 对照真实数据"才能发现的 bug

截图里有两个数字不对，一查是同一类错误：**把 `.env` 里手写的字符串当成了规范化的值**。

**① 没人动过的页面显示"保存修改（5 项）"。** 正好 5 个，因为 `.env` 里有 5 个布尔值的写法
不是小写 `true`/`false`：

```
ANALYSIS_SEMANTIC_MERGE  ''    DETAIL_FETCH_ENABLED  ''    SCHEDULER_ENABLED  'True'
WECHAT_ENABLED           ''    QQ_ENABLED            ''
```

`el-switch` 的 `active-value="true"` / `inactive-value="false"` 都匹配不上，Element Plus
在挂载时把它**静默改写成 `'false'`**，于是"表单值 ≠ 原值"，凭空多出 5 项待保存。
点一下保存就会真的往 `.env` 写 5 个键，还要求重启——**一个假警报会引发一次真实改动**。

**② 更严重：正在运行的调度器显示为"关闭"。** `SCHEDULER_ENABLED=True`（大写 T）
与 `'true'` 不相等 → 开关渲染成关闭。而同一页的总览卡片写着"运行中"，启动日志写着
`scheduler started: ['08:00','12:00','18:00']`。**UI 在说谎**，而且说的正好是
"钱会不会自己花出去"这件事。

修复：`normaliseBool()` 把 `true/1/yes/on`（大小写无关）归一成 `'true'`，其余 `'false'`，
加载时用它填表单。修复后实测：全新加载显示"保存修改"且**按钮为禁用态**（0 项），
「启用定时任务」显示为**开启**，与真实运行状态一致。

顺带修掉一个永久空列：通知通道表格的"说明"列绑的是 `detail`，而 `detail` 是**发送结果**
（`SendResult`）的字段，通道描述（`describe()`）里根本没有这个键——它永远渲染成空白。
改为按各通道真实上报的字段拼装说明。

### 实测验证（真实服务，`scripts/verify_server.py`）

```
[ok] GET / is 200 / HTML / 含 app 挂载点 / 无编码乱码
[ok] the shell references a hashed bundle — /assets/index-XXXXXXXX.js
[ok] GET /assets/index-XXXXXXXX.js is 200 / javascript / 1269743 bytes
[ok] deep link /hot /topics /rewrites /tasks /settings serve the shell
[ok] GET /api/system/stats is 200
     totals: items=142 analyses=60 selected=4 rewrites=4 needs_review=4
[ok] GET /api/hot/stored is 200 — total=142
[ok] stored title is not mojibake: 完全帅🤤 / 蜜雪冰城蜜瓜系列！！全系列测评！
[ok] GET /api/settings — the settings page has groups
[ok] no configured secret value is returned — checked 4 keys
[ok] phase=7  tikhub: connected  database: connected  deepseek: connected
[ok] unknown /api path is 404 / JSON / does not serve the shell
all checks passed
```

这个脚本同时固定了两条踩过的坑：**必须 `trust_env=False`**（本机注册表代理会把
localhost 打到 7890 上，返回 502 且服务端零日志），**必须用 httpx 抓取**（PowerShell
会把无 charset 的 UTF-8 按 ISO-8859-1 解码，中文变乱码）。脚本还断言**真实密钥值**
不出现在 `/api/settings` 响应里——只查 `sk-` 前缀是没用的，掩码本身就带前缀。

### 已知限制

- **话题聚合页通常是空的**：第 4 层是词面相似度（阈值 0.82），实测跨平台最高约 0.35，
  因此 `topic_groups` 常为 0。页面显式说明这是算法局限，**不是采集失败**。
- **设置页保存后需要重启后端**：环境变量在进程启动时读取。这是如实告知，不是没做热加载；
  账号定位确实保存即生效（每次运行重读）。
- 未做登录鉴权：服务只监听 `127.0.0.1`，是本机单用户后台。**若要暴露到局域网必须先加鉴权。**
- 前端未做单元测试（后端 277 个测试覆盖接口契约）；UI 的验证方式是**真实构建 + 真实服务
  的端到端检查 + 无头浏览器截图**（见上）。
- 已存的 4 条二创记录 `prompt_tokens`/`completion_tokens` 为 0（bug 修复前的历史数据，
  未做假回填）；新运行的二创行会记录精确用量。

---

## Phase 8：MCP 服务（让 AI 客户端直接调用本项目）

### 形态：挂在同一个 FastAPI 进程上

```
http://127.0.0.1:8000/mcp/     ← MCP（streamable-http）
http://127.0.0.1:8000/         ← 管理后台
http://127.0.0.1:8000/api/...  ← REST
```

一个服务决定可用性：**后端起来，MCP 就在**；后端停掉，工具就没了。工具跑的是同一套
settings、同一个数据库、同一批序列化函数——不存在第二份实现会走偏。

### 12 个工具：8 个免费只读 + 4 个计费

| 工具 | 费用 | 作用 |
|---|---|---|
| `get_stats` | 免费 | 各平台条数、已分析/已入选/二创/待审核 |
| `list_hot` | 免费 | 热点列表（平台/关键词/推荐/入选筛选），带 AI 结论与二创状态 |
| `get_topics` | 免费 | 跨平台话题聚合（并说明第 4 层经常为空是局限） |
| `get_analysis` | 免费 | 单条热点的完整分析 |
| `get_rewrite` | 免费 | 单条热点的三平台草稿 |
| `list_rewrites` | 免费 | 二创草稿列表与状态 |
| `list_tasks` | 免费 | 历次运行记录与错误 |
| `get_schedule` | 免费 | 定时配置与下次运行时间 |
| `run_collection` | **计费** | TikHub 采集三平台并入库（约 3 次计费调用） |
| `run_analysis` | **计费** | DeepSeek 分析 + 筛选 |
| `run_rewrite` | **计费** | DeepSeek 生成三平台草稿 |
| `run_pipeline` | **计费** | 按阶段跑完整流水线（最贵） |

每个计费工具的描述里都写明花什么钱——模型在选工具前读到它，审批的人在点确认时也读到它。

`MCP_ALLOW_BILLED=false` 会让 4 个计费工具**根本不注册**。工具 schema 会进入**每一次**模型
请求，所以"不存在的工具"不花上下文，也调不到。设置页可以开关这两项。

### 接进 DSH（`~/.dsh/profiles/desktop/cordis.patch.yml`）

```yaml
    - id: mcp-socialhot
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: socialhot
        transport: streamable-http
        url: http://127.0.0.1:8000/mcp/     # 尾斜杠不能省
        toolCallTimeoutMs: 300000           # 流水线要跑分钟级
        failOnStartupError: false           # 后端没起不该拦住 DSH
```

工具会以 `mcp__socialhot__<名字>` 出现。**改完必须重启 DSH**（配置在启动时读取）。

### ⚠️ 两处必须说清楚的边界

**① 启动顺序有要求：后端要先起。** 插件文档写明初始连接失败时"harness 仍会启动，但该
服务器的工具**不会出现**"。所以"只要后端起来了就能调用"**不完全成立**：

- 后端先起 → 再开 DSH：工具可用；此后后端重启能被自动重连接住 ✅
- DSH 先起 → 后端后起：工具**不在列表里**，要重载配置或重启 DSH ⚠️

**② 计费闸门必须跟着扩展。** 原来的 `tikhub-billing-guard` 只匹配 `mcp__tikhub-`，
新服务叫 `socialhot`，**默认完全不经过闸门**——等于绕过你自己建的拦截。
已把 4 个计费工具的名字加进闸门（精确名单，不是前缀），只读工具照旧免费直通。
闸门里的名单与 `backend/app/mcp/server.py` 的 `BILLED_TOOL_NAMES` **必须同步**：
改名不同步就是一个洞，后端启动日志会打印这 4 个名字以便核对。

### 一个必须自己先探测 SDK 的教训

第一版我按记忆写 `from mcp.server.fastmcp import FastMCP`，**装好的 mcp 2.2.0 直接报错**：

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'.
This is mcp 2.x, where FastMCP was renamed to MCPServer (from mcp.server.mcpserver import MCPServer)
```

网上所有 v1 例子对这个安装都是错的。于是先写 `scripts/probe_mcp_api.py` 把 API 问清楚，
再写实现——顺带问出了三件靠猜必错的事：`session_manager` 是**惰性创建**的（只有调过
`streamable_http_app()` 之后才存在）、子应用**必须跑自己的 lifespan**、以及
`CallToolResult` 的属性是 `is_error` 而不是 `isError`。

### 这一阶段踩到的四个坑（全部已修）

| 坑 | 症状 | 处理 |
|---|---|---|
| 挂载顺序 | `_mount_frontend` 注册了 `GET /{full_path:path}` 兜底路由，先注册者优先 → `GET /mcp/` 会被 SPA 兜底吞掉并返回 index.html | MCP 先挂载；SPA 兜底额外对 `mcp/` 前缀返回 404 |
| 子应用 lifespan | 不跑 `mcp_app.router.lifespan_context(mcp_app)` 就挂载，启动看起来正常，**第一个 MCP 请求才 500** | 在 FastAPI lifespan 里包住 yield |
| DNS rebinding 保护 | 传输层校验 `Host`，非本机名字一律 **421**（测试里 `Host: testserver` 就中招） | 显式声明 `TransportSecuritySettings`，绑定地址与本机名白名单；并补测试断言外部 Host 必须被拒 |
| 本机代理 | Python 的 MCP 客户端把 `127.0.0.1` 也发给了注册表代理 → **502、0 字节、服务端零日志** | 验证脚本设 `NO_PROXY=127.0.0.1,localhost,::1`。**实测 Node 不读注册表代理**（HTTP 200），所以 DSH 侧不受影响 |

### 第三次 PowerShell 编码事故（这次砸在测试文件上）

我用 `(Get-Content -Raw) ... | Set-Content -Encoding utf8` 做了一次字符串替换，
把测试文件里的中文 `"计费"` 变成了 `"璁¤垂"`。这次**没有造成数据损坏**——断言的是
一个永远不匹配的串，测试立刻失败——但这是同一个坑的第三次（前两次是 fixture 乱码、
验证脚本乱码）。**结论不变：文本文件不要用 PowerShell 往返，用文件工具。**

### 实测验证

```
$ python scripts/verify_mcp.py
connecting to http://127.0.0.1:8000/mcp/
tools (12):
  [免费] get_analysis / get_rewrite / get_schedule / get_stats / get_topics
         list_hot / list_rewrites / list_tasks
  [计费] run_analysis / run_collection / run_pipeline / run_rewrite
[ok] a meaningful number of tools is exposed — 12 tools
[ok] get_stats returned without error
[ok] stats carry the expected counters — {"total_items": 223, "analyses": 90, "rewrites": 8, "needs_review": 8}
[ok] list_hot returns rows — total=223
[ok] titles survive the round trip — 中国男篮vs日本男篮
[ok] raw payloads stay out of context
[ok] a foreign Host header is rejected — HTTP 421
all checks passed (no billed tool was called)
```

### 顺带被生产数据证明的一件事

Phase 7 修的二创 per-row token 记账，在 Phase 8 期间被 **18:00 的定时运行**验证了：

```
id=189 tokens={'prompt': 951,  'completion': 1311}   ← 修复后写入（10:01:32）
id=187 tokens={'prompt': 952,  'completion': 2158}
id=184 tokens={'prompt': 954,  'completion': 1364}
id=218 tokens={'prompt': 938,  'completion': 2916}
id=5/72/11/1 tokens={'prompt': 0, 'completion': 0}   ← 修复前的历史行
```

四行相加：prompt 951+952+954+938 = **3795**，completion 1311+2158+1364+2916 = **7749**，
与同一任务里 rewrite 阶段的 `tokens={'prompt_tokens': 3795, 'completion_tokens': 7749}`
**完全相等**。说明这不是估算，而是精确归因。

### ⚠️ 一次我没能提前提醒的花费（如实记录）

上面那次运行发生在**本次会话进行中**（18:00 定时任务）。我为了验证 UI 反复重启后端，
而那几次启动都带着 `SCHEDULER_ENABLED=true`，18:00 的 cron 就在窗口里触发了：

| 阶段 | 结果 | 费用 |
|---|---|---|
| fetch | 采集 140 条（新入库 81，刷新 59） | TikHub 3 次计费调用 |
| analyze | 分析 30 条，入选 4 条 | 10,571 tokens ≈ ¥0.066 |
| detail | 跳过（未开启） | 0 |
| rewrite | 生成 4 篇 | 11,544 tokens ≈ ¥0.070 |
| **合计** | 耗时 92 秒 | **22,115 tokens ≈ ¥0.14** + 3 次 TikHub |

**这是我的流程失误**：我在 17:51 起服务时就知道配置里有 18:00 这个时间点，却没有提醒
"再过 9 分钟定时任务会自己花钱"。正确做法是先告知，或者临时把调度器关掉再重启。
钱不多（¥0.14），但这是你明确要求"花钱前先问"的那类事。

### 已知限制

- **无鉴权**：靠"只监听 127.0.0.1" + `Host` 白名单保护。**不要绑定 0.0.0.0**，
  也不要把它暴露到局域网——那等于把"能花钱的接口"开放出去。
- **只桥接 MCP 工具**：resources 与 prompts 客户端不支持，本项目也没提供。
- **工具 schema 占用每一轮上下文**：12 个工具的 schema 会进入每次模型请求（DeepSeek
  token）。想要更省就把 `MCP_ALLOW_BILLED` 关掉，少 4 个。
- **DSH 必须先于/同时于后端启动**（见上文边界①）。
- MCP 服务本身**不做发布**，也不提供任何发布工具——与全项目一致。

---

## Phase 9：关键词搜索、媒体落库、图文排版方案

这一阶段直接回答三个问题：**能不能自己搜感兴趣的话题**、**热搜为什么没有图文视频**、
**图片能不能参与二创**。三个问题的答案都需要先看真实数据，所以先花了钱。

### 花掉的 3 次调用（已获你批准）

| 平台 | 探测的端点 | 发现 |
|---|---|---|
| 小红书 | `app_v2/search_notes` (GET) | 返回**全量图片列表** `images_list[]`，20/20 条都有 |
| 抖音 | `search/fetch_general_search_v2` (**POST**) | `aweme_type` 68=图文帖（带 `images[]`）、0=视频（带播放地址） |
| 微博 | `web_v2/fetch_pic_search` (GET) | `original_pic` 是**真实图片地址** |

先读 TikHub 的 OpenAPI 文档（**免费、无需 key**）确定了必需参数，避免"花一次调用换一个参数校验错误"。
文档已存到 `docs/tikhub_openapi.json`，检索结果见 `docs/tikhub_search_endpoints.txt`。

### 问题 1：能不能自己搜话题 —— 现在可以了

新增 `POST /api/hot/search` 和 MCP 工具 `search_topic`、界面上「热点列表 → 搜索话题」。
**每个平台 1 次计费调用**，条数不影响费用。结果以 `origin=search` 入库并带上关键词，
所以「榜单」和「我搜的」永远分得开。

### 问题 2：热搜为什么没有图文和视频 —— 前提需要纠正

实测库里 298 行的真实构成：

| 平台 | 行数 | 有封面 | 有视频 | 类型 |
|---|---|---|---|---|
| 微博 | 111 | **0** | 0 | 全是 `topic` |
| 抖音 | 67 | 66 | 0 | 全是 `topic` |
| 小红书 | 120 | 120 | 0 | `note` 65 / `video` 55 |

**微博和抖音的"热搜"是词条，不是帖子**——词条天然没有图片和视频，抖音只有"词条配图"
（`word_cover`）和"视频数"这种计数，微博连配图都没有。所以"热搜包含图文视频"这个目标
**只能靠搜索展开实现**，这正是问题 1 那件事。小红书是例外：homefeed 本身就是笔记流。

现在搜索结果会把三种媒体形态都规范化入库：

| 平台 | 图文 | 视频 | 图片地址来源 |
|---|---|---|---|
| 小红书 | `type=normal` | `type=video` | `images_list[].url_size_large`（1440w） |
| 抖音 | `aweme_type=68` | `aweme_type=0` | 图文帖 `images[]`；视频帖 `video.cover` + `play_addr` |
| 微博 | 帖子正文 + 图 | — | `original_pic`（协议相对，代码补 `https:`） |

### 问题 3：图片和文案能不能二创 —— 拆成四件事，答案不同

| | 能做吗 | 说明 |
|---|---|---|
| 文案二创 | ✅ 早就有 | Phase 4 的三平台草稿 |
| 拿到图片 | ✅ 现在有了 | 小红书搜索直接给全量；抖音图文帖给全量；微博给 `original_pic` |
| 存下来 | ✅ 现在有了 | 下载到本地素材库（见下） |
| **看懂图片内容** | ❌ **做不到** | `deepseek-flash` 是纯文本模型 |
| 生成/改造图片 | ❌ 不做 | 项目没有图像能力，也不打算加 |

**所以"图片二创"落地成了"图文排版方案"**（纯文本，DeepSeek 就能做，不新增模型）：
在二创 JSON 里增加 `image_plan` —— 封面选第几张、**封面大字文案**、每张图的角色、
图上文字、配文、整体配图思路。只有当条目**真的有图片**时才会要求模型输出这个字段：
没有图却要它排版，等于邀请它编造。

**并且明确禁止它描述图片内容**。提示词里写了"你**看不到图片内容**（你是纯文本模型），
所以只做结构安排，严禁描述图片里有什么"，输出里也带 `planned_blind: true`，
避免以后有人把那些"配文"误当成图片内容的描述。

### 本地素材库：为什么必须下载，而不是只存链接

因为**平台的图片链接是带签名会过期的**。真实抓包里就是这种形式：

```
https://sns-na-i11.xhscdn.com/…?imageView2/2/w/1440/format/webp&ap=5&sc=SRH_DTL&sign=65e5d78f…&t=6aad2bac
```

只存链接，几小时后链接失效，"保存下来"就成了一句空话。所以文件真的下载到 `media/`：

- **按内容寻址**：文件名是字节的 SHA-256，同一张图只存一份，重复下载不会互相破坏
- **有上限**：单文件 8MB、每条最多 9 张、每批 120 秒墙钟预算——一个挂住的 CDN 不能拖死整轮采集
- **失败是数据不是异常**：抓不到就记在 `report.errors` 里继续跑，绝不让采集失败
- **校验真实字节**：CDN 用 200 返回一个 HTML 错误页是真实存在的情况，所以既查 `Content-Type`
  也查**魔术字节**，否则会存出一个叫 `.jpg` 的网页
- **刷新不丢**：定时任务重读同一条会生成一个没有本地路径的新 bundle。直接覆盖会
  **忘记所有已下载文件并在磁盘上留下孤儿**，所以 `merge_media_downloads()` 按 URL 把
  下载信息带过去，并补了测试
- `media/` 已加入 `.gitignore`：它是别人图片的本地缓存，体积大、且来源链接会过期

### 这一阶段踩到的坑（全部已修）

| 坑 | 症状 | 处理 |
|---|---|---|
| `fetch_hot` 是抽象方法 | 搜索适配器继承同一个基类却无法实例化（`TypeError: Can't instantiate abstract class`） | 拆成 `BasePlatformAdapter`（只放共用工具）与 `HotListAdapter`（含 `fetch_hot` 契约）。让搜索适配器实现一个它用不到的抽象方法是在撒谎 |
| 抖音搜索是 POST | 客户端只有 `get_json`，调用直接 405 | 抽出 `_request_json()` 让 GET/POST 共用重试与错误分类，而不是复制 60 行；已有 `test_client.py` 验证重构没改行为 |
| `json = json` 报错 | 验证迁移时用 `layout = '{}'::json` 查询 → `operator does not exist: json = json` | PostgreSQL 的 `json` 类型（项目为兼容 SQLite 刻意不用 `jsonb`）没有等号运算符，改用 `layout::text = '{}'` |
| **路由没有转发新筛选参数** | `/api/hot/stored?origin=search` 返回全部 298 行，`with_images=true` 也是 298 | 仓储层和 MCP 工具都加了参数，**唯独 HTTP 路由忘了加**，于是界面上的新筛选器"看起来能用、其实什么都没做"。已修，并补了断言"筛选必须真的生效"的回归测试 |

最后那条是**验证脚本抓到的**，不是测试抓到的——因为在没有搜索数据之前，
"筛选没生效"和"筛选生效但结果为空"看起来一模一样。

### 实测验证（零费用）

```
$ python scripts/verify_search_surface.py
[ok] a blank keyword is rejected (422)            ← 校验发生在计费调用之前
[ok] a whitespace-only keyword is rejected (422)
[ok] an unknown platform is rejected (422)
[ok] the rejection names the supported platforms
[ok] an over-long keyword is rejected (422)
[ok] stored rows expose 'origin' / 'image_count' / 'source_keyword' / 'media'
[ok] existing ranking rows are marked as such — hot
[ok] the origin filter is accepted — rows with origin=search: 0
[ok] the with_images filter is accepted — rows with images: 0
[ok] a missing media file is 404
[ok] the SPA fallback does not swallow /media/
all checks passed (nothing was spent)
```

`/api/hot/search` 的校验**先于任何 provider 调用**，所以关键词写错不会被计费——
这一点本身也有检查在守着。

### 已知限制

- **模型看不到图片**：`image_plan` 是排版结构，不是图片理解。要做到"看图写文案"必须接
  视觉模型，那是另一笔费用，需要单独决定并确认 `deepseek-flash` 之外的可用模型。
- **版权风险要你自己定策略**：把别人的图直接搬进自己的帖子，平台会判定搬运/侵权。
  当前实现只是把素材存到本地，**是否使用、怎么使用由你决定**，系统不代替你做这个判断。
- **视频只存不处理**：`media.video` 保存播放地址、封面和时长，不下载、不切片、不转写。
- **素材库会持续增长**：目前只有上限（单文件 8MB / 每条 9 张），**没有自动清理**。
  需要时手动删 `media/` 目录即可（数据库里的 `local_path` 会随之失效，重新搜索会重新下载）。
- **重复下载**：同一条内容再次被搜索到时，因为新 bundle 没有本地路径，会重新下载一遍
  （按内容寻址，不会写坏文件，但会浪费带宽）。要做"URL 未变就跳过"需要先查库，
  本轮没做。
- **每条最多下载 9 张**（`MEDIA_MAX_IMAGES_PER_ITEM`）：实测一条小红书笔记有 18 张，
  超出的不会下载但**仍记录在 `media.images` 里**（链接在库里，只是没落盘）。

### 第一次真实搜索跑出来的三个 bug（只有花那 3 次调用才能发现）

关键词「露营装备」，三平台各 10 条，结果 **30 条全部入库**，图片下载 83 张 / 43 MB。
但真实数据立刻暴露了三个离线 fixture 测不出来的问题：

**① 抖音的图片 19 张全部下载失败。** 错误是"body is not a known image format"——
而那句话本身就是错的：抖音 CDN 返回的是 **HEIC**（`ftypheic`），是**真图片**，
只是我的魔术字节表里只有 JPEG/PNG/GIF/WebP。更关键的是查到根因：

```
url_list[0] = …~tplv-dy-aweme-images-v2:1920:1073:q80.heic   ← 我的适配器取了这个
url_list[1] = …~tplv-dy-aweme-images-v2:1920:1073:q80.jpeg   ← 真正能用的那个
```

**同一个 URLs 列表里两种编码，我盲取了第 0 个。** 而 HEIC **浏览器根本不能显示**，
所以即使下载成功也是废料。修复：`pick_web_image()` 显式优先选 jpeg/jpg/png/webp。
修复后重跑抖音：**6/6 下载成功、0 失败**（全部验证为真 JPEG）。
`_looks_like_image()` 也补上了 HEIC/AVIF——合法就是合法，能不能用是另一回事，
错误信息不该把真图片说成"不是图片"。

**② 搜索收集了 30 条，界面只显示 5 条。** 回读用的是"标题 ILIKE %关键词%"，
而只有 5 条的标题里恰好含"露营装备"。**用户会以为搜索丢了结果。**
修复：改用 `source_keyword` 精确匹配（那一列本来就是为此存在的）。
修复后同一个关键词回读 **34 条**（含上一轮的同词结果）。

**③ 界面上的"筛选"看起来能用、其实什么都没生效。** 见上一节——是验证脚本抓到的。

这三个都是**离线 fixture 测不出来**的：fixture 里我根本不知道 `url_list` 有两项、
不知道回读会走标题匹配、也没有"筛选没生效"的对照。**这就是为什么值得花那 3 次调用。**

### 实测数据（真实运行）

**⚠️ 先把费用说清楚：我把估算说错了。** 我事先估计 3 次搜索约 $0.003，
**实际 $0.021**；加上后面重跑抖音 1 次，4 次调用共 **$0.031**，即
**每次搜索调用约 $0.0078（≈¥0.056）**。也就是说一次三平台搜索约 **$0.023（≈¥0.17）**，
比我说的贵约 8 倍。余额从 $4.941 降到 $4.889 是四次调用的真实结果（免费额度 $2.0639 未被消耗）。
以后我不会再用"大约一厘钱"这种没根据的说法——**先量一次，再说数字**。

| 平台 | 图片 URL 数 | 成功下载 | 失败 | 实际落盘格式 |
|---|---|---|---|---|
| 小红书 | 119 | 74 | 0 | 全部 `.webp` |
| 微博 | 10 | 9 | 1 | 全部 `.jpg` |
| 抖音 | 22 | 6 | 0→已修 | `.jpg`（旧行仍是 HEIC，见下） |

- 小红书 119 张里只下载了 74 张：因为 `MEDIA_MAX_IMAGES_PER_ITEM=9` 对每条取前 9 张，
  **超出的图仍记录在 `media.images` 里**（链接在库，只是没落盘），日志会打印"taking 9 of 18"。
- 抖音仍有 16 张是 HEIC：属于**第一轮**收集、而第二轮搜索结果里没有再次出现的行。
  它们保留着 HEIC 链接且没有本地文件，界面上显示"失效"。**再搜一次同一个词让它们被
  重新命中就会自然修好**（merge 按 URL 匹配，新 JPEG 地址会重新下载）。
  我**没有**去做 URL 字符串改写式的"修复"——那是把猜出来的地址当数据写进库。

### 已知限制

- 🔎 上面已列。

---

## Phase 10：领域聚焦 + README 转推广文案

两件事：让推给你的东西落到你的领域上，以及把一份开源项目的 README 变成三平台推广帖。

### 先说根因：为什么以前推给你的都是娱乐内容

`run_analysis` 原来的链路是：

```
rule_filter(rows)                 # 免费：只滤广告/空标题/异常计数
  → select_candidates(kept, 30)   # ← 纯按 hot_value 排序 + 平台均衡
    → DeepSeek 分析 30 条         # 花钱
```

**`select_candidates()` 只看热度。** 账号定位里的 `AI / 科技 / 数码` 要到最后打分阶段
（`account_fit`）才起作用——那时它已经只看到 30 条娱乐热搜了。

我用真实库里的 **332 行**量了这个落差：

| 来源 | 行数 | 与领域相关 | 覆盖率 |
|---|---|---|---|
| 热搜榜单（origin=hot） | 298 | **3** | **1%** |
| 关键词搜索（origin=search） | 34 | 2 | 6% |

（那 34 条是我用「露营装备」测搜索功能时收的，不在你的领域里——恰好说明关键词选择
比排序更重要。）

**所以筛选只能剔除、不能创造。** 榜单里没有科技条目时，关键词筛完就是空的。这就是
为什么两个手段必须一起上：免费的**打分重排**决定"先看哪些"，付费的**领域搜索**负责
把不存在的内容拉进来。

### 一、免费：领域打分重排（`app/services/pipeline/interest.py`）

排序改为 `(领域得分, 热度, 排名)`，平台均衡保留：

```
score=  9  matched=['程序员','AI','编程']       AI 编程助手火了，程序员都在用
score= 11  matched=['大学生','毕业季','数码','学生'] 大学生数码好物推荐
score= -8  matched=[]                          某明星恋情曝光
```

- 标题命中权重 3、正文 1，排除词扣 4 分（所以"AI + 明星"仍排在纯娱乐之前）
- `INTEREST_ONLY=true`（默认）时，**得分为 0 的条目不送 AI 分析**——这是真正的省钱开关
- 关键词在**设置页**可改，无需编辑 `.env`
- **不存分数列**：关键词随时会改，存下来就会过期并需要回填，还可能和当前配置矛盾。
  几百条标题做字符串匹配是免费的，读的时候算即可。

⚠️ **一个必须知道的后果**：`INTEREST_ONLY=true` 且榜单没有相关内容时，这一轮会
**0 条候选**。这时结果里会写明

```
skipped_reason: INTEREST_ONLY is on and no stored item matched an interest keyword;
                nothing was analysed. Add WATCH_KEYWORDS or widen INTEREST_KEYWORDS.
```

而不是静默地什么都不做，也不是偷偷去分析娱乐内容。

### 二、付费：定时领域搜索（新的 `watch` 阶段）

流水线阶段从 5 个变为 6 个，`watch` 排在 `fetch` 之后、`analyze` 之前——
它**提供候选**：

```
fetch → watch → analyze → detail → rewrite → notify
        ↑ 6 次计费调用/轮
```

**费用 = 关键词数 × 平台数**，默认 `AI工具,编程学习,数码好物` × `xiaohongshu,douyin`
= **6 次/轮**。这个数字在**三个地方**都会出现：启动日志（WARNING）、`/api/pipeline/schedule`、
以及每次任务的步骤报告。一次没人看得见的持续扣费，就是一次控制不了的扣费。

- 可随时关：`WATCH_SEARCH_ENABLED=false`（设置页也能关）
- 可手动触发：界面/接口 `POST /api/hot/watch`、MCP `run_watch_searches`
- 3 轮/天 × 6 次 × $0.0078 ≈ **$0.14/天 ≈ ¥1/天**（余额 $4.889 约 35 天）
- 想更省：减少关键词、减少平台、或把 `HOT_FETCH_TIMES` 从 3 次改成 1 次

### 三、README → 小红书/微博/抖音

`POST /api/promo/readme`、界面「项目推广」、MCP `generate_readme_promo`。
三种输入：粘贴内容 / 本地路径 / 网址（**只能三选一**，多给少给都是 422）。

**关键设计：README 是"消化"的，不是整篇塞进去。** 实测本项目 43183 字符的 README：

```
43183 chars -> 10862 sent (25%), truncated=True
```

抽取的是：一句话简介、章节结构、要点条目、命令片段、以及信息密度最高的 8 个正文章节。
而且**明确告诉模型它没看到全文**，并禁止它猜测被省略的内容。

**反编造规则**写在提示词里：README 没写的 star 数、下载量、性能、用户量，以及
"我踩了三个月的坑"这类虚构经历，一律不许出现。实测效果（真实生成，一次调用）：

```
项目名   : SocialHot AI
一句话   : 自动采集小红书微博抖音热点，用 AI 改写成三平台文案交你审核
封面大字 : 热点自动采集+AI改写
小红书标题: 307个测试全过：热点采集到AI改写一条龙
未知信息 : ['项目仓库地址、作者信息、开源许可证',
            'Star 数、下载量、用户数等任何社区数据（README 节选中完全没有）',
            '完整 README 里被省略的部分（本次只送了 10862 字符，原文 43183 字符）', ... 共 10 条]
```

值得注意的是它**主动把项目自己的短板写进了文案**：

> ⚠️ 但它不是成品，作者自己在 README 里列了短板：获取详细内容这一步还没实现；
> 第 4 层去重是词面匹配，不是语义匹配，会出现"毫无关系的两条被算高分"。

这说明"只依据 README、不许编造"的约束真的生效了——README 里那段诚实的限制说明
被如实带进了推广文案，而不是被美化掉。

### ⚠️ 这一阶段的四个 bug（都是自己写出来的，已修）

| bug | 症状 | 处理 |
|---|---|---|
| **Alembic 静默生成空迁移** | 新模型 `readme_promo.py` 没加进 `app/models/__init__.py`，autogenerate **什么都没检测到**，迁移体是 `pass`，表根本没建，而且**全程没有任何报错** | 补上注册；新增测试遍历 `app/models/` 下每个模块，断言它声明的表都在 `Base.metadata` 里 |
| **摘要截断了却不承认** | 43183 字符的 README 只送了 10862（25%），但 `truncated` 是 `False`——因为只有命中总上限时才置位。模型会把节选当全文 | 任何裁剪（章节数、单节截断、条目数、总量）都置位，并加比值兜底；提示词里明确告知 |
| **文件类型守卫形同虚设** | 允许后缀里写了 `""`，而 `Path("id_rsa").suffix`、`Path(".env").suffix` 都是 `""` → **任意无后缀文件都能被读进 prompt**（`id_rsa`、`.env`、`.git-credentials`） | 去掉 `""`，改为"后缀在白名单内**或**文件名是 README 类" |
| **服务端日志里的中文一直是乱码** | 控制台编码不是 UTF-8，而日志流没像脚本那样 reconfigure。之前没发现只是因为**从没有中文进过服务端日志**（第一次记中文关键词就露出来了） | `setup_logging` 里 reconfigure stdout+stderr；并给 uvicorn 传 `log_config=None`，消掉重复的一份日志 |

### 成本实测（这次我又估错了）

| 项目 | 我的预估 | 实测 | 偏差 |
|---|---|---|---|
| README 转三平台文案（1 次生成） | ¥0.013 | **¥0.0419**（6177 prompt + 3697 completion tokens） | **3.2 倍** |
| 领域搜索（每轮 6 次调用） | $0.047 | 按实测单价 $0.0078/次 = **$0.047** ✓ | — |

prompt 那部分是估准了（6177 vs 估 ~10651，我估高），**错在输出长度**：我按 1200 tokens
估，实际 3697（三份文案 + 10 条 unknown + 5 条配图建议）。教训同上一次：**先量再说数字**，
而且要按"输出的上限"估，不是按"我以为它会写多短"。

### ⚠️ 一次"白花钱"的输入（已修）

在「项目推广」里输入一个小红书分享链接 `https://xhslink.cn/o/ZLoZUtPrUQ`，模型的回答是
"无法从 README 确定"一长串。**模型的回答是对的，错在我把这种输入送进去了。**

真实发生了什么（实测）：

```
https://xhslink.cn/o/ZLoZUtPrUQ
  → 302 → https://www.xiaohongshu.com/login?redirectPath=.../discovery/item/6aa377f6...
content-type: text/html
<title>   : 小红书 - 你的生活兴趣社区
og:title / og:description : 都没有（内容需要登录 + JS，HTML 里没有）
```

于是 35865 字符的 HTML/JS 被当成 README 消化成 **417 字符**的噪声，**然后照常付费调用了模型**——
花 3407 tokens（约 ¥0.015）让模型告诉我"这个 README 什么都没说"。

修复是**两道闸门，都发生在付费之前**：

| 闸门 | 拦什么 | 现在的行为 |
|---|---|---|
| HTML 检测 | `Content-Type: text/html`，或正文以 `<!doctype html>`/`<html`/`<script` 开头 | 422，并把**重定向后的地址**和**页面标题**一起返回，例如"重定向到: .../login；页面标题: 小红书 - 你的生活兴趣社区" |
| 结构检测 | 消化完发现标题/简介/条目/安装步骤/正文**全都没有**，或提取不足 200 字符 | 422，明确写"**没有调用模型，也没有产生费用**" |

实测（真实调用，两条都不再花钱）：

```
your xhslink   -> HTTP 422  cost=None
pasted junk    -> HTTP 422  cost=None      # 粘贴 "var a=1;" x40
history count  : 4 (unchanged: no junk stored)
```

顺带修掉两个相关缺陷：

- **状态码分类错了**：内容是垃圾属于**输入问题（422）**，原来却因为消息里没有匹配的子串
  而返回 502（服务端错误）。改成由服务在结果里带 `status`，不再靠错误文本猜。
- **第二道闸门最初是假的**：我一开始判断"有没有第一个段落"，但**任何文本都有第一段**——
  `var a=1;` 重复 40 次也有 360 字符的"第一段"。改为要求像散文（有句末标点且 ≥120 字符），
  并有测试守着。

界面上的 URL 输入框下也加了明确提示：这里要的是 **README 文件的 raw 地址**，
分享链接和仓库主页会在调用模型前被拒绝。

### 四、小红书笔记作为**风格参考**（不是内容来源）

小红书分享链接现在有正确的用途：**学它的形式**。这条路径把两件成本完全不同的事分开了：

```
https://xhslink.cn/o/ZLoZUtPrUQ
  → 302 → .../login?redirectPath=.../discovery/item/6aa377f60000000028036b7a?...xsec_token%3D...
  ├─ 第 1 步：解析出 note_id + xsec_token          ← 免费（只是跟随重定向，不调 API）
  └─ 第 2 步：TikHub 读笔记内容                     ← 1 次计费调用（约 $0.008）
```

**链接无效时在第 1 步就失败，不产生任何费用**（也没有 xsec_token 时直接说明原因，而不是
花一次注定失败的调用）。实测你的链接解析结果：

```
note_id: 6aa377f60000000028036b7a   has_token: true
```

拿到笔记后提取的是**形式特征**而非内容：标题长度、正文字数、段落数、标签数量与写法、
配图数量、笔记类型。提示词里明确写死：

> 1. **绝对不要复制它的任何句子、说法或事实**——那是别人的内容，与本项目无关。
> 2. 不要把它的作者经历、数据、产品名当成你的。
> 3. 学的是**形式**：标题多长、正文分几段、每段多长、标签怎么放、语气如何。

实测效果（用你的链接，真实生成）：

| | 参考笔记 | 生成的推广帖 |
|---|---|---|
| 标题 | 疑似刷到xhs上专升本邪修最有用的评论区（20 字） | 327个测试全过：热点到AI改写一条龙（**20 字**） |
| 正文 | 825 字 / 11 段 | 短段落 + emoji，学生向语气 |
| 标签 | 6 个 | **6 个** |
| 配图 | 6 张 | 给出配图建议（系统不生成图片） |

**内容零重合**：参考笔记讲专升本，输出讲你的项目。参考笔记的 id、作者、形式特征都写进
`readme_promos.style_reference`，审核的人可以核对"是不是抄的"。

### 五、手动搜索现在会记入任务记录

之前 `POST /api/hot/search` 和 `POST /api/hot/watch` **花钱但不生成任务行**——那是最该被
看到的地方却看不到，只能靠余额推。现在它们和流水线一样落一条任务记录（含关键词、计费
次数、入库量、耗时），失败也留痕：

```
id=6 type=search   status=success  dur=16732ms steps=['search']   billed_calls=1
```

- 用 `run_as_task()` 统一封装：**先建行再干活**（崩溃也留证据）、**`finally` 关闭**
  （不会卡在 running 挡住下一次流水线）、失败**先记录再抛出**（API 仍返回真实错误）
- 一个平台失败 → 任务状态记为 `partial`，不是 `success`
- 界面点「搜索话题」「领域搜索」和 MCP 工具走的都是这条路径

### 六、三个"为什么"的实测答案（运营方提问）

**"为什么热点列表里很多只有标题？"** —— 因为**热搜榜返回的是词条**，不是帖子。

| 平台 | 来源 | 行数 | 有正文 | 平均正文 |
|---|---|---|---|---|
| 抖音 | 榜单 | 67 | **0** | 0 |
| 微博 | 榜单 | 111 | **1** | 10 |
| 小红书 | 榜单 | 120 | **0** | 0 |
| 抖音 | 搜索 | 80 | **80** | 133 |
| 微博 | 搜索 | 28 | **28** | 122 |
| 小红书 | 搜索 | 83 | **82** | 55 |

榜单 298 行里只有 1 行有正文——微博/抖音热搜是词条（词条没有正文），小红书首页推荐流的卡片
也不带正文。**搜索返回真实帖子，所以几乎都有正文。** 要正文有两条路：用「搜索话题」，
或开启 §16 详情抓取（每个条目 1 次计费调用）。小红书搜索结果的平均正文只有 55 字符，
因为 `note.desc` 在搜索卡片里往往很短——想要完整正文仍需详情调用。

**"帖子的图片保存在哪？"** —— `media/` 目录（项目根下），实测 **453 个文件 / 126.8 MB**
（`.webp` 370 + `.jpg` 83）。按内容寻址：`media/<sha256 前两位>/<sha256>.<ext>`，
同一张图只存一份；通过后端 `/media/<相对路径>` 访问；路径记在行的
`media.images[].local_path`。**只有搜索来源的行有图**（榜单 298 行全部 0 张，词条没有媒体）。
视频只存链接不下载。

**"AI 分析为什么很多都为空？"** —— 不是分析失败，是**根本没跑**。这是本阶段的缺口：

```
origin=hot     rows=298  analysed=110
origin=search  rows=191  analysed=0      ← 搜索来的 191 行，一次都没分析过
```

有分析的 110 行**字段是完整的**（空 topic 0/110、空 summary 0/110）。搜索来的行确实
**通过了领域过滤**（毕业季 20/20、编程学习 21/23、AI工具 19/20、大学生数码 13/20），
真正的原因是：**分析只在流水线的 `analyze` 阶段发生**，而调度器已关闭、手动搜索只采集
不分析，所以它们只是"还没轮到"；每轮又只处理 `ANALYSIS_MAX_CANDIDATES=30` 条。

修复（两半都补上）：

1. **`GET /api/hot/pending`（免费）** 报告缺口：还有多少行没分析、其中多少能通过领域过滤、
   这一轮会送多少、预计多少钱。实测：

```
stored_rows 489   already analysed 110   pending 379
pass interest filter 96   would be sent now 30   estimated CNY 0.069
note: 有 96 条符合领域，本轮只送前 30 条（按领域相关性排序）；再跑一次会继续处理剩下的
```

   它挑出来的第一批正是想要的：`0基础Python3周上手`、`大学四年没后悔入的数码好物`、
   `准大学生开学必备购机推荐`、`大专真的不要去专升本`（分数 18/15/15/9）——而且都带图。

2. **`analyze_after`** 参数让搜索/领域搜索可以"采完顺手分析"（默认关闭：采集与分析是两笔
   不同的费用，不该由一次点击同时触发）。界面上是一条横幅：

```
有 379 条还没有 AI 分析，其中 30 条会被送去分析
有 96 条符合领域，本轮只送前 30 条；再跑一次会继续处理剩下的
预计消耗约 ¥0.069（30 条 × 约 ¥0.0023）。[立即分析]
```

### 七、热点详情抽屉：图片/视频与正文（运营方要求）

之前列表只有缩略图列，**详情抽屉里既没有图片也没有正文**——数据早就在接口返回里
（`media` 与 `description`），纯粹是抽屉没渲染。现在：

| 区块 | 内容 |
|---|---|
| 帖子内容 | 完整正文；若来自 §16 详情抓取会标注来源 |
| 没有正文时 | 不是留白，而是**解释原因**：「榜单来源是「词条」——微博/抖音热搜返回的是词条本身，不带正文和图片…请用「搜索话题」采集真实帖子，或开启详情抓取」 |
| 图片 | **全部图片画廊**，点击放大；下面写明「已下载到本地素材库 N / M 张」——不足时说明原因（受每条下载上限限制或下载失败），全下载时提示本地副本不会因链接过期失效 |
| 视频 | 封面 + 「打开视频链接」，并注明**只保存链接、没有下载**（既定策略：先存下来，不处理视频） |
| 列表 | 标题旁加 `视频` / `图文` 标签，标题下显示正文预览前 60 字——不用逐个点开就能分辨 |

实测（用 CDP 驱动浏览器点击后截图验证，而不是只看代码）：

- 搜索行「斯坦福最新Agent课程」→ 正文 + **12 张图**，`已下载 9/12 张`（其余受每条上限限制）
- 搜索行「Mate 60 RS」→ 正文 + 6 张图，`已下载 6/6 张 —— 本地副本不会因平台链接过期而失效`
- 榜单行「黄牛称1台iPhone18回收加价500元」→ **「这条没有正文：榜单来源是「词条」」** + 「这条没有图片」

**顺带补了一个验证工具**：`frontend/scripts/capture.mjs` 用 DevTools Protocol 驱动 Chrome
（Node 自带 `fetch` 与 `WebSocket`，零依赖），可以「先点击再截图」——静态 `--screenshot`
无法验证抽屉这种需要交互才出现的东西。支持多个 `--click`（例如先翻页再点行）：

```bash
node frontend/scripts/capture.mjs --url http://127.0.0.1:8000/hot \
  --out drawer.png --click ".el-pager li:last-child" --click ".el-table__row"
```

### 八、单条触发：「AI 分析」与「二创」按钮（运营方要求）

之前只能批量跑，看中某一条时没有任何办法单独处理它。现在详情抽屉里各有一个按钮，
**不点不花钱**：

| 按钮 | 作用 | 实测成本 |
|---|---|---|
| **AI 分析这一条** / 重新分析 | 只分析当前这一条 | **¥0.0031**（822 tokens） |
| **二创这一条** / 重新二创 | 只把这一条改写成三平台草稿 | **¥0.0171**（3172 tokens） |

关键设计：

- **显式请求优先于过滤规则。** 指定某一条时**绕过领域过滤与候选上限**——过滤器的职责
  是拦住批量路径的无关内容，不该否决你自己点的那一条。结果里会记录
  `interest.bypassed = true, reason = "explicit per-item request"`。
- **二创需要先有分析**：提示词由分析结论构成（话题/摘要/为什么火/切入角度），没有分析
  就改不成。这种情况返回 **409** 并明说「请先点 AI 分析」，而不是返回一个空成功。
- **重复点击是"重跑"**：已经有分析的条目按钮变成「重新分析」，已有二创的变成「重新二创」。
- 按钮上方直接写明「点按钮才会调用，不点不花钱」。

实测（真实调用，item 489「斯坦福最新Agent课程」）：

```
AI 分析 → analysed=1（不是一批）  822 tokens / ¥0.0031
          话题: 斯坦福公开的AI Agent课程学习笔记分享
          摘要: 原内容声称作者已学完斯坦福最新发布的Agent课程并做分享，
                课程真实性、版本与发布时间目前无法确认。
二创     → considered=1 rewritten=1  3172 tokens / ¥0.0171
          小红书标题: 有人替我把斯坦福Agent课学完了？
          状态: NEEDS_REVIEW（因为分析标记了需核实 —— 状态链按设计生效）
```

### ⚠️ 这一阶段的两个 bug（都已修，第二个很典型）

**① 花了钱才 500。** `analysis_for_content()` 返回的是 **`(analysis, item)` 元组**，
我第一版把它当成了单个分析对象，于是**分析已经成功、token 已经扣掉**，序列化响应时才崩：

```
analysis run complete: {... 'analysed': 1, 'tokens': {...1245...} ...}
POST /api/analysis/run?hot_content_id=489 HTTP/1.1" 500
AttributeError: 'tuple' object has no attribute 'hot_content_id'
```

修好后补了断言该返回形状的测试——**"付费成功但响应失败"是最糟的失败模式**，用户既拿不到
结果也不知道钱花了。

**② 标签把表格挤变形。** 二创区块的 `el-descriptions` 里，风险标记用了 `el-tag` 渲染，
而 **`el-tag` 是 `white-space: nowrap`**；两条很长的中文风险描述撑出一个极宽的值列，
把**标签列压成每行一个字**（状态/需人工核实/风险标记 竖着排）。改成可换行的普通文本行。

这个 bug 只有在**看真实像素**时才看得出来——我第一眼是在缩略图里看到的，以为是缩放误差，
把它按原分辨率裁出来才确认是真的。所以验证 UI 不能只看缩略预览。

### 九、图片二创（千问图像，Phase 11）

这补上了项目一直如实标注的局限：「图片不能二创」。`qwen-image-3.0-pro` 支持
**图生图/图像编辑**（1–3 张参考图 + 编辑指令 → 新图），所以素材也能改。

**⚠️ 先把价格放在最前面**（[官方定价，华北2北京](https://www.alibabacloud.com/help/zh/model-studio/qwen-image-3-0-pro)）：

| 计费项 | 单价 |
|---|---|
| 1K 图片生成（≤2.25M 像素） | **$0.03438 / 张 ≈ ¥0.25** |
| 2K 图片生成（>2.25M 像素） | **$0.068761 / 张 ≈ ¥0.50** |
| 图片输入 | $0.00275 / 张 |

**按张计费、不按 token**，一张图 ≈ **15 次文字二创**。所以三条规则写死在代码里、不可配置绕过：

1. **没有任何管线阶段会自动生成图片**——只有人点了按钮才会调用；
2. **一次请求一张图**（`n=1`，1024×1024，落在 1K 档），`n` 每加 1 就翻一倍价钱；
3. **单次上限 `IMAGE_GEN_MAX_PER_RUN`**，防止循环 bug 批量烧钱。

免登录口 `GET /api/image/estimate` 会**先报出确切单价**，所以界面上显示的不是估算是实价。

#### 三个不这样做就会静默失败的实现细节

1. **参考图必须用 base64。** 我们的原图是本地文件、平台原链接已过期，而阿里云的服务器
   也访问不到本机 localhost——用 URL 只会失败。所以 `image_to_data_uri()` 负责编码，
   并拒绝超 10MB 或模型不接受的格式（HEIC/AVIF 是真图片，但会被明确告知"模型不接受"
   而不是发出去等报错）。
2. **返回的图片 URL 只活 24 小时**，和之前那些签名链接同一个教训：生成后**立刻**下载到
   `media/generated/`。**特意与下载来的素材分开目录**——一份是别人的图，一份是我们的，
   混在一起就没法审计来源了。
3. **尺寸分隔符是 `*` 不是 `x`**（DashScope 协议用 `*`，OpenAI 兼容模式用 `x`），
   而**计费档位由输出像素面积决定**，所以尺寸就是价格。

#### 提示词写成"编辑指令"而不是"描述"

`build_edit_prompt()` 明确要求"重新构图与打光，不是加滤镜"、"改变机位、光线和背景细节，
避免与参考图相似到像同一张"，并强制"不要出现任何品牌标识、水印、二维码或可识别的真人肖像"。
**这是把"二创"和"搬运"区分开的地方**——输出的是一张新图，而不是原图的近似复制。

#### 接口与工具

| 入口 | 说明 |
|---|---|
| `GET /api/image/estimate` | 免费：单价、模型、尺寸，以及某条有几个可用参考图 |
| `POST /api/image/generate` | **按张计费**：为某条生成配图 |
| `GET /api/image/generations` | 免费：生成记录与花费 |
| 界面 | 「二创详情」页新增「图片二创」卡片：图上文字输入、生成按钮、已生成图片画廊与累计花费 |
| MCP | `generate_post_image`（计费，已加入闸门）、`get_image_estimate` / `list_generated_images`（免费） |

#### 审计

每次生成写一行 `image_generations`：提示词、用到的参考图、供应商的计量字段、本地路径、
花费、耗时、错误。**一张奇怪的图永远能追回到产生它的确切指令和参考图**。

实测 356 个测试通过，其中 Phase 11 的 11 个用 `respx` 模拟供应商，断言的是请求形状
（参考图 base64、`*` 分隔符、`Bearer` 头）、按计量档位算价（2K 是 1K 的两倍）、以及
**没有素材 / 没有 key 时绝不发起请求**。

### 十二、知识科普文章 + 标签球（Phase 13，已完成）

面向**大学生计算机大类**的科普文章生成，按平台渲染文案，配图**搜图为主**，知识标签化 +
3D 旋转球可视化，点标签即生成文案。

#### 已完成并验证：搜图（这是生图的廉价替代）

运营方问"能否支持搜图、把相关图片下载下来、最好多张"——**可以，而且比生图便宜 4 倍**：

| 方式 | 成本 | 产出 |
|---|---|---|
| **搜图**（小红书图片搜索） | **$0.0078/次** | 一次 20 条带图内容 |
| 生图（千问 1K） | $0.034/张 | 一次 1 张 |

```
GET /api/v1/xiaohongshu/app_v2/search_images?keyword=数据结构&page=1
```

**实测结构**（字段分散在四组嵌套里，**不是** item 的直接子键）：

```
data.data.items[]                      ← 注意是两层 data
  image_info: {fileid, url, original, url_size_large, width, height}
  note_info:  {title, desc, liked_count, collected_count, note_id, model_type}
  user_info:  {nickname, user_id, red_id}
  share_info: {link, title, content, image}
```

实测关键词「数据结构」返回 20 条，内容确实对路：「二叉树和森林转换＋前序中序后序」
「时间复杂度我终于会了」「数据结构期末复习版」。

**三条实现要点**：

1. **链接会过期**（`xhscdn.com` 签名 URL），所以搜到就立刻下载进素材库，复用
   `MediaStore`（格式识别、HEIC/AVIF、大小上限、内容寻址去重都现成）；
2. **图是别人的**，所以每条都保留作者与原文链接——用于署名与溯源，不是可选字段；
3. 微博的 `fetch_pic_search` 用同关键词**返回 0 张**，所以不作为默认来源。

**一次真实调用后把响应存成 fixture**（`docs/fixtures/xiaohongshu_image_search.json`，
73KB），之后解析器开发与测试全部离线免费——这个项目已经多次因为"照文档猜字段"踩坑。

#### ⚠️ 记录一次"我自己的诊断工具骗了我"

第一版解析器把 `url`/`title`/`nickname` 当成 item 的直接子键，结果 **20 条解析出 0 条**。

原因是我写解析器时依赖了一个会把嵌套键**压平打印**的结构诊断函数，输出看起来就像直接
子键。**诊断工具的输出也会骗人**——最后是逐层打印真实 JSON 才看到 `image_info` 等分组。
已改为按真实嵌套读取，并加了回归测试（断言 20/20 全解析、字段取自正确的组）。

另修一个类型不一致：`as_text()` 对缺失值返回 `None`，而 dataclass 声明 `str`，
于是 `author` 会变成 `None`（JSON 里是 null、f-string 里是 "None"）。已统一强制成字符串。

#### 已确认的四项设计决策

| 决策 | 选择 |
|---|---|
| 配图来源 | **搜图 + 生图都支持，默认搜图**（生图按需点击） |
| 标签刷新 | **标签存库、免费复用**；只有点「刷新标签」才调模型（约 ¥0.01/次） |
| 3D 标签球 | **引入 Three.js** |
| 标签规模 | **每篇 30-50 个**（大类/细分技术/难度/岗位） |

#### 首次真实生成（已验证）与四个修复

**实测第一篇**：《数据结构不是容器：一次把代价讲清楚》——5 节、6 个术语、5 条要点、
三平台文案齐全、10 个知识标签，**12347 tokens / ¥0.0851**。

内容质量符合提示词要求：用书架类比建立直觉 → **明确指出类比在哪不成立** → 主动破除
「O(1) 一定比 O(n) 快」的误解 → 指出「链表插入是 O(1)」是半句真话（漏了前提）→
最后给可执行的测量方法（用 `time.perf_counter` 亲眼看常数因子）。

运营方报回四个问题，都已修：

**① 文章生成失败：`DeepSeekJSONError`——输出被截断成不合法 JSON。**
报错里的 JSON 断在 `"audie`。根因是文章**复用了 `REWRITE_MAX_TOKENS=6000`**，而文章有
3-7 节 × 200-500 字加术语表，根本写不完。修法三条：
新增独立的 `ARTICLE_MAX_TOKENS=8000`（不再复用二创的上限）；提示词把篇幅收窄到
3-6 节、每节 150-400 字并明说"被截断的 JSON 完全不可用"；捕获 `DeepSeekJSONError`
时给出**可行动**的提示而不是原始栈。修复后实测 completion 4301 tokens，一次通过。

**② 标签球太小、字体太大。** 根因是标签尺寸按 **canvas 像素 × 容器宽/620** 算——
容器一大字体就暴涨。改成**按球半径的世界尺寸**取（球半径的 11.5%），球半径取容器短边的
42%，相机距离跟着球半径走。现在球铺满容器、标签比例稳定。

**③ 二次确认框点确认后不消失。** 根因是 `CostConfirmDialog` **只 emit 事件、从不自己关闭**，
把关闭责任推给了父组件的成功回调——于是**请求一失败，对话框就永久留在屏幕上**。
改成对话框自己在确认时关闭（成功失败都关，失败信息照旧用 toast），行为才可预期。

**④ 搜图偶发 `HTTP 400`。** 实测这个接口会**偶发 400**：同一个请求几秒后重发就是 200
（我复现时三次调用全是 200）。TikHub 客户端把 4xx（429 除外）当终态，对这个接口过于严格，
所以搜图自己重试 3 次（失败请求不计费，重试只花时间）。配图那一步失败不会影响文章——
分步容错按设计生效了。

**顺带**：启动日志还写着 `phase 10`，已改为 `phase 13`。

#### 长任务的真实进度（运营方要求）

文章生成要 30-60 秒，**一个只会转的圈等于没有信息**。现在改为 **NDJSON 流式进度**：

```
POST /api/knowledge/articles/stream   →  application/x-ndjson
{"event":"step","step":"article","status":"started","label":"生成科普文章","note":"这一步最长，通常 15-30 秒"}
{"event":"step","step":"article","status":"done","sections":5,"title":"…","tokens":{…}}
{"event":"step","step":"platforms","status":"done","platforms":["douyin","weibo","xiaohongshu"]}
{"event":"step","step":"images","status":"done","downloaded":6}
{"event":"result","ok":true,"article":{…}}
```

界面上是一个**步骤清单**（每步三个状态：进行中/完成/失败）加上**实时秒数与"已完成 N/4 步"**：

```
生成进度                      [正在：生成科普文章 · 已用 12 秒]
                             共 4 步，已完成 0 步 · 通常需要 30-60 秒
⏳ 生成科普文章 —— 这一步最长，通常 15-30 秒
每一步都是真实调用（DeepSeek / 搜图），失败的那一步不会影响其他步骤。
```

**这不是假进度条**：界面只显示后端真的报过的事件。中间每 15 秒发一个 `ping` 心跳，
既让连接不被中间代理掐掉，也让前端知道还活着。进度回调本身**失败也不影响主流程**——
进度是旁路，不能因为它出错就让一次已经付费的生成失败。

`fetch` 而不是 axios：增量读取流需要原始 body reader，而 axios 会把整个响应缓冲到结束。

#### 三个对话框/体验问题（运营方反馈，都已修）

1. **确认框点确认后不消失** —— `CostConfirmDialog` 只 emit 事件、从不自己关闭，把关闭责任
   推给父组件的成功回调，于是**请求一失败对话框就永久留在屏幕上**。现在对话框自己关
   （成功失败都关）。**另外 `HotListView` 里的 QQ 推送对话框是独立实现**，同样的问题，
   也已改为确认时先关闭、结果用 toast + 面板反馈。
2. **确认后一直转圈** —— 见上面的流式进度。
3. **球太小、字太大** —— 标签尺寸改按球半径的世界尺寸算（见前述）。

#### ⚠️ 第三个 bug：报错只说"不是合法 JSON"，而我一开始**判断错了病因**

运营方第二次遇到文章生成失败。第一版报错里只有响应**前 200 字符** ——
所以我无法判断是截断还是语法问题，只能猜。

**先修诊断能力**（这一步最关键）：
- `extract_json` 的报错现在带**总长度与结尾片段**（截断发生在末尾，头 200 字符总是正常）；
- 新增 `DeepSeekTruncatedError`（`DeepSeekJSONError` 的子类），`complete_json` 用
  `finish_reason == "length"` 区分"被截断"与"JSON 写坏了"——两者需要不同的应对。

**然后它给出了决定性证据**：响应共 3236 字符、**正常闭合**、`finish_reason=stop`，
所以**不是截断**。而我另抓一次同一个主题，**直接解析成功** —— 说明这是
**间歇性的 JSON 语法错误**（模型在 temperature 0.7 下偶尔写出非法 JSON）。

**我上一版判断错了两处**，都已纠正：
1. 我说"语法错误重试没意义"并把它写进了测试 —— 那是把它当**确定性**问题。
   证据表明重试有效，所以现在**JSON 不合法也重试一次**，并要求更严格的格式。
2. 只报告响应头部。已改为报告长度与结尾。

三处修复：
- **提示词层面**（最有效）：系统提示里明确"字符串内不要出现真实换行、不要出现未转义的
  双引号，要强调用「」"。实测**光这一条就让重试不再被触发**。
- **免重试的容错**：`json.loads(strict=False)` 能接受字符串内的真实换行——这是中文长文本
  最常见的坏法，免费修好，省掉一次调用。
- **重试一次**：带上"上一版 JSON 不合法"的说明。

实测修复后（就是失败过的那个主题）：

```
[ 0.5s] 生成科普文章: started
[20.5s] 生成科普文章: done  sections=4
[20.5s] 改写小红书/微博/抖音: started
[35.3s] 改写小红书/微博/抖音: done  platforms=['douyin','weibo','xiaohongshu']
[35.3s] 搜图并下载配图: started
[43.9s] 搜图并下载配图: done  downloaded=6
[43.9s] 保存: done
✅ 《神经网络在算什么：一次加权求和加一个开关》4 节 · 6 术语 · 6 配图 · ¥0.0583 · 44 秒
```

#### 待完成

- [x] 文章生成服务（标题/导语/分节正文/术语表/延伸阅读）
- [x] 三平台文案渲染
- [x] 知识标签抽取（30-50 个）+ 存库 + 刷新
- [x] `knowledge_articles` / `knowledge_tags` 表 + 迁移
- [x] `/api/knowledge/*` 接口（5 个）
- [x] Three.js 3D 旋转标签球 + 点击标签生成
- [x] MCP 工具 6 个（3 个计费，已加入闸门）

**已完成并验证**（399 个测试通过）。3D 球体渲染已截图确认：彩色胶囊标签在三维球面分布、
悬停放大并显示分类/难度/说明、点击即生成。Three.js 是懒加载的独立 chunk
（542 kB / gzip 138 kB），不影响主包。

#### 计费边界（界面上每个按钮对应一条）

| 动作 | 花费 |
|---|---|
| 打开页面、看标签球、看列表、看历史文章 | **免费**（词表存库复用） |
| 点「刷新标签」 | 1 次 DeepSeek，约 ¥0.01-0.03 |
| 点一个标签生成 | 1-2 次 DeepSeek + 默认 1 次搜图，合计约 ¥0.03-0.06 |
| 单点「搜图」 | 1 次 TikHub，$0.0078（一次多张） |

#### ⚠️ 一个被测试逮到的**反复发生**的静默费用

`ensure_tag_vocabulary` 第一版写的是"词表少于 20 个标签就重新生成"。测试用一个只返回
6 个标签的 stub 跑了两次，立刻暴露问题：**只要模型某次输出偏少，每次打开页面都会再调一次
模型**——一笔反复发生、用户看不见的费用，而且与"先免费复用、手动才重新生成"的约定直接冲突。

改成：**只要库里有任何标签就复用**，数量不足只用 `warnings` 提示运营方去点刷新，绝不自己花钱。
回归测试连开三次页面断言模型调用次数仍是 1。

#### QQ 推送脚本的断线崩溃（已修）

`push_to_qq.py` 在等 @ 时遇到 `ConnectionClosedError: no close frame received or sent`
——**QQ 网关会主动断开空闲连接**。第一版直接把它抛成 traceback：用户在等消息，却只看到一个栈，
而且那一次 @ 会永远丢失。现在会重连（最多 5 次）、明确提示"断线期间你 @ 过的会漏掉，
请再 @ 一次"，并加了 20 秒 ping 保活。

### 已知限制

- **标签词表目前是占位数据**：为了不花钱验证 3D 渲染，我直接插入了 32 个真实感标签
  （`source='seed'`）。点「刷新标签」会用模型生成的词表替换它们（约 ¥0.01-0.03）。

### 已知限制：接口、UI、审计与 11 个模拟测试都已就绪，但**真实 API 一次都没跑**
  （每张 ¥0.25）。首次真实调用可能有供应商特有的问题（配额、实名、限流），需要一次验证。
- **策略锁定：每次 1 张、1K 档。** 这不是默认值而是硬约束——`IMAGE_GEN_N` 校验为必须等于 1，
  `IMAGE_GEN_SIZE` 校验必须落在 1K 档（>2.25M 像素会被拒绝，因为那会让每张价格翻倍到
  $0.068761）。**接口没有 `n` 字段、MCP 工具没有 `n` 参数**，任何入口都请求不了更多张或
  更大尺寸；设置页走同一套校验（`IMAGE_GEN_SIZE` 因此从 `string` 改成 `size` 类型，
  否则它能绕过校验——这是实测中发现并修掉的一个真 bug）。
- **实测单价略高于"每张价"**：真实一次是 **$0.03988**（输出 $0.03438 + 2 张输入图
  × $0.00275），不是 $0.03438。第一版确认框只报输出价，**少报 16%**；现在
  `/api/image/estimate` 会把输入图算进去给出 `estimated_total_usd`，
  实测**报价与扣费完全一致**（$0.03988 = ¥0.2911）。
- 生成耗时较长（实测 **102.7 秒**），模型限流 5 RPM，所以不做批量。

### 十、图片二创的首次真实调用（已验证）

```
pre-flight : qwen-image-3.0-pro / 1024*1024 · 参考图 2 张 · 报价 $0.03988
HTTP 200   : 102.7s
usage      : output 1024×1024 · output_image_count=1 · output_image_type=qima_output_1k
charge     : $0.03988 = ¥0.2911
saved      : media/generated/qwen-20260918-234842-1.png（1,060,735 字节，真 PNG，1024×1024）
served     : GET /media/generated/... -> 200，与磁盘字节完全一致
```

生成结果符合要求：干净桌面、笔记本上画着流程图、咖啡与红笔，**并把指定的图上文字
「Agent 课划重点」用中文排在了顶部红底上**。参考图是那条笔记的 2 张已下载素材。

审计记录（`image_generations` id=1）完整：提示词、2 张参考图路径、供应商计量字段、
本地路径、花费、耗时。**一张奇怪的图永远能追回产生它的确切指令与参考图。**

验证脚本：`scripts/verify_image_surface.py`（免费，11 项检查：策略、单价、档位、
文件真实性、1024×1024、后端可访问）。

### 十一、图片生成入口：热点列表详情里也能点（运营方要求）

原来只有「二创详情」页能生成图片——但要先有文案。现在**热点列表的详情抽屉里也有
「生成图片」按钮**，任何有素材的条目都能直接生成，不必先二创。

**抽成了共享组件 `frontend/src/components/ImageGenPanel.vue`**，两处（热点详情抽屉、
二创详情页）用同一份。这不是洁癖：这块正好是本项目最贵的操作（每张约 $0.034–0.039），
**价格显示、策略说明、确认框只能有一处实现**，复制两份一定会走偏。
抽出后「二创详情」页从 15.45 kB 降到 11.02 kB。

抽屉里实测（CDP 点击后截图确认，不是看代码）：

```
图上文字 [可选：要排在画面上的字，例如「真香警告」]
[生成图片]  参考图 2 张（已下载的素材）· 结果存到 media/generated/
已生成 1 次，累计约 $0.0399
[缩略图]  #1 · success · $0.03988 · 2026-09-18 23:48
```

点「生成图片」弹出的确认框（**花钱前的最后一道闸**）：

```
确认生成图片（按张计费）
以这条的 2 张已下载素材为参考，按文案方向重新生成一张新图。这是本项目最贵的操作。
⚠️ 会产生费用
每次固定 1 张、1K 档：$0.03988（≈¥0.2911）（含 2 张参考图的输入费）。按输出张数与
像素档位计费，不按 token —— 约等于一次文字二创的 15 倍。张数与尺寸由策略锁定。
生成结果会立即下载到本地素材库。
                                              [取消]  [确认生成]
```

注意显示的是 **$0.03988 而不是 $0.03438**——正是上面修正过的"含输入图"总价。

**过程中遇到并修掉的两个自己的问题**：

1. **用 PowerShell 改含中文的 Vue 文件是危险的**（本项目已因此损坏过 3 次），
   所以这次用 Python 脚本替换，并加守卫：替换前断言目标区块确实含
   `图片二创`/`askToGenerate`/`generatedImages`，且**不含** `Object.keys(layout)`
   （避免误删相邻的配图方案卡片）。第一次尝试因为多写了一个 `generateImage` 标记而
   **正确地拒绝执行**（该 token 在脚本区、不在模板区）；第二次又因为 `配图方案`
   出现在图片卡片自己的说明文字里被拒——**守卫太严也会挡住正确的操作**，
   最后只保留真正有区分度的标记。
2. 替换后忘了删脚本区遗留的状态（`imageEstimate`/`askToGenerate` 等），
   用 grep 复查确认已清空。
- **`media/` 会持续增长**（实测 453 张已 126.8 MB）：只有单文件/单条上限，**没有自动清理**。
- **分析是"轮到才做"**：没有分析的行不代表分析失败，先看 `/api/hot/pending`。：网页（含需要登录的分享链接）会被拒绝而不是被解析。
  这是有意的——见上文那次事故。**小红书链接请填到「风格参考」字段**，那里会正确地解析它。
- **风格参考只学形式**：模型看到的是参考笔记的标题/正文/标签文本与结构统计，因此它**能**
  模仿语气和节奏。但它仍看不到图片内容（无论你的笔记还是参考笔记）。
- **风格参考无法验证"学得像不像"**：没有量化指标，只能人工看。这是主观判断，系统不假装能打分。
- **打分只看词面**：标题里没有关键词但确实是科技内容的条目会被漏掉（例如"华为发布会"
  命中"华为"需要你自己加词）。想更准要接语义匹配，那是另一笔钱。
- **领域搜索的召回取决于平台的搜索质量**，同一个词每天返回的内容会变；重复的靠去重挡住。
- **推广文案仍是文案**：`image_ideas` 只是"建议截哪个界面"，系统不生成图片。
- **README 只取结构化节选**，答案的完整度受节选影响；页面会显示"原文 x 字符 → 送 y 字符"。
- **路径输入只接受文档类文件**，这是有意的限制（见上表第三个 bug）。
- 已存的历史推广记录不会因为 README 改动而自动更新；重新生成会新增一条记录。

---

## 测试

```bash
cd backend
.venv\Scripts\python -m pytest
```

**411 个测试全部通过（约 67 秒），零网络、零费用。**

真实服务的端到端检查（需要后端已启动；默认不发外部请求，加 `--probe` 才做一次
免费连通性探测）：

```bash
.venv\Scripts\python scripts\verify_server.py          # 静态资源 + API + SPA 回落
.venv\Scripts\python scripts\verify_server.py --probe  # 额外验证 TikHub/DeepSeek 连通
.venv\Scripts\python scripts\verify_mcp.py             # MCP 端点（真 socket，不调计费工具）
.venv\Scripts\python scripts\verify_search_surface.py  # 搜索/媒体接口（校验路径，零费用）
.venv\Scripts\python scripts\verify_image_surface.py   # 图片生成策略/文件/可访问性（零费用）
.venv\Scripts\python scripts\diagnose_content.py       # 为什么没正文/没分析/图片在哪
.venv\Scripts\python scripts\list_routes.py            # 打印当前 API 路由表
.venv\Scripts\python scripts\check_schema.py           # 列与迁移版本核对
.venv\Scripts\python scripts\check_tikhub_balance.py   # TikHub 余额（免费元数据）
.venv\Scripts\python scripts\probe_mcp_api.py          # 探测已装 MCP SDK 的真实 API
node scripts\check_node_localhost.mjs                  # Node 是否被注册表代理影响
```

- 全部测试**零网络、零费用**：`respx` 拦截 HTTP，适配器用构造载荷。
- 真实响应验证走 `scripts/discover_raw.py`（**会计费**，必须显式 `--yes`）：

```bash
.venv\Scripts\python scripts\discover_raw.py --yes
```

它把每个平台的**真实响应原样存进** `tests/fixtures/raw/<platform>.json`，并把结构
（顶层 keys、条目条数、首条字段）打印出来。此后所有归一化器与测试都对着这些 fixture 离线运行，
**不需要再花一分钱**。这是刻意的设计：TikHub 官方文档**不提供任何响应示例**
（所有 200 都是通用 `ResponseModel`），字段名无法从文档推断，与其猜，不如只花一次调用拿真数据。

---

## 与规格的偏差（如实列出）

| 规格 | 实现 | 原因 |
|---|---|---|
| Python 3.11+ | **Python 3.10.18** | 本机所有解释器（base + 6 个 conda 环境）最高 3.10；代码写成 3.10 可运行，未使用 3.11+ 专属语法 |
| 目录树 | 多出 `services/tikhub/base.py`、`services/pipeline/{dedup,topic_grouping,rule_filter}.py`、`services/ai/account_profile.py`、`db/repository.py`、`alembic.ini`、`account_profile.json`、`scripts/*.py`、`tests/fixtures/raw/`、`requirements.txt`、`pytest.ini` | 提取工具、四层去重、规则初筛、账号定位加载各自需要归宿；仓储层与迁移配置需要落脚点；可编辑的账号定位需要一个文件；真实验证与离线复现需要可复现脚本和存档数据 |
| PostgreSQL 端口 5432 | 项目独立集群 **55432**（`initdb`，trust，仅 127.0.0.1） | 本机 5432 实例需要密码，不应让项目依赖机器所有者的凭据；原服务未被改动 |
| `ai_analyses` 规格列 | 另加 `topic`/`discussion_points`/`account_fit`/`is_duplicate`/`selected`/`model`/tokens/`raw_response` | §14 要求模型回答这些问题；"入选"是筛选步骤的结果；AI 花费需可逐行审计 |
| `ai_rewrites` 规格列 | 另加 `xiaohongshu_ending`/`weibo_opening`/`douyin_subtitles`、`status`/`risk_flags`、`needs_verification`/`verification_note`、`model`/tokens/`attempts`/`copy_similarity`/`raw_response` | §17 明确要求结尾互动/开头/字幕而列清单漏了；§20 要求 `status`；§19 要求核验措辞；AI 花费与反抄检测需可审计 |
| `tasks` 规格列 | 另加 `trigger`/`steps`/`summary`/`duration_ms` | 手动与定时执行混在一份历史里必须能区分；§22 要求每步记录且单步失败不影响调度器，步骤报告就是证据；一次运行的开销应当可查 |
| 阶段命名 | 管线阶段为 `fetch/analyze/detail/rewrite/notify` | 与规格的流程框图一一对应；五个阶段在 Phase 6 后全部实现，"未实现阶段"机制保留给未来 |
| `hot_contents` 列 | 另加 `detail_fetched_at` / `detail_endpoint` | §16 抓到的正文存进 `description`（那本就是正文字段），这两列记录来源与时间，使任何正文都可追溯到返回它的端点 |
| 通知通道默认 `log` | 规格只提 wechat/qq | 需要一条**零凭据**通道来验证格式、拆分、回退与管线阶段；`log` 只写日志，不假装投递 |
| 配置来源 | 规格 §32 只说"设置项可修改"，未规定存放位置 | 改写**项目自己的 `.env`**（就地、原子、保留注释与未知键），而不是新建数据库配置表：本项目所有配置本来就来自环境变量，再引入一份"运行时可改配置"会让"到底哪个生效"变得无法回答；因此接口直接返回 `restart_required`，不假装热加载。唯一例外是账号定位——它每次运行重读，保存即生效 |
| 前端 `dist` 由 FastAPI 托管 | 规格的 Phase 7 未规定部署方式 | 同进程同源：不需要 CORS，不需要第二个服务，一个地址即用；`npm run dev` 仍保留给调 UI |
| `frontend/` 目录 | Phase 1 的偏差表曾写"未创建 frontend" | 该行是 Phase 1 的状态；Phase 7 已创建（Vue 3 + Vite + Element Plus + Pinia + Router + Axios）。`services/mcp` 仍未创建，属于 Phase 8 |
| `ComponentStatus.status` | 规格未定义取值 | 在 `connected`/`error`/`not_configured` 之外增加 `skipped`：密钥已配置但未探测时，报 `not_configured` 会让人去配一个已经配好的东西 |
| 前端无鉴权 | 规格未要求 | 服务只监听 `127.0.0.1`。暴露到局域网前必须加鉴权，README 已如实标注 |
| MCP 服务形态 | 规格 §33 只说"提供 MCP 服务"，未规定进程形态 | 挂在**现有 FastAPI 进程**的 `/mcp/`，不新起服务：一个进程、一个端口，后端启动即 MCP 可用，且与 REST/UI 共用同一套配置与数据访问 |
| MCP 工具数量 | 规格未规定 | 只暴露 12 个（8 免费 + 4 计费）。工具 schema 进入**每次**模型请求，所以刻意不做成"把所有 REST 端点都包一遍" |
| MCP 默认允许计费工具 | 规格未规定 | 默认 `MCP_ALLOW_BILLED=true`（即"可用但每次需人工点击确认"，由 DSH 侧闸门执行）。要彻底锁死就设为 false，工具直接不注册 |
| 依赖 `mcp==2.2.0` | 规格未指定 | 2.x 把 `FastMCP` 改名为 `MCPServer`；实现按**实际安装版本**写，并用 `scripts/probe_mcp_api.py` 固化探测结果，不用记忆中的 v1 API |
| 媒体存本地而非只存链接 | 规格未规定 | 平台图片 URL 带签名会过期，只存链接等于没存。因此下载到 `media/`（按 SHA-256 命名、有大小与数量上限、失败只记录不中断），并用 `merge_media_downloads()` 保证定时刷新不会丢已下载的文件 |
| `image_plan` 而非"图片二创" | 规格 §17 只要求分镜建议 | 当前模型是纯文本，看不到图片。给不了"看懂图片"的能力，就给能给的：封面选择、封面大字、配图顺序与配文，并明确标注 `planned_blind` 与"严禁描述图片内容" |
| 新增 `origin` / `source_keyword` | 规格未规定 | 榜单词条与搜索帖子是两种东西，混在一起不标注来源会让"这条为什么在这"无法回答 |
| `/api/system/status` 的 Database | `not_configured` | Phase 1 无数据库（规格如此），不谎报 connected |

---

## 后续阶段

- ✅ Phase 1 数据采集（TikHub → 统一 HotContent → FastAPI）
- ✅ Phase 2 数据库、四层去重、跨平台话题聚合（PostgreSQL 16 + Alembic）
- ✅ Phase 3 DeepSeek 热点分析、筛选、账号定位、话题摘要（含可选的语义归并）
- ✅ Phase 4 AI 二创（三平台版本、§18 代码级反机械改写、§19/§20 状态链）
- ✅ Phase 5 定时任务（APScheduler、完整管线、任务记录、防重复执行、手动执行接口）
- ✅ §16 详情抓取（三平台各自的端点与语义，默认关闭因每条计费）
- ✅ Phase 6 通知（企业微信机器人 / QQ 官方 Bot / log 验证通道，含 §25 格式与 §28 拆分）
- ✅ Phase 7 Vue 后台（总览/热点/话题/二创审核/任务/设置；同源托管、计费二次确认、密钥只写不读）
- ✅ Phase 8 MCP 服务（12 个工具挂在同一进程的 `/mcp/`，8 免费只读 + 4 计费受闸门管控）
- ✅ Phase 9 关键词搜索 + 媒体落库 + 图文排版方案（搜索 13 个 MCP 工具，媒体存本地素材库）
- ✅ Phase 10 领域聚焦 + README 转推广文案（免费打分重排、付费定时领域搜索、三平台推广生成）
- ✅ Phase 11 图片二创（千问图生图：本地图 base64 入参、按张计费、结果下载进素材库）
- ✅ Phase 12 QQ 图文推送（官方 Bot API、分片上传、按平台分条、被动回复分批）
- ✅ Phase 13 知识科普 + 3D 标签球（面向计算机大类，搜图配图，Three.js 标签球，流式进度）

### 四个阶段回答了四个"能不能"

| 问题 | 答案 |
|---|---|
| 能不能自己搜感兴趣的话题 | **能**：`POST /api/hot/search`、界面「搜索话题」、MCP `search_topic`；每平台 1 次计费调用 |
| 热搜能不能带图文和视频 | **词条本身不能**（微博/抖音热搜是关键词）；**按词条搜索展开就能**，这正是搜索做的事 |
| 图片能不能二创 | **文案能、排版能、看懂图不能**（无视觉模型）；图片真下载到本地，不会因链接过期失效 |
| 能不能只推我关心的领域 | **排序能（免费）+ 主动搜回来能（付费）**；实测现有榜单只有 1% 与领域相关，所以两者缺一不可 |
| README 能不能转成小红书帖子 | **能**，同时出微博和抖音版；一次生成约 ¥0.04，且不许编造 README 里没有的数据 |
- 规格的阶段已全部实现。

Phase 8 之后的系统边界（**这一点不会改变**）：管线到"生成三平台草稿 + 通知"为止，
**没有任何自动发布能力**。AI 客户端能读到全部内容、能触发采集与分析，
但发布的动作只能由人复制文案后自行完成。

`docker-compose.yml` 里的 PostgreSQL 服务是给**用 Docker 的环境**准备的备选方案；
本机没有 Docker，因此 Phase 2 用的是 `initdb` 建的本地集群。

---

## 密钥与部署

### 密钥放在哪里

密钥**只在本地 `.env`**（被 `.gitignore` 排除），仓库里只有 `.env.example` 空模板。
换机器时手工把 `.env` 拷过去——就这样，不需要任何额外工具。

### 部署到另一台电脑

**① 先在新机器上准备好 `.env`**（这一步在 clone 之前或之后都行）：

```bash
# 从旧机器拷（scp / U 盘 / 密码管理器 / 网盘，任选）
scp 旧机器:.../social-hot-ai/.env  social-hot-ai/.env
```

**② 然后走常规部署**：

```bash
git clone git@github.com:apodxx/social-hot-ai.git
cd social-hot-ai

# 后端
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt      # Linux/macOS: .venv/bin/python

# 数据库（.env 里的 DATABASE_URL 指向 127.0.0.1:55432）
#   有 Docker 就用仓库里的 docker-compose.yml；没有就 initdb 建本地集群
docker compose up -d postgres

# 迁移
.venv\Scripts\python -m alembic upgrade head

# 前端
cd ..\frontend
npm install && npm run build

# 启动
cd ..\backend
.venv\Scripts\python -m app.main
```

**③ 体检一遍**——先看这份清单，再启动：

| 要检查的项 | 为什么 |
|---|---|
| `DATABASE_URL` | 端口 `55432` 是本机 `initdb` 集群用的；用 docker-compose 通常是 `5432`，用户/库名也可能不同 |
| `APP_HOST` | `127.0.0.1` 只监听本机；要在局域网访问就改 `0.0.0.0` |
| 四个密钥字段是否都非空 | 空值会让对应功能以 `not configured` 失败 |
| `MEDIA_ROOT` | 留空则用项目下的 `media/`，通常不用改 |

仓库里有脚本可以一眼看出**哪些字段是空的、哪些值绑定了本机**（密钥值会遮蔽）：

```bash
python backend/scripts/audit_env_for_deploy.py
```

它还会列出几个影响行为的开关（`SCHEDULER_ENABLED`、`IMAGE_GEN_ENABLED`、`QQ_ENABLED`
等）——换机器时值得确认一遍，尤其是**定时任务**：`SCHEDULER_ENABLED=true` 会让采集
自动跑起来并产生 TikHub 费用。

### 为什么不提交「加密后的 .env」

我认真评估过这个方案（age / sops / git-crypt），结论是**对这个项目的目标不划算**：

- **加密省不掉手工搬运。** 解密私钥**不能**放仓库（放进去等于没加密），所以它同样必须
  手工搬到新机器——和直接拷 `.env` 是同一件事。
- **却要多装一个工具、多跑一步。** 两台机器都要装 `age`，部署时先解密再启动。
- **换来的是"配置有版本历史"**，而 `.env` 只有 48 行、改动不频繁，这个收益很小。

**什么时候值得重新考虑**：如果你以后要频繁改 `.env`、或者有第三台以上的机器、或者希望
"clone 下来就自带配置"，那加密方案就开始划算了。

另一种不需要额外工具的思路：把密钥放进 **仓库 Settings → Secrets and variables → Actions**
（加密存储、不进文件树），部署时注入 `TIKHUB_API_KEY` 等环境变量——代码本来就通过
`pydantic-settings` 读环境变量，所以**不用改一行代码**。

### 怀疑泄漏时

**立刻轮换全部四家密钥**，改完 `.env`。轮换比"删掉再强推"可靠，因为强推清不掉
已经 clone 出去的历史。

| 服务 | 轮换入口 |
|---|---|
| TikHub | https://api.tikhub.io 控制台 → API Keys |
| DeepSeek | https://platform.deepseek.com → API keys |
| 阿里云百炼（DashScope） | 百炼控制台 → API-KEY 管理 |
| QQ 机器人 | https://q.qq.com → 开发设置 → AppSecret 重置 |

### 附：GitHub 会拦截含密钥的推送

试过把 `.env` 提交上去，**被 GitHub 自己的 push protection 拦下了**：

```
remote: error: GH013: Repository rule violations found for refs/heads/master
remote: - GITHUB PUSH PROTECTION
remote:     - Push cannot contain secrets
remote:       —— DeepSeek API Key ——  commit: 19936b1  path: .env:8
```

也就是说"先提交看看"这条路本身走不通——而且**以后每次改 `.env`（比如轮换密钥）都会被
再拦一次**。这是选 C 而不是 A 的直接原因之一。
