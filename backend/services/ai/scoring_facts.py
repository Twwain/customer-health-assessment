"""由当前评分配置和求值器提供的事实；投影只操作脱离 ORM 的取值副本。"""
from __future__ import annotations

import datetime
import json
import math
import re
from copy import deepcopy
from types import SimpleNamespace

from services.scoring.config_loader import KEY_PERSON_COUNT, load_scoring_config
from services.scoring.rules import evaluate_dimension, evaluate_factor, resolve_value


def _json_value(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _factors(config):
    return [(dim, factor) for dim in config.enabled_dimensions for factor in dim.enabled_factors]


def _snapshot(customer, config):
    snapshot = SimpleNamespace(custom_fields={})
    for _, factor in _factors(config):
        value = deepcopy(resolve_value(customer, factor))
        if factor.source == "custom_fields":
            snapshot.custom_fields[factor.field] = value
        else:
            setattr(snapshot, factor.field, value)
    return snapshot


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


def project_scores(customer, changes: dict, scoring_config=None):
    """验证完整目标后重算全部启用维度；拒绝未知/禁用因子和非法选项。"""
    config = scoring_config or load_scoring_config()
    if not isinstance(changes, dict):
        raise ValueError("changes 必须是因子目标字典")
    available = {factor.field: factor for _, factor in _factors(config)}
    for field in changes:
        if field not in available:
            raise ValueError(f"未知或禁用因子：{field}")
    normalized = {field: _legal_target(available[field], value) for field, value in changes.items()}
    snapshot = _snapshot(customer, config)
    today = datetime.date.today()
    before = _summary(snapshot, config, today)
    for field, value in normalized.items():
        if available[field].source == "custom_fields":
            snapshot.custom_fields[field] = deepcopy(value)
        else:
            setattr(snapshot, field, deepcopy(value))
    after = _summary(snapshot, config, today)
    return {
        "changes": _json_value(normalized), "before": before, "after": after,
        "delta": {
            "total_score": round(after["raw_total_score"] - before["raw_total_score"], 6),
            "dimensions": {a["key"]: round(a["score"] - b["score"], 6)
                           for a, b in zip(after["dimensions"], before["dimensions"])},
        },
    }


def build_scoring_facts(customer, scoring_config=None):
    """提供配置档位、缺口排名和引擎实算的单因子目标收益。"""
    config = scoring_config or load_scoring_config()
    snapshot = _snapshot(customer, config)
    today = datetime.date.today()
    summary = _summary(snapshot, config, today)
    factors = {}
    gains = []
    for dim, factor in _factors(config):
        current = resolve_value(snapshot, factor)
        item = {
            "label": factor.label, "dimension_key": dim.key, "source": factor.source,
            "input_type": factor.input.type, "current_value": _json_value(current),
            "current_score": evaluate_factor(factor, current, today).score,
            "options": [], "min_score": None, "max_score": None,
            "lowest_options": [], "highest_options": [],
        }
        try:
            _legal_target(factor, current)
            item["current_value_valid"] = True
        except ValueError:
            item["current_value_valid"] = False
            item["current_value_note"] = "当前原值未通过目标输入校验；引擎默认计分不代表已填合法最低档。"
        if factor.input.type == "key_person_levels":
            item.update({"option_semantics": "每个选项为单人等级，不是完整分布或因子映射分；完整列表取平均后按规则评分",
                         "person_levels": list(factor.input.options), "expected_people_count": KEY_PERSON_COUNT})
        elif factor.input.editable:
            for option in factor.input.options:
                projection = project_scores(snapshot, {factor.field: option}, config)
                score = evaluate_factor(factor, option, today).score
                delta = projection["delta"]
                item["options"].append({"value": option, "score": score,
                                        "dimension_delta": delta["dimensions"][dim.key],
                                        "total_delta": delta["total_score"]})
            if item["options"]:
                scores = [option["score"] for option in item["options"]]
                item["min_score"], item["max_score"] = min(scores), max(scores)
                item["lowest_options"] = [o["value"] for o in item["options"] if o["score"] == min(scores)]
                item["highest_options"] = [o["value"] for o in item["options"] if o["score"] == max(scores)]
                best = max(item["options"], key=lambda option: option["total_delta"])
                gains.append({"field": factor.field, "target_value": best["value"],
                              "total_delta": best["total_delta"], "dimension_delta": best["dimension_delta"]})
        factors[factor.field] = item
    dimensions = summary["dimensions"]
    return {
        "config_version": config.version, "total_score": summary["total_score"],
        "max_score": config.total_max_score, "dimensions": dimensions,
        "gap_ranking": [d["key"] for d in sorted(dimensions, key=lambda d: d["gap"], reverse=True)],
        "score_percent_ranking": [d["key"] for d in sorted(dimensions, key=lambda d: d["score_percent"]
                                                            if d["score_percent"] is not None else math.inf)],
        "factors": factors,
        "single_factor_gain_ranking": sorted(gains, key=lambda gain: gain["total_delta"], reverse=True),
        "ranking_scope": "收益排名仅比较具有完整离散选项的启用因子；关键人单人等级不视为完整分布目标。业务优先级不等同于分数收益。",
    }


def _field_pattern(field):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(field).replace("_", r"[\s_\-－–]*")
                      + r"(?![A-Za-z0-9_])", re.I)


