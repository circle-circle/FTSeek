"""
Embedding 相似度匹配 (N10 / 智能新闻 L3, 2026-05-14)

【可选层】 - 默认 disabled, 用户决定是否装 sentence-transformers.

设计:
- 离线模型 (一次加载, 永久使用)
- 国内可达推荐:
  - shibing624/text2vec-base-multilingual (~430MB, 多语言)
  - moka-ai/m3e-small (~70MB, 中英专精)
  - paraphrase-multilingual-MiniLM-L12-v2 (~120MB, 多语言)
- HuggingFace 镜像: https://hf-mirror.com (国内可达)

何时启用:
- 当 SmartRelevanceEngine 评分在边界 (0.3-0.5) 时, 用 embedding 二次确认
- 处理"间接相关"信号 (台积电跌 → NVDA 关联)
- 替代纯关键词的"语义相似度"

可控:
- 默认 enabled=false
- 模型 lazy-load, 失败降级
- 单条新闻 ~10ms (CPU), GPU 更快
- 完全离线 (启动后无网络依赖)
"""
from __future__ import annotations
import logging
import os
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class EmbeddingMatcher:
    """语义相似度匹配 (lazy load, 失败降级)"""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or os.environ.get(
            "EMBEDDING_MODEL", "moka-ai/m3e-small"
        )
        self._model = None
        self._company_embeddings: Dict[str, "np.ndarray"] = {}
        self._lock = threading.Lock()
        self._available = None  # None=未试, True=可用, False=失败

    def _ensure_loaded(self) -> bool:
        """lazy load — 首次调用才尝试加载. 失败永久禁用 (避免反复重试)"""
        if self._available is not None:
            return self._available
        with self._lock:
            if self._available is not None:
                return self._available
            try:
                # 国内 HuggingFace 镜像 (可通过 yaml 覆盖)
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
                logger.info(f"[EmbeddingMatcher] 模型加载成功: {self.model_name}")
                self._available = True
                return True
            except ImportError:
                logger.warning(
                    "[EmbeddingMatcher] sentence-transformers 未安装. "
                    "如需启用语义匹配, 运行 `pip install sentence-transformers`"
                )
                self._available = False
                return False
            except Exception as e:
                logger.warning(f"[EmbeddingMatcher] 模型加载失败: {e}")
                self._available = False
                return False

    def is_available(self) -> bool:
        return self._ensure_loaded()

    def encode(self, text: str) -> Optional["np.ndarray"]:
        """文本 → embedding vector. 不可用返回 None"""
        if not self._ensure_loaded():
            return None
        try:
            return self._model.encode(text, show_progress_bar=False)
        except Exception as e:
            logger.debug(f"[EmbeddingMatcher·encode] 失败: {e}")
            return None

    def cosine(self, vec_a, vec_b) -> float:
        """余弦相似度. 失败返回 0"""
        try:
            import numpy as np
            a = np.asarray(vec_a)
            b = np.asarray(vec_b)
            denom = (np.linalg.norm(a) * np.linalg.norm(b))
            if denom == 0:
                return 0.0
            return float(np.dot(a, b) / denom)
        except Exception:
            return 0.0

    def precompute_company_embeddings(self, company_descriptions: Dict[str, str]):
        """
        预计算每个 ticker 的描述 embedding (启动时调一次).

        company_descriptions: {ticker: "Apple Inc. consumer electronics, iPhone, Mac, ..."}
        """
        if not self._ensure_loaded():
            return
        for tk, desc in company_descriptions.items():
            emb = self.encode(desc)
            if emb is not None:
                self._company_embeddings[tk] = emb
        logger.info(f"[EmbeddingMatcher] 预计算 {len(self._company_embeddings)} 个公司 embedding")

    def match_news_to_tickers(
        self,
        news_text: str,
        threshold: float = 0.5,
        top_k: int = 5,
    ) -> List[Tuple[str, float]]:
        """
        给定新闻, 返回最相关 top_k tickers + 相似度分数.

        Returns: [(ticker, similarity_score)], 按 score 降序
        """
        if not self._company_embeddings:
            return []
        news_emb = self.encode(news_text)
        if news_emb is None:
            return []
        scores = []
        for tk, emb in self._company_embeddings.items():
            sim = self.cosine(news_emb, emb)
            if sim >= threshold:
                scores.append((tk, sim))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


_singleton: Optional[EmbeddingMatcher] = None


def get_embedding_matcher() -> EmbeddingMatcher:
    """单例. 第一次调用 lazy load 模型."""
    global _singleton
    if _singleton is None:
        _singleton = EmbeddingMatcher()
    return _singleton


def is_embedding_enabled() -> bool:
    """供调用方快速检查 (yaml flag)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        if not cfg.get_dotted("news_relevance.embedding.enabled", False):
            return False
        return get_embedding_matcher().is_available()
    except Exception:
        return False
