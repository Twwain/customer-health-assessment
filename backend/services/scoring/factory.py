from config import SCORING_STRATEGY
from .base import ScoringStrategy
from .config_driven import ConfigDrivenStrategy


_strategies = {
    "rule_based": ConfigDrivenStrategy,  # 保留现有部署配置名
    "config": ConfigDrivenStrategy,    # 同上，语义更直白的别名
}


def get_scoring_strategy() -> ScoringStrategy:
    cls = _strategies.get(SCORING_STRATEGY)
    if cls is None:
        raise ValueError(f"未知评分策略: {SCORING_STRATEGY}，可用: {list(_strategies.keys())}")
    return cls()