def _number(value):
    return f"{value:.6f}".rstrip("0").rstrip(".") if value is not None else "未知"


def _dimension_line(dimension):
    return (f"{dimension['key']}（{dimension['name']}）：{_number(dimension['score'])}/{_number(dimension['max_score'])}；"
            f"绝对缺口{_number(dimension['gap'])}；得分率{_number(dimension['score_percent'])}%")


def _factor_line(field, item):
    line = f"{field}（{item['label']}）：当前映射分{_number(item['current_score'])}"
    if not item["current_value_valid"]:
        line += "（原值不合法或未填，默认分不代表已填最低档）"
    if item["input_type"] == "key_person_levels":
        return line + "；单人等级选项=" + "/".join(item["person_levels"]) + "，不是完整分布目标或因子映射分"
    if item["min_score"] is None:
        return line + "；未枚举合法档位边界，不推断最低/最高档及最大收益"
    line += f"；最低映射分{_number(item['min_score'])}，最高映射分{_number(item['max_score'])}；合法目标[映射分/维度增分/总增分]："
    return line + "；".join(
        f"{option['value']}[{_number(option['score'])}/{_number(option['dimension_delta'])}/{_number(option['total_delta'])}]"
        for option in item["options"]
    )


def _gain_line(facts):
    return "可枚举完整选项的单因子最大总收益排序：" + "；".join(
        f"{gain['field']}→{gain['target_value']}：{_number(gain['total_delta'])}"
        for gain in facts["single_factor_gain_ranking"]
    )


def ranking_summary(facts):
    """直接陈述引擎排名（含并列），避免生成模型再次自行判断最高/最低。"""
    dimensions = facts.get("dimensions", [])
    statements = []
    if dimensions:
        largest = max(d["gap"] for d in dimensions)
        leaders = [d["name"] for d in dimensions if math.isclose(d["gap"], largest, abs_tol=1e-6)]
        statements.append(f"绝对缺口最大为{'、'.join(leaders)}（{_number(largest)}分）")
    rated = [d for d in dimensions if d["score_percent"] is not None]
    if rated:
        lowest = min(d["score_percent"] for d in rated)
        leaders = [d["name"] for d in rated if math.isclose(d["score_percent"], lowest, abs_tol=1e-6)]
        statements.append(f"得分率最低为{'、'.join(leaders)}（{_number(lowest)}%）")
    return "；".join(statements)


def format_scoring_facts(facts):
    """同一份精简原文供生成、模型核对和本地问题锚点使用，不重复多层 JSON。"""
    return "\n".join([
        f"配置版本{facts['config_version']}；总分{_number(facts['total_score'])}/{_number(facts['max_score'])}。",
        *(_dimension_line(dimension) for dimension in facts["dimensions"]),
        f"引擎确认的排名：{ranking_summary(facts)}。该排名不等于业务行动优先级。",
        *(_factor_line(field, item) for field, item in facts["factors"].items()),
        _gain_line(facts), facts["ranking_scope"],
    ])


def _dimension_mentions(text, facts):
    """名称来自配置；先排除因子 ID，再合并重叠的维度别名。"""
    factor_spans = [match.span() for field in facts["factors"]
                    for match in _field_pattern(field).finditer(text)]
    mentions = []
    for dimension in facts["dimensions"]:
        name = dimension["name"].strip()
        chinese_name = re.sub(r"^[A-Za-z][A-Za-z0-9_]*\s+", "", name)
        aliases = {dimension["key"], name, chinese_name}
        for alias in aliases:
            pattern = re.escape(alias)
            if alias == dimension["key"]:
                pattern = r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_\-－–])"
            for match in re.finditer(pattern, text, re.I):
                if not any(start < match.end() and match.start() < end for start, end in factor_spans):
                    mentions.append((match.start(), match.end(), dimension))
    result = []
    for start, end, dimension in sorted(mentions, key=lambda item: (item[0], -item[1])):
        if not result or start >= result[-1][1]:
            result.append((start, end, dimension))
    return result


