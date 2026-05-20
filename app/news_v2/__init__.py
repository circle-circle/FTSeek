"""
FTSeek-US Unified News Pipeline V2

架构:
  Sources (采集层) → Quality Gate (质量门禁) → Pipeline (编排器) → External Systems

用法:
    from app.news_v2 import NewsPipelineV2
    from app.news_v2.sources.rsshub_us_source import RSSHubUSSource
    
    pipeline = NewsPipelineV2()
    pipeline.add_source(RSSHubUSSource())
    await pipeline.run_once()
"""

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.pipeline import NewsPipelineV2, QualityGate
from app.news_v2.quality.dedup_v2 import DedupEngineV2
from app.news_v2.sources.base import SourceAdapter

__all__ = [
    "UnifiedNewsItemV2",
    "NewsUrgency", 
    "NewsSource",
    "NewsPipelineV2",
    "QualityGate",
    "DedupEngineV2",
    "SourceAdapter",
]
