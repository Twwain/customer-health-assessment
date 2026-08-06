"""客情因子演示数据（7 维度 × 60 因子，由评分配置动态生成）。

三档画像 GOOD（健康）/ MEDIUM（亚健康）/ RISKY（高危）的因子取值从
``backend/scoring_config.yaml`` 的打分规则自动推导：

- mapping 规则：取 map 中分数最高 / 中间 / 最低档对应的枚举值；
- threshold 规则：取分数最高档阈值（GOOD）、中间档阈值（MEDIUM）、
  触发默认档的值（RISKY，即低于最低 gte 档或高于最低 lte 档）；
- 其余规则类型取 input.options 的首项 / 中间项 / 末项。

配置档位调整后演示数据自动跟随，无需人工维护；tests/factories.py 的
``MAX_FACTORS`` 直接复用本模块的 ``GOOD_FACTORS``，保证测试满分口径一致。
注意：满意度因子（HIS-09）复用模型列 customer_satisfaction，不在此处填报。
"""

from services.scoring import load_scoring_config


def _mapping_value(map_items, rank):
    items = sorted(map_items, key=lambda kv: float(kv[1]))
    if rank == "best":
        return items[-1][0]
    if rank == "worst":
        return items[0][0]
    return items[len(items) // 2][0]


def _bracket_key(bracket):
    for key in ("gte", "gt", "lte", "lt", "eq"):
        if key in bracket:
            return key, bracket[key]
    return None, None


def _threshold_value(brackets, rank):
    bs = sorted(brackets, key=lambda b: float(b.get("score", 0)))
    if not bs:
        return ""
    if rank == "worst":
        # 取低于最低 gte/gt 档或高于最低 lte/lt 档的值，触发默认档
        key, th = _bracket_key(bs[0])
        if th is None:
            return 0
        if key in ("lte", "lt"):
            return float(th) + 1
        return max(0.0, float(th) - 1)
    target = bs[-1] if rank == "best" else bs[len(bs) // 2]
    key, th = _bracket_key(target)
    if th is None:
        return 0
    # 严格比较符（lt/gt）需要越过阈值本身
    if key == "lt":
        return max(0.0, float(th) - 1)
    if key == "gt":
        return float(th) + 1
    return th


def _rank_value(factor, rank):
    rule = factor.rule
    if rule.type == "mapping":
        return _mapping_value(list((rule.params.get("map") or {}).items()), rank)
    if rule.type == "threshold":
        return _threshold_value(rule.params.get("brackets", []), rank)
    opts = factor.input.options or []
    if not opts:
        return ""
    if rank == "best":
        return opts[0]
    if rank == "worst":
        return opts[-1]
    return opts[len(opts) // 2]


def build_profiles():
    config = load_scoring_config()
    good: dict[str, object] = {}
    medium: dict[str, object] = {}
    risky: dict[str, object] = {}
    for dim in config.dimensions:
        for factor in dim.factors:
            if factor.source != "custom_fields" or not factor.input.editable:
                continue
            good[factor.field] = _rank_value(factor, "best")
            medium[factor.field] = _rank_value(factor, "mid")
            risky[factor.field] = _rank_value(factor, "worst")
    return good, medium, risky


GOOD_FACTORS, MEDIUM_FACTORS, RISKY_FACTORS = build_profiles()

PROFILE_BY_CUSTOMER = {
    "示例银行(总行)": GOOD_FACTORS,
    "示例互联网公司": GOOD_FACTORS,
    "示例内容平台": GOOD_FACTORS,
    "示例大学": GOOD_FACTORS,
    "示例汽车制造": GOOD_FACTORS,
    "示例通信集团": MEDIUM_FACTORS,
    "示例股份银行": MEDIUM_FACTORS,
    "示例电网公司": MEDIUM_FACTORS,
    "示例中心医院": MEDIUM_FACTORS,
    "示例地产集团": RISKY_FACTORS,
    "示例能源集团": RISKY_FACTORS,
    "示例汽车集团": RISKY_FACTORS,
    "示例保险集团": RISKY_FACTORS,
}
