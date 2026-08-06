import datetime
from database import SessionLocal
from models import Customer

db = SessionLocal()

customers = [
    Customer(
        customer_name='示例银行(总行)',
        industry='金融',
        contact_person='张伟',
        contact_phone='13800138001',
        cooperation_years=6.5,
        contact_frequency='每周',
        last_contact_date=datetime.date.today(),
        customer_satisfaction=9,
        contract_amount=800,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='高',
        notes='核心交换机 + 全行SDN改造项目，长期战略合作伙伴',
        custom_fields={'客户级别': 'S级', '产品线': '交换机/路由器/SDN', '区域': '华北', '销售代表': '陈工'}
    ),
    Customer(
        customer_name='示例通信集团',
        industry='通信',
        contact_person='李明',
        contact_phone='13800138002',
        cooperation_years=4.0,
        contact_frequency='双周',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=5),
        customer_satisfaction=8,
        contract_amount=500,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='高',
        notes='5G承载网设备采购 + 服务器集群，有扩展空间',
        custom_fields={'客户级别': 'A级', '产品线': '交换机/服务器', '区域': '华东', '销售代表': '王工'}
    ),
    Customer(
        customer_name='示例股份银行',
        industry='金融',
        contact_person='王芳',
        contact_phone='13800138003',
        cooperation_years=3.0,
        contact_frequency='每月',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=30),
        customer_satisfaction=7,
        contract_amount=300,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='中',
        notes='防火墙 + 云桌面解决方案，合规要求高',
        custom_fields={'客户级别': 'A级', '产品线': '安全/云桌面', '区域': '华南', '销售代表': '赵工'}
    ),
    Customer(
        customer_name='示例电网公司',
        industry='能源',
        contact_person='刘洋',
        contact_phone='13800138004',
        cooperation_years=2.5,
        contact_frequency='每月',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=45),
        customer_satisfaction=7,
        contract_amount=150,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='中',
        notes='电力调度网交换机替换项目，数字化转型客户',
        custom_fields={'客户级别': 'B级', '产品线': '交换机/路由器', '区域': '华北', '销售代表': '陈工'}
    ),
    Customer(
        customer_name='示例互联网公司',
        industry='互联网',
        contact_person='陈静',
        contact_phone='13800138005',
        cooperation_years=5.0,
        contact_frequency='双周',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=3),
        customer_satisfaction=8,
        contract_amount=650,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='高',
        notes='数据中心交换机 + 智能运维平台，云计算深度合作',
        custom_fields={'客户级别': 'S级', '产品线': '交换机/运维平台', '区域': '华东', '销售代表': '王工'}
    ),
    Customer(
        customer_name='示例地产集团',
        industry='房地产',
        contact_person='赵强',
        contact_phone='13800138006',
        cooperation_years=1.5,
        contact_frequency='每季度',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=100),
        customer_satisfaction=5,
        contract_amount=80,
        payment_status='部分逾期',
        risk_signals='行业下行，IT预算缩减；无线网络项目暂停',
        competitor_involvement=True,
        growth_potential='低',
        notes='受地产行业环境影响，无线网络项目搁置，需密切关注',
        custom_fields={'客户级别': 'C级', '产品线': '无线/WLAN', '区域': '西南', '销售代表': '孙工'}
    ),
    Customer(
        customer_name='示例汽车制造',
        industry='制造业',
        contact_person='孙磊',
        contact_phone='13800138007',
        cooperation_years=2.0,
        contact_frequency='每月',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=15),
        customer_satisfaction=8,
        contract_amount=400,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='高',
        notes='新能源汽车工厂网络建设 + 工业交换机，增长迅猛',
        custom_fields={'客户级别': 'A级', '产品线': '工业交换机/无线', '区域': '华南', '销售代表': '陈工'}
    ),
    Customer(
        customer_name='示例能源集团',
        industry='能源',
        contact_person='周明',
        contact_phone='13800138008',
        cooperation_years=7.0,
        contact_frequency='每季度',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=120),
        customer_satisfaction=4,
        contract_amount=200,
        payment_status='部分逾期',
        risk_signals='对接人变更频繁；内部采购流程冗长；友商竞争介入',
        competitor_involvement=True,
        growth_potential='低',
        notes='路由器/交换机存量替换，但国企决策链长，友商积极渗透',
        custom_fields={'客户级别': 'B级', '产品线': '路由器/交换机', '区域': '华北', '销售代表': '赵工'}
    ),
    Customer(
        customer_name='示例内容平台',
        industry='互联网',
        contact_person='黄凯',
        contact_phone='13800138009',
        cooperation_years=1.0,
        contact_frequency='每周',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=1),
        customer_satisfaction=9,
        contract_amount=350,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='高',
        notes='高性能交换机用于新建数据中心，快速增长的互联网客户',
        custom_fields={'客户级别': 'A级', '产品线': '交换机/服务器', '区域': '华北', '销售代表': '王工'}
    ),
    Customer(
        customer_name='示例中心医院',
        industry='医疗',
        contact_person='郑丽',
        contact_phone='13800138010',
        cooperation_years=3.5,
        contact_frequency='每月',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=60),
        customer_satisfaction=6,
        contract_amount=250,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='中',
        notes='HIS系统网络升级 + 医疗物联网方案',
        custom_fields={'客户级别': 'A级', '产品线': '交换机/物联网', '区域': '华北', '销售代表': '李工'}
    ),
    Customer(
        customer_name='示例汽车集团',
        industry='制造业',
        contact_person='马超',
        contact_phone='13800138011',
        cooperation_years=1.2,
        contact_frequency='不定期',
        last_contact_date=None,
        customer_satisfaction=3,
        contract_amount=50,
        payment_status='严重逾期',
        risk_signals='长期未联系；满意度持续下降；友商已介入报价',
        competitor_involvement=True,
        growth_potential='低',
        notes='车联网试点项目搁置，友商积极渗透，需立即制定挽留方案',
        custom_fields={'客户级别': 'D级', '产品线': '工业交换机/车联网', '区域': '华东', '销售代表': '孙工'}
    ),
    Customer(
        customer_name='示例大学',
        industry='教育',
        contact_person='吴鹏',
        contact_phone='13800138012',
        cooperation_years=4.5,
        contact_frequency='双周',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=7),
        customer_satisfaction=9,
        contract_amount=180,
        payment_status='正常',
        risk_signals='',
        competitor_involvement=False,
        growth_potential='高',
        notes='校园网全系列设备 + 科研超算网络，标杆教育客户',
        custom_fields={'客户级别': 'A级', '产品线': '交换机/无线/服务器', '区域': '华北', '销售代表': '陈工'}
    ),
    Customer(
        customer_name='示例保险集团',
        industry='金融',
        contact_person='何涛',
        contact_phone='13800138013',
        cooperation_years=4.0,
        contact_frequency='每季度',
        last_contact_date=datetime.date.today() - datetime.timedelta(days=200),
        customer_satisfaction=2,
        contract_amount=60,
        payment_status='严重逾期',
        risk_signals='连续两个季度未回款；IT部门架构调整裁撤；友商已签约替代',
        competitor_involvement=True,
        growth_potential='低',
        notes='安全产品线被友商全面替代，极高风险，建议评估退出策略',
        custom_fields={'客户级别': 'D级', '产品线': '安全/防火墙', '区域': '华南', '销售代表': '赵工'}
    ),
]

