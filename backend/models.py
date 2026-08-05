"""数据模型。

v1.0 只有 Customer；v3.0 按 SOW §5 扩展出对话、评估历史与知识库三组模型：

- 对话：ChatSession / ChatMessage
- 评估历史：AssessmentHistory（支撑预警趋势箭头与历史曲线）
- 知识库：KnowledgeDocument / KnowledgeChunk / KnowledgeItem / KnowledgeMetric
  （SOW §3.3.1 知识分层：叙事文本进向量库，精确数值进 SQLite）
"""

import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base, utcnow

# ── 枚举常量（SQLite 不用原生枚举，统一在这里约束取值）───────────────────────

CHAT_ROLES = ("user", "assistant", "system")
MESSAGE_FEEDBACKS = ("", "up", "down")
KNOWLEDGE_CATEGORIES = ("内部规范", "内部指标", "外部指标", "对话沉淀")
KNOWLEDGE_SOURCE_TYPES = ("文档", "对话沉淀")
KNOWLEDGE_STATUSES = ("proposed", "canonical")
KNOWLEDGE_STORAGES = ("vector", "structured")
INDEX_STATUSES = ("pending", "indexing", "indexed", "failed", "empty")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="客户名称")
    industry: Mapped[str] = mapped_column(String(50), default="", comment="所属行业")
    contact_person: Mapped[str] = mapped_column(String(50), default="", comment="对接人")
    contact_phone: Mapped[str] = mapped_column(String(20), default="", comment="联系电话")
    cooperation_years: Mapped[float] = mapped_column(Float, default=0, comment="合作年限")
    contact_frequency: Mapped[str] = mapped_column(String(20), default="每月", comment="沟通频率")
    last_contact_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True, comment="最近联系日期")
    customer_satisfaction: Mapped[int] = mapped_column(Integer, default=5, comment="客户满意度(1-10)")
    contract_amount: Mapped[float] = mapped_column(Float, default=0, comment="合同金额(万元)")
    payment_status: Mapped[str] = mapped_column(String(20), default="正常", comment="回款情况")
    risk_signals: Mapped[str] = mapped_column(String(500), default="", comment="风险信号")
    competitor_involvement: Mapped[bool] = mapped_column(Boolean, default=False, comment="竞品介入")
    growth_potential: Mapped[str] = mapped_column(String(20), default="中", comment="增长潜力")
    notes: Mapped[str] = mapped_column(Text, default="", comment="备注")
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict, comment="自定义扩展字段")
    # 时间戳用 Python 侧 utcnow（UTC），与业务代码中的 utcnow() 口径一致；
    # server_default 仅兜底非 ORM 写入
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    assessment_history: Mapped[list["AssessmentHistory"]] = relationship(
        back_populates="customer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ══════════════════════════ 评估历史（SOW §3.5.2 / §5）══════════════════════


class AssessmentHistory(Base):
    """每次评估的快照，支撑趋势箭头（最近两次差值）与历史曲线。"""

    __tablename__ = "assessment_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True, comment="关联客户"
    )
    assessed_by: Mapped[str] = mapped_column(String(50), default="system", comment="评估触发方")
    trigger: Mapped[str] = mapped_column(String(30), default="manual", comment="触发场景")
    total_score: Mapped[float] = mapped_column(Float, default=0, comment="总分")
    max_score: Mapped[float] = mapped_column(Float, default=100, comment="满分")
    level: Mapped[str] = mapped_column(String(20), default="", comment="等级")
    level_color: Mapped[str] = mapped_column(String(20), default="", comment="等级色值")
    dimensions: Mapped[list] = mapped_column(JSON, default=list, comment="各维度分数")
    risk_alerts: Mapped[list] = mapped_column(JSON, default=list, comment="风险预警快照")
    factor_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, comment="评估时的因子取值快照")
    strategy_snapshot: Mapped[list] = mapped_column(JSON, default=list, comment="策略建议快照")
    config_version: Mapped[str] = mapped_column(String(20), default="", comment="评分配置版本")
    assessed_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    customer: Mapped["Customer"] = relationship(back_populates="assessment_history")


Index("ix_assessment_history_customer_time", AssessmentHistory.customer_id, AssessmentHistory.assessed_at)


# ══════════════════════════ AI 对话（SOW §3.2 / §5）═════════════════════════


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话", comment="会话标题")
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True, comment="关联客户"
    )
    assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assessment_history.id", ondelete="SET NULL"), nullable=True, comment="关联评估快照"
    )
    system_prompt: Mapped[str] = mapped_column(Text, default="", comment="系统 Prompt")
    scenario: Mapped[str] = mapped_column(String(30), default="free_qa", comment="对话场景")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), index=True
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMessage.id",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True, comment="所属会话"
    )
    role: Mapped[str] = mapped_column(String(20), default="user", comment="user / assistant / system")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息内容（Markdown）")
    references: Mapped[list] = mapped_column(JSON, default=list, comment="RAG 引用来源（可溯源）")
    strategy_items: Mapped[list] = mapped_column(JSON, default=list, comment="结构化策略建议")
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, comment="Token 消耗")
    feedback: Mapped[str] = mapped_column(String(10), default="", comment="用户反馈：up / down")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否为 LLM 降级兜底回复")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


