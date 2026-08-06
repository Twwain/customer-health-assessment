import datetime
from typing import Any
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
    key: str = Field(default="", description="维度标识（来自 scoring_config.yaml）")
    name: str
    score: float
    max_score: float
    details: list[str]


class AlertItem(BaseModel):
    """结构化风险预警，供前端 AlertBadge 做色彩编码（high=红/medium=黄/low=蓝）。"""

    id: str
    level: str = Field(default="medium", description="high / medium / low")
    message: str


class AssessmentResponse(BaseModel):
    customer_id: int
    customer_name: str
    total_score: float
    max_score: float = 100
    level: str
    level_color: str
    dimensions: list[DimensionScore]
    risk_alerts: list[str]
    alerts: list[AlertItem] = Field(default_factory=list)
    suggestions: list[str]
    config_version: str = ""
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


# ── 因子配置（GET /api/customers/factor-config）─────────────────────────────
# 前端据此动态渲染客情因子表单，新增因子无需改前端代码。


class FactorInputSpec(BaseModel):
    type: str = "text"
    options: list[str] = Field(default_factory=list)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    placeholder: str = ""


class FactorConfigItem(BaseModel):
    field: str
    label: str
    weight: float
    source: str = Field(default="model", description="model=模型列 / custom_fields=扩展字段")
    source_role: str = Field(default="", description="因子来源角色：销售/研发/市场/HR ...")
    description: str = ""
    rule_text: str = Field(default="", description="打分规则的人类可读文案（如 ≥100% → 2.1分）")
    rule_type: str = ""
    editable: bool = True
    input: FactorInputSpec


class DimensionConfigItem(BaseModel):
    key: str
    name: str
    max_score: float
    enabled: bool = True
    description: str = ""
    factors: list[FactorConfigItem]


class LevelConfigItem(BaseModel):
    name: str
    min_score: float
    color: str


class FactorConfigResponse(BaseModel):
    version: str
    updated_at: str
    description: str = ""
    strategy: str
    total_max_score: float
    dimensions: list[DimensionConfigItem]
    levels: list[LevelConfigItem]


# ── 因子编辑（PUT /api/customers/{id}/factors）──────────────────────────────


class FactorUpdateRequest(BaseModel):
    """按「字段名 → 值」提交因子，字段白名单来自 scoring_config.yaml。"""

    factors: dict[str, Any] = Field(default_factory=dict)


class FactorUpdateResponse(BaseModel):
    customer: CustomerResponse
    assessment: AssessmentResponse
    updated_fields: list[str] = Field(default_factory=list)
    ignored_fields: list[str] = Field(default_factory=list)


# ── 评估历史与趋势─────────────────────────────────────


class AssessmentHistoryItem(BaseModel):
    id: int
    customer_id: int
    assessed_by: str
    trigger: str
    total_score: float
    max_score: float
    level: str
    level_color: str
    dimensions: list[DimensionScore] = Field(default_factory=list)
    risk_alerts: list[str] = Field(default_factory=list)
    factor_snapshot: dict[str, Any] = Field(default_factory=dict)
    strategy_snapshot: list[Any] = Field(default_factory=list)
    config_version: str = ""
    assessed_at: datetime.datetime

    model_config = {"from_attributes": True}


class AssessmentHistoryResponse(BaseModel):
    customer_id: int
    customer_name: str
    total: int
    items: list[AssessmentHistoryItem]


class TrendPoint(BaseModel):
    assessed_at: datetime.datetime
    label: str = Field(description="用于图表 X 轴的短标签，如 06-24")
    total_score: float
    level: str
    dimensions: dict[str, float] = Field(default_factory=dict)


class AssessmentTrendResponse(BaseModel):
    customer_id: int
    customer_name: str
    max_score: float = 100
    points: list[TrendPoint] = Field(default_factory=list)
    latest_score: float = 0
    previous_score: float | None = None
    delta: float = 0
    trend: str = Field(default="flat", description="up / down / flat")
    level: str = ""
    level_color: str = ""
    level_lines: list[LevelConfigItem] = Field(
        default_factory=list, description="等级阈值参考线（风险线/良好线等）"
    )


# ── AI 对话───────────────────────────────────────


class StrategyItem(BaseModel):
    """结构化策略建议（前端 StrategyItem 组件直接渲染）。"""

    priority: str = Field(default="recommended", description="recommended / alternative / long_term")
    title: str
    urgency: str = Field(default="medium", description="high / medium / low")
    reason: str = ""
    action: str = ""
    expected_outcome: str = ""
    reference: str = Field(default="", description="知识溯源：引用的知识条目")


class KnowledgeReference(BaseModel):
    """RAG 引用来源（M3 接入后填充，供 📎 溯源抽屉定位原文）。"""

    id: str = ""
    title: str = ""
    category: str = ""
    score: float = 0
    snippet: str = ""
    chunk_id: int | None = None


