"""客情健康度评分引擎（对外统一入口）。

（M0）改造：由硬编码 4 维度算法重构为**配置驱动**——
维度 / 因子 / 权重 / 打分规则 / 预警规则均来自 ``backend/scoring_config.yaml``，
修改配置后重启服务即生效，无需改动代码。

类名与用法保持不变，历史调用方无需改动：

    engine = HealthScoreEngine()
    assessment = engine.evaluate(customer)
"""

from services.scoring.config_driven import ConfigDrivenStrategy


class HealthScoreEngine(ConfigDrivenStrategy):
    """配置驱动的客情健康度评分引擎。"""


__all__ = ["HealthScoreEngine"]