# ── 客情因子（7 维度 × 60 因子，配置驱动）────────────────────────────────────
# 三档画像 GOOD（健康）/ MEDIUM（亚健康）/ RISKY（高危）由 seed_factors.py
# 从 scoring_config.yaml 的打分规则自动推导；修改配置档位无需维护本文件。
# 注意：满意度因子（HIS-09）复用模型列 customer_satisfaction，不在此处填报。
from seed_factors import GOOD_FACTORS, MEDIUM_FACTORS, RISKY_FACTORS

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


def main() -> None:
    # 幂等：已存在的客户按名称跳过，重复执行不会产生重复数据
    existing = {name for (name,) in db.query(Customer.customer_name).all()}
    to_insert = []
    skipped = 0
    for c in customers:
        if c.customer_name in existing:
            skipped += 1
            continue
        profile = PROFILE_BY_CUSTOMER.get(c.customer_name)
        if profile:
            c.custom_fields = {**(c.custom_fields or {}), **profile}
        to_insert.append(c)

    if to_insert:
        db.add_all(to_insert)
        db.commit()

    print(f'新增 {len(to_insert)} 条 客情模拟数据（跳过已存在 {skipped} 条）：')
    print()
    for c in to_insert:
        cf = c.custom_fields or {}
        print(f'  [{c.id}] {c.customer_name} ({c.industry})')
        print(f'       产品: {cf.get("产品线", "-")} | {c.contract_amount}万 | {c.cooperation_years}年')
        print(f'       满意度: {c.customer_satisfaction}/10 | 回款: {c.payment_status} | 级别: {cf.get("客户级别", "-")}')
        print(f'       备注: {c.notes}')
        print()

    db.close()


if __name__ == "__main__":
    main()
