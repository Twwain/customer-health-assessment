"""默认评分策略。

早期这里是硬编码的 4 维度算法；（M0）改造后，
规则全部下沉到 ``backend/scoring_config.yaml``，本类只是配置驱动引擎的别名，
保留类名以兼容 `SCORING_STRATEGY = "rule_based"` 配置与既有引用。
"""

from .config_driven import ConfigDrivenStrategy


class RuleBasedStrategy(ConfigDrivenStrategy):
    """规则评分策略 = 配置驱动引擎 + scoring_config.yaml 默认配置。"""