def _dimension_count(text):
    if text.isdecimal():
        return int(text)
    digits = {char: value for value, char in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if text in digits:
        return digits[text]
    if text.count("十") == 1:
        tens, units = text.split("十")
        if (not tens or tens in digits) and (not units or units in digits):
            return digits.get(tens, 1) * 10 + digits.get(units, 0)
    return None


def _full_dimension_comparison(claim, dimensions, mentions):
    """省略范围的得分率/缺口排名沿用全体；显式子集不得拿全体排名否定。"""
    counts = re.findall(r"([零一二三四五六七八九十百两\d]+)\s*个?\s*(?:启用的?|已启用的?)?维度", claim)
    if any(_dimension_count(count) != len(dimensions) for count in counts):
        return False
    if re.search(r"部分|所选|选定|选取的|子集|两者|二者|三者|这[一二两三四五六七八九十\d]+个", claim):
        return False
    explicit_all = bool(counts or re.search(r"(?:各个?|全部|所有(?:已?启用)?|全体|已启用)(?:的)?维度", claim))
    named_subset = (len({dimension["key"] for _, _, dimension in mentions}) > 1
                    and re.search(r"[与和、].{0,30}(?:中|之间)", claim))
    return explicit_all or not named_subset


def _extreme_band_claim(fragment, extreme):
    """识别等于边界档的断言，保留高于最低档/低于最高档等相对描述。"""
    comparison = r"高于|高出|超过|大于" if extreme == "最低" else r"低于|低出|小于"
    direction = "上" if extreme == "最低" else "下"
    pattern = r"(?:为|是|处于|属于)(?P<prefix>[^，,。；;\n]{0,8})" + extreme + r"档(?:位)?"
    for match in re.finditer(pattern, fragment):
        if re.search(r"(?<!不)(?:" + comparison + r")\s*$", match["prefix"]):
            continue
        if re.match(r"\s*(?:(?:之|以)" + direction + "|" + direction + r"(?:方|面|[一二两三四五六七八九十\d]+档))",
                    fragment[match.end():]):
            continue
        return True
    return False


def _lowest_rate_subjects(claim, mentions):
    """将最低谓词绑定到其维度主体，不借用前一维度的得分率。"""
    rate = r"得分率|得分百分比|得分占比"
    for lowest in re.finditer(r"最低(?!档|之一|之列)", claim):
        rates = list(re.finditer(rate, claim[:lowest.start()]))
        if not rates:
            continue
        comparison = claim[rates[-1].end():lowest.start()]
        if len(comparison) > 80 or re.search(r"优先级|映射|档位|缺口|失分|收益", comparison):
            continue
        following = next((mention for mention in mentions if mention[0] >= lowest.end()), None)
        if following and re.fullmatch(r"\s*(?:(?:的?维度)?(?:为|是|属于|在|位于)|的是|的)\s*",
                                      claim[lowest.end():following[0]]):
            if re.search(rate, claim[:lowest.start()]):
                yield following[2]
            continue
        preceding = [mention for mention in mentions if mention[1] <= lowest.start()]
        if not preceding:
            continue
        start, _, dimension = preceding[-1]
        fragment = claim[start:lowest.start()]
        if re.search(r"(?:高于|超过|大于|低于|不是|并非)\s*$", fragment):
            continue
        if re.search(rate, fragment):
            yield dimension


def _claim_fragments(draft):
    # 机器JSON的一行可能含所有卡片；字段之间不继承否定语、维度主体或比较谓词。
    # 保留带引号的原始字符串，确保硬校验引用仍能精确定位到原文。
    from .local_quality import parse_answer
    try:
        units = parse_answer(draft).units
    except (ValueError, TypeError):
        units = None
    texts = []
    if units is None:
        texts.append(draft)
    else:
        for unit in units:
            if unit.item is None:
                texts.append(unit.text)
            else:
                texts.extend(match.group(0) for match in re.finditer(r'"(?:\\.|[^"\\])*"', unit.text))
    return [match.group(0).strip() for text in texts for match in re.finditer(r"[^\n。！？；]+", text)]


def check_scoring_claims(draft, customer, facts=None):
    """保守核对明确的档位、最大缺口和单因子最高收益断言。

    不抽取多项目标，也不把建议优先级当作收益排名。证据逐字存在于评分事实原文。
    """
    facts = facts if facts is not None else build_scoring_facts(customer)
    issues = []
    gains = {gain["field"]: gain for gain in facts["single_factor_gain_ranking"]}
    max_gain = max((gain["total_delta"] for gain in gains.values()), default=0)

    def add(claim, evidence, reason):
        issue = {"claim": claim, "source_id": "scoring",
                 "evidence": evidence, "reason": reason}
        if issue not in issues and len(issues) < 8:
            issues.append(issue)

    # 连续原文作 claim，避免改写后无法定位；不跨中文句界和换行。
    for claim in _claim_fragments(draft):
        if not claim or re.search(r"不是|并非|不应|不能|误称|错误|假设|如果|若|是否|引用|原文|曾称", claim):
            continue
        if re.match(r"^[“‘].*[”’]", claim):
            continue
        field_matches = [(m.start(), field, item) for field, item in facts["factors"].items()
                         for m in _field_pattern(field).finditer(claim)]
        field_matches.sort(key=lambda entry: entry[0])
        for index, (start, field, item) in enumerate(field_matches):
            end = field_matches[index + 1][0] if index + 1 < len(field_matches) else len(claim)
            fragment = claim[start:end]
            if re.search(r"(?:单点|单因子|单项)(?:分数|得分|总分|加权)?(?:提升|收益|增益)(?:为|是)?最大", fragment):
                gain = gains.get(field)
                if gain and gain["total_delta"] < max_gain - 1e-6:
                    add(claim, _gain_line(facts), "该因子的最大单项加权收益小于其他因子，不能称为单点提升最大项；业务优先级可另行说明。")
            minimum, maximum = item["min_score"], item["max_score"]
            if minimum is not None:
                # 明确未来目标不等同于当前已处该档；数值越界仍需核对。
                future = bool(re.search(r"目标|提升至|升至|推进到|拟|建议|争取", fragment))
                low_claim = _extreme_band_claim(fragment, "最低")
                high_claim = _extreme_band_claim(fragment, "最高")
                if not future and (low_claim or high_claim) and not item["current_value_valid"]:
                    add(claim, _factor_line(field, item), "原值未通过合法输入校验，不能把默认计分当作已填最高或最低档。")
                elif not future and low_claim and item["current_score"] > minimum:
                    add(claim, _factor_line(field, item), "当前映射分高于配置最低分，不能描述为最低档。")
                elif not future and high_claim and item["current_score"] < maximum:
                    add(claim, _factor_line(field, item), "当前映射分低于配置最高分，不能描述为最高档。")
                score_tokens = re.findall(r"(?:映射分|因子得分|该项得分)\s*(?:为|是|至|[:：])?\s*(-?\d+(?:\.\d+)?)", fragment)
                for clause in re.split(r"[，,（）()]", fragment):
                    if re.search(r"总分|维度|金额|收益|贡献|增益|增加", clause):
                        continue
                    score_tokens += re.findall(r"(-?\d+(?:\.\d+)?)\s*/\s*10\b", clause)
                    score_tokens += re.findall(r"(?:当前|现为|得分|评分|提升至|升至)\s*(?:为|是)?\s*(-?\d+(?:\.\d+)?)\s*分", clause)
                if any(float(value) < minimum or float(value) > maximum for value in score_tokens):
                    add(claim, _factor_line(field, item), "所述因子映射分超出配置合法选项的分数范围。")
            elif item["input_type"] == "key_person_levels":
                bounds = [int(value) for value in item["person_levels"]]
                targets = re.findall(r"(?:等级(?:提升|升)?至|等级为|单人.{0,6}→)\s*(-?\d+)", fragment)
                if any(not min(bounds) <= int(target) <= max(bounds) for target in targets):
                    add(claim, _factor_line(field, item), "单人等级超出配置量表；单人等级不等同于因子映射分。")
        dim_matches = _dimension_mentions(claim, facts)
        if not _full_dimension_comparison(claim, facts["dimensions"], dim_matches):
            continue
        for index, (start, _, dim) in enumerate(dim_matches):
            end = dim_matches[index + 1][0] if index + 1 < len(dim_matches) else len(claim)
            fragment = claim[start:end]
            if re.search(r"(?:绝对缺口|缺口|失分)(?:(?!优先级|映射|档位|得分率|收益|增分|增益|可增|单因子|单项).){0,60}(?:最大|最多)", fragment):
                if dim["gap"] < max(d["gap"] for d in facts["dimensions"]) - 1e-6:
                    add(claim, "\n".join(_dimension_line(d) for d in facts["dimensions"]), "按维度满分减当前得分计算的绝对缺口，该维度并非最大。")
        for dimension in _lowest_rate_subjects(claim, dim_matches):
            rates = [d["score_percent"] for d in facts["dimensions"] if d["score_percent"] is not None]
            if dimension["score_percent"] is not None and dimension["score_percent"] > min(rates) + 1e-6:
                add(claim, "\n".join(_dimension_line(d) for d in facts["dimensions"]), "该维度得分率并非所有启用维度最低。")
    return issues
