"""知识科普模块的两张表（Phase 13）。

分开建表而不是复用 ``hot_contents``：这里的内容**不是采集来的热点**，而是我们为
计算机大类学生生成的原创科普，生命周期、字段、计费方式都不同（按 token 计费，没有平台
来源、没有热度、没有去重窗口）。

* :class:`KnowledgeTagRecord` —— 标签词表，也是 3D 标签球的数据源。**存库、免费复用**，
  只有显式「刷新标签」才调模型，所以打开页面不花钱。
* :class:`KnowledgeArticleRecord` —— 一篇科普文章及其三平台文案、配图、标签快照。

标签与文章是多对多（一个标签可以生成多篇文章，一篇文章带多个标签），但文章里保存的是
**标签名快照**而不是外键：文章写完后即使词表被刷新，这篇文章当时用了哪些标签仍然是事实。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeTagRecord(Base):
    """一个知识标签（标签球上的一个点）。"""

    __tablename__ = "knowledge_tags"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 展示名，唯一——球体上不能有两个同名点。
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: 分类，用于球体着色：基础理论 / 编程语言 / 系统网络 / 数据与AI / 工程实践 / 职业发展…
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 难度：入门 / 进阶 / 高阶。用于筛选。
    difficulty: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: 一句话说明，鼠标悬停时显示。
    blurb: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 权重：越大球体上的点越大。由生成顺序与文章数共同决定。
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: 已为这个标签生成过几篇文章（点击生成后 +1）。
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 词表来源：model（模型生成）或 manual（人工添加）。
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="model")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KnowledgeTag {self.name} kind={self.kind}>"


class KnowledgeArticleRecord(Base):
    """一篇生成好的知识科普文章。"""

    __tablename__ = "knowledge_articles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: 触发这篇文章的标签/主题。
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: 可以为空：允许后来删除标签而保留文章。
    tag_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    hook: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 正文分节：[{heading, body, key_points: [...]}]
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: 术语表：[{term, explanation}]
    glossary: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: 延伸阅读/实践建议：[{title, note}]
    further_reading: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    #: 面向人群与难度，让审核的人一眼看出适配度。
    audience: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    difficulty: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: 读完应该掌握什么。
    takeaways: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    #: 三平台文案：{"xiaohongshu": {...}, "weibo": {...}, "douyin": {...}}
    platforms: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 配图（搜到的或生成的），每条含 local_path 与出处，便于署名。
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: 这篇文章带的标签名快照（不是外键，见模块说明）。
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 生成过程的分步记录（文章/平台/标签各自的 token 与状态），便于对账。
    steps: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KnowledgeArticle id={self.id} topic={self.topic!r}>"
