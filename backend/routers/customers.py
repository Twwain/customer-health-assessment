import datetime
import io
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, func, or_
from sqlalchemy.orm import Session
from config import SCORING_STRATEGY
from database import get_db
from models import AssessmentHistory, Customer
from schemas import (
    CustomerCreate,
    CustomerUpdate,
    CustomerResponse,
    CustomerListResponse,
    DimensionConfigItem,
    FactorConfigItem,
    FactorConfigResponse,
    FactorInputSpec,
    FactorUpdateRequest,
    FactorUpdateResponse,
    LevelConfigItem,
)
from services import assessment_history
from services.scoring import get_scoring_strategy, load_scoring_config
from services.scoring.config_loader import FactorConfig
import openpyxl

router = APIRouter(prefix="/customers", tags=["客情信息"])


@router.get("", response_model=CustomerListResponse)
def list_customers(
    search: str = Query(default="", description="搜索客户名称/行业/对接人"),
    industry: str = Query(default="", description="按行业筛选"),
    level: str = Query(default="", description="按健康等级筛选（等级名来自 scoring_config.yaml 的 levels）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(Customer)
    if search:
        query = query.filter(
            or_(
                Customer.customer_name.contains(search),
                Customer.industry.contains(search),
                Customer.contact_person.contains(search),
            )
        )
    if industry:
        query = query.filter(Customer.industry == industry)

    if level:
        # 等级筛选基于最近一次评估快照（AssessmentHistory.level）走 SQL 过滤，
        # 避免对全表逐条实时评分；快照在建档/编辑/因子更新/AI 评估时均会刷新。
        # 无快照的客户（理论上建档即落基线）回退为实时评估，保证语义完整。
        latest = (
            db.query(
                AssessmentHistory.customer_id,
                func.max(AssessmentHistory.id).label("max_id"),
            )
            .group_by(AssessmentHistory.customer_id)
            .subquery()
        )
        snapshot_rows = (
            db.query(AssessmentHistory.customer_id, AssessmentHistory.level)
            .join(latest, AssessmentHistory.id == latest.c.max_id)
            .all()
        )
        level_by_customer = {cid: lv for cid, lv in snapshot_rows}
        all_ids = [cid for (cid,) in query.with_entities(Customer.id).all()]
        missing = [cid for cid in all_ids if cid not in level_by_customer]
        if missing:
            engine = get_scoring_strategy()
            for c in db.query(Customer).filter(Customer.id.in_(missing)).all():
                level_by_customer[c.id] = engine.evaluate(c).level
        matched = [cid for cid in all_ids if level_by_customer.get(cid) == level]
        query = query.filter(Customer.id.in_(matched) if matched else Customer.id == -1)

    total = query.count()
    items = query.order_by(Customer.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return CustomerListResponse(
        items=[CustomerResponse.model_validate(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/industries")
def list_industries(db: Session = Depends(get_db)):
    results = db.query(Customer.industry).distinct().filter(Customer.industry != "").all()
    return [r[0] for r in results]


# 注意：本路由必须声明在 `/{customer_id}` 之前，否则会被当成客户 ID 解析
@router.get("/factor-config", response_model=FactorConfigResponse)
def get_factor_config():
    """返回当前因子配置（来自 scoring_config.yaml），供前端动态渲染因子表单。

    SOW §3.0.2 / §6.2：新增因子只需在配置里注册，前后端均无需改代码。
    """
    config = load_scoring_config()
    return FactorConfigResponse(
        version=config.version,
        updated_at=config.updated_at,
        description=config.description,
        strategy=SCORING_STRATEGY,
        total_max_score=config.total_max_score,
        dimensions=[
            DimensionConfigItem(
                key=dim.key,
                name=dim.name,
                max_score=dim.max_score,
                enabled=dim.enabled,
                description=dim.description,
                factors=[
                    FactorConfigItem(
                        field=f.field,
                        label=f.label,
                        weight=f.weight,
                        source=f.source,
                        source_role=f.source_role,
                        description=f.description,
                        rule_type=f.rule.type,
                        editable=f.input.editable,
                        input=FactorInputSpec(
                            type=f.input.type,
                            options=f.input.options,
                            min=f.input.min,
                            max=f.input.max,
                            step=f.input.step,
                            unit=f.input.unit,
                            placeholder=f.input.placeholder,
                        ),
                    )
                    for f in dim.factors
                ],
            )
            for dim in config.dimensions
        ],
        levels=[
            LevelConfigItem(name=lv.name, min_score=lv.min_score, color=lv.color)
            for lv in config.levels
        ],
    )


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return CustomerResponse.model_validate(customer)


@router.post("", response_model=CustomerResponse)
def create_customer(data: CustomerCreate, db: Session = Depends(get_db)):
    customer = Customer(**data.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    # 建档即落一条评估基线，趋势图从第一天就有数据
    assessment_history.record_assessment(
        db, customer, assessed_by="system", trigger="create", skip_if_unchanged=False
    )
    return CustomerResponse.model_validate(customer)


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(customer_id: int, data: CustomerUpdate, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    # 与 PUT /factors 同一校验口径：注册为因子的字段须满足配置中的可选项约束，
    # 防止整体更新绕开因子校验写入非法枚举值，导致规则兜底、分数静默漂移
    config = load_scoring_config()
    for key, value in data.model_dump(exclude_unset=True).items():
        factor = config.find_factor(key)
        if factor is not None and factor.input.options and value not in (None, ""):
            _validate_choice(factor, str(value))
        setattr(customer, key, value)
    db.commit()
    db.refresh(customer)
    # 因子有变化才记历史（仅改备注等非因子字段不会产生新快照）
    assessment_history.record_assessment(db, customer, assessed_by="编辑客户", trigger="update")
    return CustomerResponse.model_validate(customer)


@router.put("/{customer_id}/factors", response_model=FactorUpdateResponse)
def update_customer_factors(
    customer_id: int,
    data: FactorUpdateRequest,
    db: Session = Depends(get_db),
):
    """更新客情因子并即时重算基础客情分。

    - 字段白名单来自 `scoring_config.yaml`，未注册字段会被忽略（返回 ignored_fields）
    - `source: custom_fields` 的扩展因子自动写入 Customer.custom_fields
    - 评审结论 Q6：仅桌面端调用，移动端为只读视图
    """
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")

    config = load_scoring_config()
    updated_fields: list[str] = []
    ignored_fields: list[str] = []
    custom_fields = dict(customer.custom_fields or {})

    for name, raw_value in data.factors.items():
        factor = config.find_factor(name)
        if factor is None or not factor.input.editable:
            ignored_fields.append(name)
            continue

        if factor.source == "custom_fields":
            custom_fields[name] = _coerce_by_input(factor, raw_value)
        else:
            column = Customer.__table__.columns.get(name)
            if column is None:
                ignored_fields.append(name)
                continue
            setattr(customer, name, _coerce_by_column(factor, column, raw_value))
        updated_fields.append(name)

    if custom_fields != (customer.custom_fields or {}):
        customer.custom_fields = custom_fields

    db.commit()
    db.refresh(customer)

    engine = get_scoring_strategy()
    assessment = engine.evaluate(customer)
    # 因子变更即重算并写入历史，供趋势曲线对比
    assessment_history.record_assessment(
        db, customer, assessment, assessed_by="因子编辑", trigger="factor_update"
    )

    return FactorUpdateResponse(
        customer=CustomerResponse.model_validate(customer),
        assessment=assessment,
        updated_fields=updated_fields,
        ignored_fields=ignored_fields,
    )


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _validate_choice(factor: FactorConfig, value: Any) -> Any:
    options = factor.input.options
    if options and str(value) not in options:
        raise HTTPException(
            status_code=400,
            detail=f"因子「{factor.label}」的值 `{value}` 不在可选项 {options} 中",
        )
    return value


def _validate_range(factor: FactorConfig, number: float) -> float:
    spec = factor.input
    if spec.min is not None and number < float(spec.min):
        raise HTTPException(status_code=400, detail=f"因子「{factor.label}」不能小于 {spec.min}")
    if spec.max is not None and number > float(spec.max):
        raise HTTPException(status_code=400, detail=f"因子「{factor.label}」不能大于 {spec.max}")
    return number


def _parse_number(factor: FactorConfig, value: Any) -> float:
    try:
        return _validate_range(factor, float(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"因子「{factor.label}」需要数字，收到 `{value}`")


def _parse_date(factor: FactorConfig, value: Any) -> datetime.date | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    raise HTTPException(
        status_code=400, detail=f"因子「{factor.label}」日期格式应为 YYYY-MM-DD，收到 `{value}`"
    )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "y", "是")


def _coerce_by_column(factor: FactorConfig, column: Any, value: Any) -> Any:
    """按 Customer 模型列类型做类型转换与校验。"""
    col_type = column.type

    if isinstance(col_type, Boolean):
        return _parse_bool(value)
    if isinstance(col_type, (Date, DateTime)):
        return _parse_date(factor, value)
    if isinstance(col_type, Integer):
        if _is_blank(value):
            return 0
        return int(_parse_number(factor, value))
    if isinstance(col_type, Float):
        if _is_blank(value):
            return 0.0
        return _parse_number(factor, value)

    text = "" if value is None else str(value).strip()
    if text and factor.input.options:
        _validate_choice(factor, text)
    return text


def _coerce_by_input(factor: FactorConfig, value: Any) -> Any:
    """custom_fields 扩展因子按 input.type 做类型转换。"""
    input_type = factor.input.type

    if input_type == "bool":
        return _parse_bool(value)
    if input_type == "date":
        parsed = _parse_date(factor, value)
        return parsed.isoformat() if parsed else None
    if input_type in ("number", "slider"):
        return None if _is_blank(value) else _parse_number(factor, value)
    if _is_blank(value):
        return None

    text = str(value).strip()
    if factor.input.options:
        _validate_choice(factor, text)
    return text


@router.delete("/{customer_id}")
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    db.delete(customer)
    db.commit()
    return {"ok": True}


@router.post("/import")
def import_customers(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """批量导入（同步 def：解析 + 写库较重，交给 FastAPI 线程池，避免阻塞事件循环）。

    仅支持 .csv（UTF-8 / GBK 自动回退）与 .xlsx；老版 .xls 需另存为 .xlsx。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="请选择文件")

    content = file.file.read()
    filename = file.filename.lower()

    if filename.endswith(".csv"):
        return _import_csv(content, db)
    elif filename.endswith(".xls"):
        raise HTTPException(status_code=400, detail="不支持老版 .xls 格式，请在 Excel 中另存为 .xlsx 后重试")
    elif filename.endswith(".xlsx"):
        return _import_excel(content, db)
    else:
        raise HTTPException(status_code=400, detail="仅支持 CSV 或 Excel(.xlsx) 文件")


def _import_csv(content: bytes, db: Session):
    import csv

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            # 国内 Excel 导出的 CSV 常见 GBK 编码
            text = content.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="CSV 编码无法识别（仅支持 UTF-8 / GBK）")
    reader = csv.DictReader(io.StringIO(text))
    return _process_rows(list(reader), db)


def _import_excel(content: bytes, db: Session):
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Excel 文件无法解析：{exc}")
    ws = wb.active
    if ws is None:
        raise HTTPException(status_code=400, detail="Excel 中没有可用的工作表")
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")

    headers = [str(h) if h else "" for h in rows[0]]
    records = []
    for row in rows[1:]:
        record = {}
        for i, value in enumerate(row):
            if i < len(headers):
                record[headers[i]] = value
        records.append(record)
    return _process_rows(records, db)


def _process_rows(records: list[dict], db: Session):
    """批量写入客户行。

    因子列（含 custom_fields 扩展因子）统一走与「编辑因子」相同的
    _coerce_by_column / _coerce_by_input + _validate_choice 校验与类型转换，
    因此枚举值非法会被拒绝、数字/日期/布尔会被正确转换；新增因子只需在
    scoring_config.yaml 注册即可被导入识别，无需改此处代码。
    基础字段（客户名称/行业/对接人/联系电话/备注）走简单字符串映射，保持向后兼容。
    """
    config = load_scoring_config()
    factor_by_field: dict[str, Any] = {}
    factor_by_label: dict[str, Any] = {}
    for dim in config.dimensions:
        for f in dim.factors:
            if f.input.type == "readonly":  # 派生/共享因子（如回款逾期扣分）不单独成列
                continue
            # setdefault 保留首次出现：可编辑因子（带 options）优先于同名 readonly 因子
            factor_by_field.setdefault(f.field, f)
            factor_by_label.setdefault(f.label, f)

    # 基础（非因子）字段：中文表头 -> 模型列
    base_map = {
        "客户名称": "customer_name",
        "行业": "industry",
        "对接人": "contact_person",
        "联系电话": "contact_phone",
        "备注": "notes",
    }
    # 兼容旧模板里与配置 label 不一致的历史中文表头
    legacy_factor_headers = {
        "客户满意度": "customer_satisfaction",
        "合同金额(万元)": "contract_amount",
    }

    created = 0
    errors = []
    imported: list[Customer] = []

    for i, row in enumerate(records):
        try:
            data: dict[str, Any] = {}
            custom_fields: dict[str, Any] = {}

            for cn_key, val in row.items():
                if val is None or val == "":
                    continue
                en = base_map.get(cn_key)
                if en:
                    data[en] = str(val).strip()
                    continue

                # 因子识别：先按配置 label，再按遗留中文表头，最后按英文 field
                factor = factor_by_label.get(cn_key) or factor_by_field.get(
                    legacy_factor_headers.get(cn_key, cn_key)
                )
                if factor is None:
                    factor = factor_by_field.get(cn_key)
                if factor is not None and factor.input.editable:
                    if factor.source == "custom_fields":
                        custom_fields[factor.field] = _coerce_by_input(factor, val)
                    else:
                        column = Customer.__table__.columns.get(factor.field)
                        if column is None:
                            custom_fields[factor.field] = _coerce_by_input(factor, val)
                        else:
                            data[factor.field] = _coerce_by_column(factor, column, val)
                    continue

                # 其余列归入自定义字段（原始字符串）
                if isinstance(val, (datetime.datetime, datetime.date)):
                    custom_fields[cn_key] = val.isoformat() if isinstance(val, datetime.datetime) else str(val)
                else:
                    custom_fields[cn_key] = str(val).strip()

            if custom_fields:
                data["custom_fields"] = custom_fields

            if "customer_name" not in data:
                errors.append(f"第{i + 2}行缺少客户名称")
                continue

            customer = Customer(**data)
            db.add(customer)
            imported.append(customer)
            created += 1
        except Exception as e:
            errors.append(f"第{i + 2}行: {str(e)}")

    db.commit()

    # 导入完成后补一条评估基线，导入的客户也能看到趋势起点
    for customer in imported:
        try:
            assessment_history.record_assessment(
                db, customer, assessed_by="批量导入", trigger="import",
                skip_if_unchanged=False, commit=False,
            )
        except Exception as e:  # 历史记录失败不应影响导入结果
            errors.append(f"{customer.customer_name} 评估基线写入失败: {e}")
    db.commit()

    return {"created": created, "errors": errors}
