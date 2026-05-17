import datetime
from pydantic import BaseModel, Field


class CustomerBase(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=100)
    industry: str = Field(default="", max_length=50)
    contact_person: str = Field(default="", max_length=50)
    contact_phone: str = Field(default="", max_length=20)
    cooperation_years: float = Field(default=0, ge=0)
    contact_frequency: str = Field(default="每月")
    last_contact_date: datetime.date | None = None
    customer_satisfaction: int = Field(default=5, ge=1, le=10)
    contract_amount: float = Field(default=0, ge=0)
    payment_status: str = Field(default="正常")
    risk_signals: str = Field(default="", max_length=500)
    competitor_involvement: bool = False
    growth_potential: str = Field(default="中")
    notes: str = Field(default="")
    custom_fields: dict = Field(default_factory=dict)


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    customer_name: str | None = Field(None, min_length=1, max_length=100)
    industry: str | None = Field(None, max_length=50)
    contact_person: str | None = Field(None, max_length=50)
    contact_phone: str | None = Field(None, max_length=20)
    cooperation_years: float | None = Field(None, ge=0)
    contact_frequency: str | None = None
    last_contact_date: datetime.date | None = None
    customer_satisfaction: int | None = Field(None, ge=1, le=10)
    contract_amount: float | None = Field(None, ge=0)
    payment_status: str | None = None
    risk_signals: str | None = Field(None, max_length=500)
    competitor_involvement: bool | None = None
    growth_potential: str | None = None
    notes: str | None = None
    custom_fields: dict | None = None


class CustomerResponse(CustomerBase):
    id: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class CustomerListResponse(BaseModel):
    items: list[CustomerResponse]
    total: int
    page: int
    page_size: int


class DimensionScore(BaseModel):
    name: str
    score: float
    max_score: float
    details: list[str]


class AssessmentResponse(BaseModel):
    customer_id: int
    customer_name: str
    total_score: float
    level: str
    level_color: str
    dimensions: list[DimensionScore]
    risk_alerts: list[str]
    suggestions: list[str]
    assessed_at: datetime.datetime


class CustomerHealthSummary(BaseModel):
    customer_id: int
    customer_name: str
    industry: str
    total_score: float
    level: str
    level_color: str


class OverviewResponse(BaseModel):
    total_customers: int
    avg_score: float
    risk_count: int
    level_distribution: dict[str, int]
    recent_customers: list[CustomerHealthSummary]
    risk_customers: list[CustomerHealthSummary]
