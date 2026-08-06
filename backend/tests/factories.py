"""测试共享的因子取值工厂。

``MAX_FACTORS`` 与演示数据共用同一份配置推导（seed_factors.GOOD_FACTORS），
避免多份"最高档"真源漂移；配置档位调整后测试输入自动跟随，保证满分场景成立。
"""

from seed_factors import GOOD_FACTORS as MAX_FACTORS
