import datetime
from sqlalchemy import String, Integer, Float, Boolean, Text, Date, DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
