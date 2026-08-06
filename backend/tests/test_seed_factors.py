"""seed_factors 演示档位与 scoring_config.yaml 的一致性校验。

修改评分配置（新增/删除因子、调整档位取值）后，seed_data.py 的演示画像
必须同步，本测试自动暴露不一致，避免人工双处核对遗漏。
"""

import pytest

from models import Customer
from seed_factors import GOOD_FACTORS, MEDIUM_FACTORS, RISKY_FACTORS
from factories import MAX_FACTORS
from services.scoring import get_scoring_strategy, load_scoring_config


def _editable_custom_field_map(config):
    out = {}
    for dim in config.dimensions:
        for fac in dim.factors:
            if fac.source == "custom_fields" and fac.input.editable:
                out[fac.field] = fac
    return out


def test_seed_profiles_cover_all_editable_custom_fields():
    config = load_scoring_config()
    fields = set(_editable_custom_field_map(config))
    for name, profile in (("GOOD", GOOD_FACTORS), ("MEDIUM", MEDIUM_FACTORS), ("RISKY", RISKY_FACTORS)):
        assert set(profile) == fields, f"{name} 档位应恰好覆盖全部可编辑 custom_fields 因子"


def test_seed_values_are_valid_options():
    config = load_scoring_config()
    by_field = _editable_custom_field_map(config)
    for name, profile in (("GOOD", GOOD_FACTORS), ("MEDIUM", MEDIUM_FACTORS), ("RISKY", RISKY_FACTORS)):
        for field, value in profile.items():
            factor = by_field[field]
            if factor.input.options:
                assert value in factor.input.options, (
                    f"{name} 档位 {field}={value!r} 不在配置 options 中"
                )


def test_seed_profiles_land_in_expected_levels():
    """三档画像应落在最高 / 中间 / 最低等级区间（口径与配置一致）。"""
    config = load_scoring_config()
    levels_desc = sorted(config.levels, key=lambda lv: lv.min_score, reverse=True)
    good = get_scoring_strategy().evaluate(
        Customer(id=1, customer_name="g", custom_fields=dict(GOOD_FACTORS), customer_satisfaction=10)
    )
    medium = get_scoring_strategy().evaluate(
        Customer(id=2, customer_name="m", custom_fields=dict(MEDIUM_FACTORS), customer_satisfaction=8)
    )
    risky = get_scoring_strategy().evaluate(
        Customer(id=3, customer_name="r", custom_fields=dict(RISKY_FACTORS), customer_satisfaction=3)
    )
    assert good.total_score == pytest.approx(100.0)
    assert good.level == levels_desc[0].name
    assert medium.total_score < good.total_score
    assert medium.level == levels_desc[1].name
    assert risky.total_score < medium.total_score
    assert risky.level == levels_desc[-1].name


def test_factories_max_factors_aligned_with_seed_factors():
    """测试满分口径与演示 GOOD 档共用同一份配置推导，避免多真源漂移。"""
    assert MAX_FACTORS == GOOD_FACTORS


def test_seed_data_profiles_land_in_expected_levels():
    """seed_data.py 实际入库的三档画像应落在 健康 / 亚健康 / 高危 区间。"""
    import seed_data

    levels_desc = sorted(load_scoring_config().levels, key=lambda lv: lv.min_score, reverse=True)
    good = get_scoring_strategy().evaluate(
        Customer(id=1, customer_name="g", custom_fields=dict(seed_data.GOOD_FACTORS), customer_satisfaction=10)
    )
    medium = get_scoring_strategy().evaluate(
        Customer(id=2, customer_name="m", custom_fields=dict(seed_data.MEDIUM_FACTORS), customer_satisfaction=8)
    )
    risky = get_scoring_strategy().evaluate(
        Customer(id=3, customer_name="r", custom_fields=dict(seed_data.RISKY_FACTORS), customer_satisfaction=3)
    )
    assert good.total_score == pytest.approx(100.0)
    assert good.level == levels_desc[0].name
    assert medium.level == levels_desc[1].name
    assert risky.level == levels_desc[-1].name
