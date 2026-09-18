"""算法超参数固化常量表：提供检索、融合打分、反思衰减的标准参数。"""

from __future__ import annotations

# BM25 相关标准超参数（成熟 Okapi BM25 默认值）
BM25_K1 = 1.5
BM25_B = 0.75

# 多路召回 RRF 融合超参数
RRF_K = 60
KEYWORD_SCORE_WEIGHT = 0.15
EMBED_SCORE_WEIGHT = 0.10
RECENCY_SCORE_WEIGHT = 0.05
PINNED_BOOST_SCORE = 0.05

# 检索过滤门槛
RERANK_MIN_SCORE = 0.35
LOW_INFO_WORD_THRESHOLD = 3

# 反思与数据维护默认超参数
DEFAULT_ARCHIVE_CHECK_INTERVAL = 86400  # 每天一次
DEFAULT_STALE_CLEANUP_DAYS = 90         # 90 天未活跃事实清理
