from config import SCORING_STRATEGY
from .base import ScoringStrategy
from .rule_based import RuleBasedStrategy
from .ml_placeholder import MLPlaceholderStrategy


_strategies = {
    "rule_based": RuleBasedStrategy,
    "ml": MLPlaceholderStrategy,
}


def get_scoring_strategy() -> ScoringStrategy:
    cls = _strategies.get(SCORING_STRATEGY)
    if cls is None:
        raise ValueError(f"未知评分策略: {SCORING_STRATEGY}，可用: {list(_strategies.keys())}")
    return cls()
