import datetime
from models import Customer
from schemas import AssessmentResponse, DimensionScore
from .base import ScoringStrategy


class RuleBasedStrategy(ScoringStrategy):
    def evaluate(self, c: Customer) -> AssessmentResponse:
        dims = [
            self._relationship_score(c),
            self._satisfaction_score(c),
            self._business_score(c),
            self._risk_score(c),
        ]
        total = sum(d.score for d in dims)
        level, color = self._level(total)

        risk_alerts = []
        suggestions = []

        if not c.last_contact_date or (datetime.date.today() - c.last_contact_date).days > 90:
            risk_alerts.append("超过90天未联系客户，关系存在疏远风险")
            suggestions.append("建议尽快安排客户拜访或沟通")
        if c.customer_satisfaction <= 4:
            risk_alerts.append(f"客户满意度较低({c.customer_satisfaction}/10)，存在流失风险")
            suggestions.append("建议深入了解客户不满原因并制定改善计划")
        if c.competitor_involvement:
            risk_alerts.append("竞品已介入，客户存在被挖角风险")
            suggestions.append("建议加强客情维护，突出自身差异化优势")
        if c.payment_status in ("部分逾期", "严重逾期"):
            risk_alerts.append(f"回款状态异常：{c.payment_status}")
            suggestions.append("建议跟进回款进度，评估是否需要调整合作模式")
        if c.risk_signals:
            risk_alerts.append(f"存在风险信号：{c.risk_signals}")
            suggestions.append("建议针对风险信号制定应对方案")
        if c.growth_potential == "高" and c.customer_satisfaction >= 7:
            suggestions.append("该客户增长潜力高且满意度良好，建议加大资源投入推动增长")

        return AssessmentResponse(
            customer_id=c.id,
            customer_name=c.customer_name,
            total_score=round(total, 1),
            level=level,
            level_color=color,
            dimensions=dims,
            risk_alerts=risk_alerts,
            suggestions=suggestions,
            assessed_at=datetime.datetime.now(),
        )

    def _relationship_score(self, c: Customer) -> DimensionScore:
        score = 0.0
        details = []

        # 合作年限: 满分10分
        if c.cooperation_years >= 5:
            score += 10
            details.append(f"合作{c.cooperation_years}年(≥5年)：+10分")
        elif c.cooperation_years >= 3:
            score += 7
            details.append(f"合作{c.cooperation_years}年(3-5年)：+7分")
        elif c.cooperation_years >= 1:
            score += 4
            details.append(f"合作{c.cooperation_years}年(1-3年)：+4分")
        else:
            details.append(f"合作不足1年：+0分")

        # 沟通频率: 满分10分
        freq_map = {"每周": 10, "双周": 7, "每月": 5, "每季度": 3, "不定期": 1}
        freq_score = freq_map.get(c.contact_frequency, 3)
        score += freq_score
        details.append(f"沟通频率「{c.contact_frequency}」：+{freq_score}分")

        # 最近联系: 满分5分
        if c.last_contact_date:
            days = (datetime.date.today() - c.last_contact_date).days
            if days <= 7:
                score += 5
                details.append(f"最近联系{days}天前(≤7天)：+5分")
            elif days <= 30:
                score += 3
                details.append(f"最近联系{days}天前(8-30天)：+3分")
            elif days <= 90:
                score += 1
                details.append(f"最近联系{days}天前(31-90天)：+1分")
            else:
                details.append(f"最近联系{days}天前(>90天)：+0分")
        else:
            details.append("无联系记录：+0分")

        return DimensionScore(name="关系紧密度", score=score, max_score=25, details=details)

    def _satisfaction_score(self, c: Customer) -> DimensionScore:
        score = c.customer_satisfaction * 2.5
        details = [f"客户满意度评分 {c.customer_satisfaction}/10 × 2.5 = {score}分"]
        return DimensionScore(name="客户满意度", score=score, max_score=25, details=details)

    def _business_score(self, c: Customer) -> DimensionScore:
        score = 0.0
        details = []

        # 合同金额: 满分15分
        if c.contract_amount >= 500:
            score += 15
            details.append(f"合同金额{c.contract_amount}万元(≥500万)：+15分")
        elif c.contract_amount >= 100:
            score += 10
            details.append(f"合同金额{c.contract_amount}万元(100-500万)：+10分")
        elif c.contract_amount >= 50:
            score += 6
            details.append(f"合同金额{c.contract_amount}万元(50-100万)：+6分")
        elif c.contract_amount > 0:
            score += 3
            details.append(f"合同金额{c.contract_amount}万元(<50万)：+3分")
        else:
            details.append("无合同金额：+0分")

        # 回款情况: 满分10分
        payment_map = {"正常": 10, "部分逾期": 4, "严重逾期": 0}
        payment_score = payment_map.get(c.payment_status, 5)
        score += payment_score
        details.append(f"回款情况「{c.payment_status}」：+{payment_score}分")

        return DimensionScore(name="商业价值", score=score, max_score=25, details=details)

    def _risk_score(self, c: Customer) -> DimensionScore:
        score = 25.0
        details = ["基础分25分，按风险项扣减"]

        if c.risk_signals:
            score -= 8
            details.append("存在风险信号：-8分")
        if c.competitor_involvement:
            score -= 10
            details.append("竞品已介入：-10分")
        if c.payment_status == "严重逾期":
            score -= 7
            details.append("严重逾期：-7分")
        elif c.payment_status == "部分逾期":
            score -= 4
            details.append("部分逾期：-4分")

        # 增长潜力加分
        growth_map = {"高": 5, "中": 2, "低": 0}
        growth_bonus = growth_map.get(c.growth_potential, 0)
        score += growth_bonus
        details.append(f"增长潜力「{c.growth_potential}」：+{growth_bonus}分")

        score = max(2.5, min(25, score))
        return DimensionScore(name="风险等级", score=score, max_score=25, details=details)

    def _level(self, total: float) -> tuple[str, str]:
        if total >= 85:
            return "优秀", "#22c55e"
        elif total >= 70:
            return "良好", "#3b82f6"
        elif total >= 55:
            return "一般", "#eab308"
        else:
            return "风险", "#ef4444"
