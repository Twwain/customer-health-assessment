"""按最新因子模板生成跨分数区间的测试客户并导入数据库。

设计要点
--------
- 评分配置 ``scoring_config.yaml`` 中 59/60 因子为 ``custom_fields + mapping + select``，
  取值即 map 的 key；仅 ``customer_satisfaction`` 为 ``model + threshold``（1-10 滑块）。
- 各维度 ``base_score=0``、满分 100；对每个可编辑因子按其健康度 ``t`` 取
  ``round((1-t)*(档位数-1))`` 档。分数随 t 单调递增，但两端会"饱和"（非严格线性），
  因此本脚本对每个测试客户给定**目标分数**，对 t 做二分搜索，用真实评分引擎
  反解出使总分≈目标分的 t，从而稳定落入 4 个等级区间：
  健康 ≥80 / 亚健康 ≥60 / 风险 ≥40 / 高危 <40。
- 建档后调用 ``assessment_history.record_assessment`` 写入评估基线（与
  ``routers/customers.create_customer`` 行为一致）。

运行（务必在 backend 目录下，使用项目 venv）：
    cd backend
    ../.venv/Scripts/python.exe seed_test_customers.py

仅在数据库中存在同名「测试-」前缀客户时才会被清理重建，不影响其它数据。
"""

from __future__ import annotations

from database import SessionLocal
from models import Customer
from services import assessment_history
from services.scoring import get_scoring_strategy, load_scoring_config

TEST_PREFIX = "测试-"
# 每个测试客户：名称、行业、目标总分。
# 因子取值为离散档位，分数呈阶跃分布（如 76.5 与 96.2 之间无中间值），
# 故目标分选取各等级区间内的可达值，使命名与最终等级严格对应：
#   健康 ≥80 / 亚健康 60-79 / 风险 40-59 / 高危 <40
SPECS = [
    ("测试-政企-健康甲", "政企", 95),
    ("测试-金融-健康乙", "金融", 88),
    ("测试-教育-亚健甲", "教育", 76),
    ("测试-医疗-亚健乙", "医疗", 72),
    ("测试-制造-亚健丙", "制造", 69),
    ("测试-政企-亚健丁", "政企", 62),
    ("测试-能源-风险甲", "能源", 49),
    ("测试-金融-风险乙", "金融", 46),
    ("测试-零售-风险丙", "零售", 43),
    ("测试-医疗-高危甲", "医疗", 35),
    ("测试-制造-高危乙", "制造", 27),
    ("测试-教育-高危丙", "教育", 18),
]


def _clamp(value, low, high):
    return max(low, min(high, value))


def _candidates(factor):
    """返回该可编辑因子的候选取值列表（按分数从高到低排序）。"""
    if factor.rule.type == "mapping":
        table = factor.rule.params.get("map", {})
        items = sorted(table.items(), key=lambda kv: kv[1], reverse=True)
        return [k for k, _ in items]
    return []


def _custom_fields_for_t(config, t):
    """为所有 custom_fields 因子按 t 生成取值。

    注：评分引擎读取 custom_fields 时不区分 editable 标志，因此这里覆盖**全部**
    注册因子（含非 editable），才能把总分推到接近上限；否则非 editable 因子
    固定取默认低分，会把总分上限压到 ~76，导致「健康」区间无法达到。
    """
    custom_fields: dict[str, object] = {}
    for dim in config.enabled_dimensions:
        for factor in dim.enabled_factors:
            if factor.source == "custom_fields":
                cands = _candidates(factor)
                if cands:
                    n = len(cands)
                    idx = _clamp(round((1 - t) * (n - 1)), 0, n - 1)
                    custom_fields[factor.field] = cands[idx]
    return custom_fields


def _model_fields_for_t(t):
    """依据 t 生成与展示相关的模型列（不影响分数，仅增强真实感）。"""
    sat = _clamp(round(t * 8 + 2), 1, 10)
    if t >= 0.7:
        freq, growth = "每周", "高"
    elif t >= 0.4:
        freq = "每月"
        growth = "中"
    else:
        freq, growth = "每季度", "低"
    return {
        "cooperation_years": round(t * 7 + 0.5, 1),
        "contact_frequency": freq,
        "customer_satisfaction": sat,
        "contract_amount": round(t * 500 + 20, 1),
        "growth_potential": growth,
    }


def _build_customer(name, industry, t):
    return Customer(
        customer_name=name,
        industry=industry,
        contact_person=f"{industry}对接人",
        contact_phone="138****0000",
        notes=f"自动生成的跨区间测试客户（目标健康度 t={t:.3f}）。",
        custom_fields=_custom_fields_for_t(CONFIG, t),
        **_model_fields_for_t(t),
    )


def _build_score_grid(strategy):
    """预计算 t∈[0,1] 细网格对应的真实总分（分数曲线非平滑/近阶跃，故用网格法
    反解，而非假设单调平滑的二分搜索）。"""
    grid = []
    for i in range(0, 201):
        t = i / 200.0
        tmp = _build_customer("__tmp__", "政企", t)
        tmp.id = 0  # 占位，仅用于评分反解，不落库
        s = strategy.evaluate(tmp).total_score
        grid.append((t, s))
    return grid


def _solve_t(target_score, grid):
    """在网格中选取总分最接近目标分的 t。"""
    return min(grid, key=lambda ts: abs(ts[1] - target_score))[0]


def _cleanup_all_test(db):
    """删除所有「测试-」前缀客户（含其评估历史），保证数据集干净、可重复导入。"""
    old = db.query(Customer).filter(Customer.customer_name.like("测试-%")).all()
    for c in old:
        db.delete(c)  # 级联删除评估历史
    db.commit()
    if old:
        print(f"  已清理 {len(old)} 个旧测试客户")


CONFIG = load_scoring_config()


def main():
    strategy = get_scoring_strategy()
    grid = _build_score_grid(strategy)
    db = SessionLocal()
    _cleanup_all_test(db)  # 一次性清理旧「测试-」客户，保证可重复导入
    summary: list[tuple[str, float, str]] = []

    try:
        for name, industry, target in SPECS:
            t = _solve_t(target, grid)

            customer = _build_customer(name, industry, t)

            db.add(customer)
            db.commit()
            db.refresh(customer)

            assessment_history.record_assessment(
                db, customer, assessed_by="测试数据导入", trigger="create", skip_if_unchanged=False
            )

            assessment = strategy.evaluate(customer)
            summary.append((name, assessment.total_score, assessment.level))
            print(f"  {name:<14} 目标={target:3d}  实际={assessment.total_score:5.1f}  等级={assessment.level}")
    finally:
        db.close()

    print(f"\n共导入 {len(summary)} 个测试客户，区间分布：")
    buckets: dict[str, int] = {}
    for _, _, level in summary:
        buckets[level] = buckets.get(level, 0) + 1
    for level in ("健康", "亚健康", "风险", "高危"):
        print(f"  {level}: {buckets.get(level, 0)} 个")


if __name__ == "__main__":
    main()