# ══════════════════════════ 知识库（SOW §3.3 / §5）══════════════════════════


class KnowledgeDocument(Base):
    """知识源文档：一次上传（或一次策略采纳）= 一个文档。"""

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="文档标题")
    category: Mapped[str] = mapped_column(String(30), default="内部规范", index=True, comment="知识分类")
    source_type: Mapped[str] = mapped_column(String(20), default="文档", comment="文档 / 对话沉淀")
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="原始文件路径")
    file_size: Mapped[int] = mapped_column(Integer, default=0, comment="原始文件字节数")
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True, comment="proposed / canonical")
    index_status: Mapped[str] = mapped_column(String(20), default="pending", comment="切片与向量化状态")
    index_error: Mapped[str] = mapped_column(Text, default="", comment="索引失败原因")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, comment="切片数量")
    created_by: Mapped[str] = mapped_column(String(50), default="system", comment="创建人")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    items: Mapped[list["KnowledgeItem"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class KnowledgeChunk(Base):
    """文档切片 = 向量化最小单元；vector_id 用于删除时级联清理 Chroma。"""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True, comment="所属文档"
    )
    content: Mapped[str] = mapped_column(Text, default="", comment="切片正文")
    vector_id: Mapped[str] = mapped_column(String(64), default="", index=True, comment="Chroma 向量 ID")
    # 注意：属性名不能叫 metadata（SQLAlchemy 声明式基类保留字）
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict, comment="行业/标签/时间等过滤字段")
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, comment="在文档中的顺序")
    token_count: Mapped[int] = mapped_column(Integer, default=0, comment="切片 Token 估算")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    document: Mapped["KnowledgeDocument"] = relationship(back_populates="chunks")


class KnowledgeItem(Base):
    """知识条目：对外浏览 / 检索的聚合视图，元数据可编辑（正文不可编辑）。"""

    __tablename__ = "knowledge_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True, comment="关联源文档"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="条目标题（可编辑）")
    category: Mapped[str] = mapped_column(String(30), default="内部规范", index=True, comment="分类（可编辑）")
    tags: Mapped[list] = mapped_column(JSON, default=list, comment="标签（可编辑）")
    summary: Mapped[str] = mapped_column(Text, default="", comment="摘要，用于列表展示")
    storage: Mapped[str] = mapped_column(String(20), default="vector", comment="vector=向量库 / structured=SQLite")
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True, comment="proposed / canonical")
    adoption_count: Mapped[int] = mapped_column(Integer, default=0, comment="被采纳次数")
    hit_count: Mapped[int] = mapped_column(Integer, default=0, comment="被检索命中次数")
    created_by: Mapped[str] = mapped_column(String(50), default="system", comment="创建人")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    document: Mapped["KnowledgeDocument"] = relationship(back_populates="items")


class KnowledgeMetric(Base):
    """结构化知识指标（SOW §3.3.1 知识分层）。

    行业基准值、续约率、客户画像统计等**精确数值**不进向量库，
    评估时按行业 / 规模 / 地域精确查询，避免语义检索造成数值漂移。
    """

    __tablename__ = "knowledge_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"), nullable=True, comment="来源文档"
    )
    category: Mapped[str] = mapped_column(String(30), default="内部指标", comment="知识分类")
    metric_key: Mapped[str] = mapped_column(String(80), index=True, comment="指标标识，如 industry_avg_score")
    metric_name: Mapped[str] = mapped_column(String(100), default="", comment="指标名称")
    metric_value: Mapped[float] = mapped_column(Float, default=0, comment="指标数值")
    unit: Mapped[str] = mapped_column(String(20), default="", comment="单位")
    industry: Mapped[str] = mapped_column(String(50), default="", index=True, comment="适用行业")
    region: Mapped[str] = mapped_column(String(50), default="", comment="适用地域")
    scale: Mapped[str] = mapped_column(String(50), default="", comment="适用客户规模")
    dimension_key: Mapped[str] = mapped_column(String(50), default="", comment="对应评分维度")
    period: Mapped[str] = mapped_column(String(30), default="", comment="统计周期，如 2026H1")
    notes: Mapped[str] = mapped_column(Text, default="", comment="备注")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


Index("ix_knowledge_metrics_lookup", KnowledgeMetric.metric_key, KnowledgeMetric.industry)
