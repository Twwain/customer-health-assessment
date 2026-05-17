import datetime
import io
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import or_
from database import get_db
from models import Customer
from schemas import CustomerCreate, CustomerUpdate, CustomerResponse, CustomerListResponse
from services.scoring import get_scoring_strategy
import openpyxl

router = APIRouter(prefix="/customers", tags=["客情信息"])


@router.get("", response_model=CustomerListResponse)
def list_customers(
    search: str = Query(default="", description="搜索客户名称/行业/对接人"),
    industry: str = Query(default="", description="按行业筛选"),
    level: str = Query(default="", description="按健康等级筛选（优秀/良好/一般/风险）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
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

    # 按健康等级筛选：需要先获取所有匹配客户，计算评估后过滤
    if level:
        all_items = query.order_by(Customer.updated_at.desc()).all()
        engine = get_scoring_strategy()
        filtered = [c for c in all_items if engine.evaluate(c).level == level]
        total = len(filtered)
        items = filtered[(page - 1) * page_size : page * page_size]
    else:
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
    return CustomerResponse.model_validate(customer)


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(customer_id: int, data: CustomerUpdate, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(customer, key, value)
    db.commit()
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.delete("/{customer_id}")
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    db.delete(customer)
    db.commit()
    return {"ok": True}


@router.post("/import")
async def import_customers(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="请选择文件")

    content = await file.read()

    if file.filename.endswith(".csv"):
        return _import_csv(content, db)
    elif file.filename.endswith((".xlsx", ".xls")):
        return _import_excel(content, db)
    else:
        raise HTTPException(status_code=400, detail="仅支持 CSV 或 Excel 文件")


def _import_csv(content: bytes, db: Session):
    import csv

    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return _process_rows(list(reader), db)


def _import_excel(content: bytes, db: Session):
    wb = openpyxl.load_workbook(io.BytesIO(content))
    ws = wb.active
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
    field_map = {
        "客户名称": "customer_name",
        "行业": "industry",
        "对接人": "contact_person",
        "联系电话": "contact_phone",
        "合作年限": "cooperation_years",
        "沟通频率": "contact_frequency",
        "最近联系日期": "last_contact_date",
        "客户满意度": "customer_satisfaction",
        "合同金额(万元)": "contract_amount",
        "回款情况": "payment_status",
        "风险信号": "risk_signals",
        "竞品介入": "competitor_involvement",
        "增长潜力": "growth_potential",
        "备注": "notes",
    }

    created = 0
    errors = []

    for i, row in enumerate(records):
        try:
            data = {}
            for cn_key, en_key in field_map.items():
                val = row.get(cn_key, None)
                if val is None or val == "":
                    continue
                if en_key == "cooperation_years":
                    data[en_key] = float(val) if val else 0
                elif en_key == "customer_satisfaction":
                    data[en_key] = int(val) if val else 5
                elif en_key == "contract_amount":
                    data[en_key] = float(val) if val else 0
                elif en_key == "competitor_involvement":
                    data[en_key] = str(val).strip() in ("是", "yes", "True", "1", "true")
                elif en_key == "last_contact_date":
                    if isinstance(val, datetime.datetime):
                        data[en_key] = val.date()
                    elif isinstance(val, datetime.date):
                        data[en_key] = val
                    else:
                        try:
                            data[en_key] = datetime.datetime.strptime(str(val).strip(), "%Y-%m-%d").date()
                        except ValueError:
                            pass
                else:
                    data[en_key] = str(val).strip()

            # 不在 field_map 中的列归入 custom_fields
            custom_fields = {}
            for key, val in row.items():
                if key not in field_map and val is not None and val != "":
                    if isinstance(val, (datetime.datetime, datetime.date)):
                        custom_fields[key] = val.isoformat() if isinstance(val, datetime.datetime) else str(val)
                    else:
                        custom_fields[key] = str(val).strip()
            if custom_fields:
                data["custom_fields"] = custom_fields

            if "customer_name" not in data:
                errors.append(f"第{i + 2}行缺少客户名称")
                continue

            customer = Customer(**data)
            db.add(customer)
            created += 1
        except Exception as e:
            errors.append(f"第{i + 2}行: {str(e)}")

    db.commit()
    return {"created": created, "errors": errors}
