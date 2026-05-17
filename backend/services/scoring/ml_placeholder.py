from .base import ScoringStrategy
from models import Customer
from schemas import AssessmentResponse


class MLPlaceholderStrategy(ScoringStrategy):
    def evaluate(self, customer: Customer) -> AssessmentResponse:
        raise NotImplementedError(
            "ML 模型尚未接入，请在此实现模型调用逻辑。"
            "完成后将 backend/config.py 中的 SCORING_STRATEGY 改为 'ml' 即可启用。"
        )
