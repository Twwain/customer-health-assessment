from abc import ABC, abstractmethod
from models import Customer
from schemas import AssessmentResponse


class ScoringStrategy(ABC):
    @abstractmethod
    def evaluate(self, customer: Customer) -> AssessmentResponse:
        ...
