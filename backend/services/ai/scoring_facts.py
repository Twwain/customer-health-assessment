"""Shared scoring value validation and deterministic projection summaries."""
import datetime
import json
import math
from services.scoring.config_loader import KEY_PERSON_COUNT
from services.scoring.rules import evaluate_dimension

def _json_value(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value

def _summary(customer, config, today):
    dimensions = []
    for dim in config.enabled_dimensions:
        result = evaluate_dimension(dim, customer, today)
        dimensions.append({
            "key": dim.key, "name": dim.name, "score": round(result.score, 6),
            "max_score": dim.max_score, "gap": round(dim.max_score - result.score, 6),
            "score_percent": round(result.score / dim.max_score * 100, 6) if dim.max_score else None,
        })
    raw_total = sum(item["score"] for item in dimensions)
    return {"total_score": round(raw_total, 1), "raw_total_score": round(raw_total, 6),
            "dimensions": dimensions}

def _legal_target(factor, value):
    spec = factor.input
    if not spec.editable:
        raise ValueError(f"因子 {factor.field} 不允许编辑")
    if spec.type == "key_person_levels":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError(f"{factor.field} 目标必须为完整关键人列表") from exc
        if not isinstance(value, (list, tuple)) or len(value) != KEY_PERSON_COUNT:
            raise ValueError(f"{factor.field} 目标必须为 {KEY_PERSON_COUNT} 位关键人的完整列表")
        allowed = set(spec.options)
        if any(isinstance(item, bool) or str(item) not in allowed for item in value):
            raise ValueError(f"{factor.field} 包含非法关键人等级")
        return json.dumps([int(item) for item in value], separators=(",", ":"))
    if spec.options:
        if isinstance(value, str) and value not in spec.options:
            # 出站不可信数据包装仅将尖括号改为全角；还原同一比较符号，不改变方向或数值。
            unescape = str.maketrans("＜＞", "<>")
            equivalent = [option for option in spec.options if option.translate(unescape) == value.translate(unescape)]
            if len(equivalent) == 1:
                value = equivalent[0]
        if value not in spec.options or isinstance(value, bool):
            raise ValueError(f"{factor.field} 目标不是合法选项")
        return value
    if spec.type in ("number", "slider"):
        if isinstance(value, bool):
            raise ValueError(f"{factor.field} 目标必须为数值")
        try:
            number = float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{factor.field} 目标必须为数值") from exc
        if (not math.isfinite(number) or (spec.min is not None and number < spec.min)
                or (spec.max is not None and number > spec.max)):
            raise ValueError(f"{factor.field} 目标超出范围")
        if spec.step and not math.isclose((number - (spec.min or 0)) / spec.step,
                                         round((number - (spec.min or 0)) / spec.step), abs_tol=1e-8):
            raise ValueError(f"{factor.field} 目标不符合步长")
        return number
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{factor.field} 目标必须为布尔值")
        return value
    if spec.type == "date":
        try:
            return datetime.date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise ValueError(f"{factor.field} 目标必须为 ISO 日期") from exc
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{factor.field} 目标必须为非空文本")
    return value
