"""ORM models.

Importing this package registers every table on ``Base.metadata``, which is what
``create_all`` and Alembic's autogenerate both rely on.
"""

from app.models.ai_analysis import AiAnalysisRecord
from app.models.ai_rewrite import AiRewriteRecord, RewriteStatus
from app.models.hot_content import ContentType, HotContent, HotContentRecord, Platform
from app.models.image_generation import ImageGenerationRecord
from app.models.knowledge import KnowledgeArticleRecord, KnowledgeTagRecord
from app.models.readme_promo import ReadmePromoRecord
from app.models.task import PipelineTaskRecord, TaskStatus
from app.models.topic_group import TopicGroupRecord

__all__ = [
    "AiAnalysisRecord",
    "AiRewriteRecord",
    "ContentType",
    "HotContent",
    "HotContentRecord",
    "ImageGenerationRecord",
    "KnowledgeArticleRecord",
    "KnowledgeTagRecord",
    "PipelineTaskRecord",
    "Platform",
    "ReadmePromoRecord",
    "RewriteStatus",
    "TaskStatus",
    "TopicGroupRecord",
]
