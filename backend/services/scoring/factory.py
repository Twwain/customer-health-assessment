from config import SCORING_STRATEGY
from .base import ScoringStrategy
from .config_driven import ConfigDrivenStrategy
from .rule_based import RuleBasedStrategy
from .ml_placeholder import MLPlaceholderStrategy


_strategies = {
    "rule_based": RuleBasedStrategy,   # 默认：配置驱动（scoring_config.yaml）
    "config": ConfigDrivenStrategy,    # 同上，语义更直白的别名
    "ml": MLPlaceholderStrategy,       # 预留：接入模型后切换
}


def get_scoring_strategy() -> ScoringStrategy:
    cls = _strategies.get(SCORING_STRATEGY)
    if cls is None:
        raise ValueError(f"未知评分策略: {SCORING_STRATEGY}，可用: {list(_strategies.keys())}")
    return cls()