class ChatMessageItem(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    references: list[Any] = Field(default_factory=list)
    strategy_items: list[Any] = Field(default_factory=list)
    tokens_used: int = 0
    feedback: str = ""
    degraded: bool = False
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ChatSessionCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    customer_id: int | None = None
    scenario: str = Field(default="free_qa", description="free_qa / assessment / strategy / alert_analysis")
    system_prompt: str = ""


class ChatSessionItem(BaseModel):
    id: int
    title: str
    customer_id: int | None = None
    customer_name: str = ""
    scenario: str = "free_qa"
    message_count: int = 0
    last_message: str = ""
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ChatSessionDetail(ChatSessionItem):
    system_prompt: str = ""
    messages: list[ChatMessageItem] = Field(default_factory=list)


class ChatSessionListResponse(BaseModel):
    items: list[ChatSessionItem] = Field(default_factory=list)
    total: int = 0


class ChatRequest(BaseModel):
    """发送消息 / 快捷场景共用的请求体。"""

    content: str = ""
    scenario: str | None = None
    customer_id: int | None = None
    stream: bool = Field(default=True, description="true=SSE 流式；false=一次性返回 JSON")


class ChatTurnResponse(BaseModel):
    """非流式模式下的一轮对话结果。"""

    session_id: int
    message: ChatMessageItem | None = None
    assessment: AssessmentResponse | None = None
    trend: AssessmentTrendResponse | None = None
    strategy_items: list[StrategyItem] = Field(default_factory=list)
    references: list[Any] = Field(default_factory=list)
    degraded: bool = False
    tokens_used: int = 0
    latency_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class MessageFeedbackRequest(BaseModel):
    feedback: str = Field(default="", description="up / down / 空字符串取消")


class MessageFeedbackResponse(BaseModel):
    id: int
    feedback: str


class LLMStatusResponse(BaseModel):
    """前端 AI 服务状态数据源（Chat 页降级提示条等）。"""

    available: bool = False
    degraded: bool = True
    provider: str = ""
    model: str = ""
    reason: str = ""
    embedding_provider: str = ""
    embedding_model: str = ""
    embedding_available: bool = False
    prompt_version: str = ""
    scenarios: list[str] = Field(default_factory=list)


# ════════════════════════ 知识库 RAG═══════════════════════


class KnowledgeItemResponse(BaseModel):
    """知识条目（对外浏览 / 检索的聚合视图）。"""

    id: int
    document_id: int
    title: str
    category: str
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    storage: str = "vector"
    status: str = "proposed"
    adoption_count: int = 0
    hit_count: int = 0
    chunk_count: int = 0
    created_by: str = "system"
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class KnowledgeItemListResponse(BaseModel):
    items: list[KnowledgeItemResponse] = Field(default_factory=list)
    total: int = 0


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="检索自然语言")
    customer_id: int | None = None
    category: str | None = None
    status: str = Field(default="canonical", description="canonical / proposed / all")
    top_k: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchResult(BaseModel):
    """单条检索命中（含溯源信息）。"""

    document_id: int
    chunk_index: int
    item_id: int
    item_title: str
    category: str
    content: str
    score: float


class KnowledgeSearchResponse(BaseModel):
    query: str
    results: list[KnowledgeSearchResult] = Field(default_factory=list)


class KnowledgeUploadResponse(BaseModel):
    document_id: int
    item_id: int
    title: str
    category: str
    index_status: str
    chunk_count: int = 0
    index_error: str = ""


class KnowledgeUpdateRequest(BaseModel):
    """仅编辑元数据（正文不可编辑，）。"""

    title: str | None = None
    category: str | None = None
    tags: list[str] | None = None


class KnowledgeReindexRequest(BaseModel):
    category: str | None = None


class KnowledgeReindexResponse(BaseModel):
    reindexed: int = 0


class KnowledgeStatusResponse(BaseModel):
    """知识库健康状态（前端知识库页 / 降级态参考）。"""

    vector_store: str = ""
    count: int = 0
    embedding_available: bool = False
    reranker: str = ""
    categories: list[str] = Field(default_factory=list)


# ── 结构化知识指标─────────


class KnowledgeMetricCreate(BaseModel):
    metric_key: str = Field(..., min_length=1, max_length=80)
    metric_name: str = Field(default="", max_length=100)
    metric_value: float = 0
    unit: str = Field(default="", max_length=20)
    industry: str = Field(default="", max_length=50)
    region: str = Field(default="", max_length=50)
    scale: str = Field(default="", max_length=50)
    dimension_key: str = Field(default="", max_length=50)
    period: str = Field(default="", max_length=30)
    notes: str = ""
    category: str = Field(default="内部指标", max_length=30)


class KnowledgeMetricResponse(KnowledgeMetricCreate):
    id: int
    document_id: int | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class KnowledgeMetricListResponse(BaseModel):
    items: list[KnowledgeMetricResponse] = Field(default_factory=list)
    total: int = 0
