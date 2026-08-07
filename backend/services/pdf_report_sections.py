"""PDF 报告章节渲染 mixin：封面 / 综合评分 / 维度明细 / 风险建议 / 页脚。

从 ``pdf_report.PdfReportGenerator`` 中按职责拆出，各 mixin 通过 MRO 访问
核心类提供的 ``self.styles`` / ``self.FONT_NAME`` / 设计 Token 与辅助方法。
"""

from __future__ import annotations

import re

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, KeepTogether
from reportlab.platypus.flowables import HRFlowable

from schemas import AssessmentResponse, AssessmentTrendResponse


class _CoverMixin:
    """封面：品牌条 + 客户元信息 + 综合评分 + 结论速览。

    对外展示口径：不出现 AI/规则引擎等内部实现措辞。
    """

    def _cover(
        self,
        a: AssessmentResponse,
        *,
        industry: str = "",
    ) -> list:
        color = self._color_for(a.level, self.BRAND)
        meta_rows = [
            [
                Paragraph("客户名称", self.styles["MetaLabel"]),
                Paragraph(f"<b>{a.customer_name}</b>", self.styles["MetaValue"]),
                Paragraph("所属行业", self.styles["MetaLabel"]),
                Paragraph(industry or "—", self.styles["MetaValue"]),
            ],
            [
                Paragraph("评估日期", self.styles["MetaLabel"]),
                Paragraph(a.assessed_at.strftime("%Y年%m月%d日"), self.styles["MetaValue"]),
                "",
                "",
            ],
        ]
        meta = Table(meta_rows, colWidths=[2.6 * cm, 5.6 * cm, 2.6 * cm, 5.6 * cm])
        meta.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.SURFACE_2),
            ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.BORDER_SOFT),
            ("SPAN", (1, 1), (-1, 1)),
        ]))
        return [
            HRFlowable(width="100%", thickness=0.5 * cm, color=self.BRAND),
            Spacer(1, 2.6 * cm),
            Paragraph("客情评估智能体", self.styles["CoverBrand"]),
            Spacer(1, 0.5 * cm),
            Paragraph("客情健康度评估报告", self.styles["CoverTitle"]),
            Spacer(1, 0.35 * cm),
            HRFlowable(width=5.5 * cm, thickness=2.5, color=self.BRAND, hAlign="CENTER"),
            Spacer(1, 1.8 * cm),
            meta,
            Spacer(1, 1.6 * cm),
            Paragraph(
                f'<font color="{color.hexval()}"><b>{a.level}</b></font>'
                f'<font color="#94a3b8" size="16">　综合客情评分　</font>'
                f'<font color="{color.hexval()}"><b>{a.total_score:.1f}</b></font>'
                f'<font color="#94a3b8" size="16"> / {a.max_score:.0f}</font>',
                ParagraphStyle("CoverScore", fontName=self.FONT_NAME, fontSize=30,
                              leading=40, alignment=TA_CENTER, textColor=color),
            ),
            Spacer(1, 0.6 * cm),
            Paragraph(
                f'<font color="{color.hexval()}"><b>{self._cover_brief(a)}</b></font>',
                ParagraphStyle("CoverBrief", fontName=self.FONT_NAME, fontSize=11.5,
                              leading=18, alignment=TA_CENTER, textColor=color),
            ),
            Spacer(1, 1.2 * cm),
            Paragraph(f"报告生成时间：{self._now_str()}", self.styles["CoverSubtitle"]),
            Spacer(1, 1 * cm),
        ]

    def _cover_brief(self, a: AssessmentResponse) -> str:
        """封面一句话结论速览（确定性文案，不依赖 AI）。"""
        n_alerts = len(a.alerts) if a.alerts else len(a.risk_alerts)
        n_high = sum(1 for x in (a.alerts or []) if x.level == "high")
        if self._level_is_risky(a):
            if n_alerts:
                high_txt = f"，其中高优先级 {n_high} 项" if n_high else ""
                return f"发现 {n_alerts} 项风险信号{high_txt}，建议重点跟进"
            return "客情评分已落入风险区间，建议主动排查、重点跟进"
        if n_alerts:
            return f"发现 {n_alerts} 项风险信号，整体可控，建议按计划跟进"
        return "客情状态良好，未发现明显风险信号"


class _OverviewMixin:
    """综合评分：大数字 + 等级标尺（分数游标 + 等级色块 + 区间标注）+ 评估结论。"""

    def _overview(self, a: AssessmentResponse, trend: AssessmentTrendResponse | None = None) -> list:
        color = self._color_for(a.level)
        pct = min(100, max(0, a.total_score / max(a.max_score, 1) * 100))
        bar_w = 15 * cm
        elements = [
            KeepTogether([
                self._section_header("一", "综合评分"),
                Spacer(1, 0.7 * cm),
                Paragraph(
                    f'<font color="{color.hexval()}"><b>{a.level}</b></font>'
                    f'<font color="#94a3b8" size="18">　</font>'
                    f'<font color="{color.hexval()}"><b>{a.total_score:.1f}</b></font>'
                    f'<font color="#94a3b8" size="14"> 分</font>',
                    ParagraphStyle("HeroLine", fontName=self.FONT_NAME, fontSize=30,
                                  leading=40, alignment=TA_CENTER, textColor=color),
                ),
                Spacer(1, 0.2 * cm),
            ]),
        ]
        elements.extend(self._build_level_scale(a, color, bar_w, pct))
        elements.append(KeepTogether([self._build_conclusion_card(a, trend)]))
        elements.append(Spacer(1, 0.4 * cm))
        return elements

    def _build_conclusion_card(self, a: AssessmentResponse, trend: AssessmentTrendResponse | None):
        """评估结论卡：左侧红色竖条 + 浅灰底（沿用 DESIGN 引用块样式），分点呈现。"""
        rows = [[Paragraph(
            '<font color="#E60012"><b>评估结论</b></font>',
            ParagraphStyle("ConclTitle", fontName=self.FONT_NAME, fontSize=11.5,
                          leading=17, textColor=self.BRAND, spaceAfter=2),
        )]]
        for label, html in self._conclusion_points(a, trend):
            # 符号只用中文字体确认有的（●/○/▲/·），▸/✓/⚠ 在 msyh 下会缺字成方框
            rows.append([Paragraph(
                f'<font color="#E60012" size="9">●</font> <b>{label}</b>'
                f'<font color="#333333">　{html}</font>',
                ParagraphStyle("ConclItem", fontName=self.FONT_NAME, fontSize=10.5,
                              leading=17, textColor=self.INK_2, spaceAfter=4),
            )])
        card = Table(rows, colWidths=[16.4 * cm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.SURFACE_2),
            ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LINEBEFORE", (0, 0), (0, -1), 3, self.BRAND),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        return card

    def _conclusion_points(self, a: AssessmentResponse, trend: AssessmentTrendResponse | None) -> list[tuple[str, str]]:
        """评估结论要点：总体评价 / 维度表现 / 风险排查 / 环比趋势 / 综合研判。"""
        color = self._color_for(a.level)
        points: list[tuple[str, str]] = [(
            "总体评价",
            f"综合客情评分 <b>{a.total_score:.1f}</b> 分（满分 {a.max_score:.0f}），"
            f'健康等级 <font color="{color.hexval()}"><b>「{a.level}」</b></font>',
        )]
        dims = [d for d in a.dimensions if d.max_score > 0]
        if len(dims) >= 2:
            strongest = max(dims, key=lambda d: d.score / d.max_score)
            weakest = min(dims, key=lambda d: d.score / d.max_score)
            if strongest is not weakest:
                points.append((
                    "维度表现",
                    f"「{strongest.name}」最佳（得分率 {strongest.score / strongest.max_score * 100:.0f}%）；"
                    f"「{weakest.name}」相对薄弱（得分率 {weakest.score / weakest.max_score * 100:.0f}%），"
                    "可作为后续改进重点",
                ))
        n_alerts = len(a.alerts) if a.alerts else len(a.risk_alerts)
        n_high = sum(1 for x in (a.alerts or []) if x.level == "high")
        if n_alerts:
            high_txt = f"，其中高优先级 <b>{n_high}</b> 项" if n_high else ""
            points.append(("风险排查", f"共发现 <b>{n_alerts}</b> 项风险信号{high_txt}"))
        else:
            points.append(("风险排查", "未发现明显风险信号"))
        if trend is not None and trend.previous_score is not None:
            if trend.trend == "flat":
                points.append(("环比趋势", f"较上次评估（{trend.previous_score:.1f} 分）基本持平"))
            else:
                verb = "上升" if trend.trend == "up" else "下降"
                points.append(("环比趋势", f"较上次评估（{trend.previous_score:.1f} 分）{verb} {abs(trend.delta):.1f} 分"))
        if self._level_is_risky(a):
            points.append(("综合研判", "客情已处于风险区间，建议参照「推荐策略」尽快制定并落实跟进计划"))
        else:
            points.append(("综合研判", "客情整体可控，建议按「推荐策略」持续巩固，并关注潜在风险信号"))
        return points

    def _build_level_scale(
        self, a: AssessmentResponse, color: HexColor, bar_w, pct: float
    ) -> list:
        """综合评分下方的等级标尺：分数游标 + 等级色块 + 区间标注。"""
        elements: list = []
        level_ranges = self._levels()
        total_span = level_ranges[-1][2] - level_ranges[0][1] + 1 if level_ranges else 100
        level_order = [(name, lo, hi) for name, lo, hi, _ in level_ranges]
        range_pcts = [max(0.02, (hi - lo + 1) / total_span) for _, lo, hi in level_order]
        pct_sum = sum(range_pcts)
        range_pcts = [rp / pct_sum for rp in range_pcts]
        indicator_pct = max(2, min(98, pct)) / 100
        score_w = 1.8 * cm
        left_w = bar_w * indicator_pct - score_w / 2
        right_w = bar_w - left_w - score_w
        if left_w < 0:
            left_w = 0
            right_w = bar_w - score_w
        if right_w < 0:
            right_w = 0
            score_w = bar_w - left_w

        marker = Table(
            [[
                "",
                Paragraph(
                    f'<font color="{color.hexval()}"><b>{a.total_score:.1f}</b></font>',
                    ParagraphStyle("Marker", fontName=self.FONT_NAME, fontSize=9,
                                  leading=13, alignment=TA_CENTER),
                ),
                "",
            ]],
            colWidths=[left_w, score_w, right_w],
            hAlign="CENTER",
        )
        marker.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        elements.append(marker)

        scale_row_cells = []
        for lvl_name, lo, hi in level_order:
            lvl_color = self._color_for(lvl_name)
            is_current = lvl_name == a.level
            cell_text = "white" if is_current else lvl_color.hexval()
            scale_row_cells.append(Paragraph(
                f'<font color="{cell_text}"><b>{lvl_name}</b></font>',
                ParagraphStyle(f"Scale_{lvl_name}", fontName=self.FONT_NAME,
                              fontSize=9.5, leading=14, alignment=TA_CENTER),
            ))
        scale = Table(
            [scale_row_cells],
            colWidths=[bar_w * rp for rp in range_pcts],
            rowHeights=[0.62 * cm],
            hAlign="CENTER",
        )
        scale_style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ]
        for i, (lvl_name, lo, hi) in enumerate(level_order):
            is_current = lvl_name == a.level
            lvl_color = self._color_for(lvl_name)
            bg = lvl_color if is_current else self.SURFACE_2
            scale_style_cmds.append(("BACKGROUND", (i, 0), (i, 0), bg))
            if i > 0:
                scale_style_cmds.append(("LINEBEFORE", (i, 0), (i, 0), 0.5, self.BORDER))
        scale.setStyle(TableStyle(scale_style_cmds))
        elements.append(scale)

        range_row_cells = []
        for lvl_name, lo, hi in level_order:
            range_row_cells.append(Paragraph(
                f'<font color="#666666" size="8">{lo}-{hi}分</font>',
                ParagraphStyle(f"Range_{lvl_name}", fontName=self.FONT_NAME,
                              fontSize=8, leading=11, alignment=TA_CENTER),
            ))
        range_row = Table(
            [range_row_cells],
            colWidths=[bar_w * rp for rp in range_pcts],
            hAlign="CENTER",
        )
        range_row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        elements.append(range_row)
        elements.append(Spacer(1, 0.6 * cm))
        return elements


class _DimensionMixin:
    """分维度明细：得分占比总览表 + 逐维度因子明细卡。

    强弱一眼可辨：占比条与得分按「得分率 → 等级色」着色（复用评分配置等级区间），
    低于亚健康线的维度在卡片标题处标记「需关注」。
    """

    def _ratio_level_color(self, ratio: float) -> HexColor:
        """得分率 → 等级色（等级区间连续，取首个 pct <= hi 的档）。"""
        pct = max(0.0, min(100.0, ratio * 100))
        fallback = self.MUTED
        for name, lo, hi, color in self._levels():
            try:
                c = HexColor(color)
            except Exception:
                continue
            fallback = c
            if pct <= hi:
                return c
        return fallback

    def _ratio_needs_attention(self, ratio: float) -> bool:
        """得分率低于「亚健康」线（等级配置倒数第二档下限）视为需关注。"""
        levels = self._levels()
        line = levels[-2][1] if len(levels) >= 2 else 60
        return ratio * 100 < line

    def _dimension_detail(self, a: AssessmentResponse) -> list:
        name_w, bar_w, score_w = 4.8 * cm, 9.2 * cm, 3.2 * cm
        elements = [
            KeepTogether([
                self._section_header("二", "分维度得分明细"),
                Spacer(1, 0.3 * cm),
                Paragraph(
                    "占比条按得分率着色（绿=健康，橙=亚健康，红=偏弱），低于亚健康线的维度标记「需关注」。",
                    self.styles["SmallCN"],
                ),
                Spacer(1, 0.4 * cm),
                self._build_dimension_summary(a, name_w, bar_w, score_w),
            ]),
            Spacer(1, 0.6 * cm),
        ]
        elements.extend(self._dimension_cards(a))
        return elements

    def _build_dimension_summary(
        self, a: AssessmentResponse, name_w, bar_w, score_w
    ) -> Table:
        """维度得分占比总览表（每维度一行：名称 + 占比条 + 得分/满分）。"""
        head = ParagraphStyle("SumHead", fontName=self.FONT_NAME, fontSize=9,
                              leading=13, textColor=self.MUTED)
        summary_rows = [[
            Paragraph("<b>维度</b>", head),
            Paragraph("<b>得分占比</b>", head),
            Paragraph("<b>得分 / 满分</b>", head),
        ]]
        for d in a.dimensions:
            pct = d.score / d.max_score
            clamped = max(0.02, min(0.98, pct))
            dim_color = self._ratio_level_color(pct)
            bar = Table(
                [["", ""]],
                colWidths=[bar_w * clamped, bar_w * (1 - clamped)],
                rowHeights=[0.34 * cm],
            )
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), dim_color),
                ("BACKGROUND", (1, 0), (1, 0), self.SURFACE_2),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("ROUNDEDCORNERS", [4, 4, 4, 4]),
            ]))
            summary_rows.append([
                Paragraph(d.name, ParagraphStyle(
                    "SumName", fontName=self.FONT_NAME, fontSize=10, leading=14,
                    textColor=self.INK)),
                bar,
                Paragraph(
                    f'<font color="{dim_color.hexval()}"><b>{d.score:.1f}</b></font>'
                    f'<font color="#666666"> / {d.max_score:.0f}</font>',
                    ParagraphStyle("SumScore", fontName=self.FONT_NAME, fontSize=10,
                                  leading=14, textColor=self.INK_2),
                ),
            ])
        summary = Table(summary_rows, colWidths=[name_w, bar_w, score_w], repeatRows=1)
        summary_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), self.SURFACE_2),
            ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]
        for r in range(1, len(summary_rows) - 1):
            summary_cmds.append(("LINEBELOW", (0, r), (-1, r), 0.5, self.BORDER_SOFT))
        summary.setStyle(TableStyle(summary_cmds))
        return summary

    # 因子明细的得分后缀（模板统一以 "：+{score}分" 结尾）
    _SCORE_SUFFIX = re.compile(r"：\s*[+\-]?\d+(?:\.\d+)?\s*分$")
    # 定性因子现状写法「X」
    _QUALITATIVE = re.compile(r"^(.+?)「(.*)」$")
    # 基准值括注（内容含数字/比较符，如 （100%）（≥3次）（>5亿）），对外不展示；
    # 档级描述（如（较低）（良好））是现状的一部分，予以保留
    _BENCHMARK_NOTE = re.compile(r"（[^（）]*[0-9≥≤><][^（）]*）$")
    # 去掉基准后只剩单位的视为未填写
    _UNIT_ONLY = {"", "%", "次", "天", "条", "人", "家", "万元"}

    @classmethod
    def _factor_status_text(cls, detail: str) -> str:
        """因子明细对外口径：去得分后缀与基准值，统一为「名称：现状」。

        模板原始形态：定性 "名称「现状」"；数值 "名称 现状（基准）"
        （因子名不含空格，按首个空格切分名称与现状）。
        """
        text = cls._SCORE_SUFFIX.sub("", detail or "").strip()
        m = cls._QUALITATIVE.match(text)
        if m:
            return f"{m.group(1)}：{m.group(2) or '未填写'}"
        label, sep, rest = text.partition(" ")
        if not sep:
            return text
        rest = cls._BENCHMARK_NOTE.sub("", rest.strip()).rstrip()  # 空值时模板残留双空格
        if rest in cls._UNIT_ONLY:
            return f"{label}：未填写"
        return f"{label}：{rest}"

    def _dimension_cards(self, a: AssessmentResponse) -> list:
        """逐维度因子明细卡（两列排布，压缩纵向长度）；得分按得分率着色，薄弱维度标记「需关注」。"""
        elements: list = []
        for d in a.dimensions:
            pct = d.score / d.max_score if d.max_score else 0
            dim_color = self._ratio_level_color(pct)
            title_html = f"<b>{d.name}</b>"
            if self._ratio_needs_attention(pct):
                title_html += '<font size="8" color="#DD5B00">　▲ 需关注</font>'
            details = [self._factor_status_text(x) for x in (d.details or [])]
            half = (len(details) + 1) // 2
            left_items = details[:half]
            right_items = details[half:]
            left = "<br/>".join(f'<font color="#333333">· {x}</font>' for x in left_items) or "暂无明细"
            right = "<br/>".join(f'<font color="#333333">· {x}</font>' for x in right_items) or ""
            card_data = [
                [
                    Paragraph(title_html, self.styles["CardTitle"]),
                    Paragraph(
                        f'<font color="{dim_color.hexval()}"><b>{d.score:.1f}</b></font>'
                        f'<font color="#666666"> / {d.max_score:.0f} 分</font>',
                        self.styles["CardScore"],
                    ),
                ],
                [
                    Paragraph(left, self.styles["CardDetail"]),
                    Paragraph(right, self.styles["CardDetail"]),
                ],
            ]
            card = Table(card_data, colWidths=[8.6 * cm, 8.6 * cm], hAlign="LEFT")
            card.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, -1), white),
                ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
                ("ROUNDEDCORNERS", [8, 8, 8, 8]),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.BORDER_SOFT),
            ]))
            # 卡片不可跨页拆分（卡头与明细必须在同一页）
            elements.append(KeepTogether([card, Spacer(1, 0.4 * cm)]))
        return elements


class _AlertMixin:
    """风险排查：计数小结 + 结构化排查表（级别徽章 + 排查发现）+ 改进建议绿块。"""

    _LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低"}

    def _alerts(self, a: AssessmentResponse) -> list:
        elements = [Spacer(1, 0.5 * cm)]
        header = self._section_header("三", "风险排查")

        items = self._normalize_alerts(a)
        risky = self._level_is_risky(a)
        if items:
            counts = {"high": 0, "medium": 0, "low": 0}
            for level, _ in items:
                counts[level] = counts.get(level, 0) + 1
            seg = " · ".join(
                f"{self._LEVEL_LABEL[k]} {counts[k]} 项"
                for k in ("high", "medium", "low")
                if counts.get(k)
            )
            # 标题与小结绑定，避免孤行标题落在页尾
            elements.append(KeepTogether([
                header,
                Spacer(1, 0.3 * cm),
                Paragraph(
                    f"本次排查共发现 <b>{len(items)}</b> 项风险信号（{seg}）。",
                    self.styles["BodyCN"],
                ),
            ]))
            elements.append(Spacer(1, 0.2 * cm))
            elements.append(self._build_alert_table(items))
            if risky:
                elements.append(Spacer(1, 0.25 * cm))
                elements.append(Paragraph(
                    f'<font color="{self.DANGER.hexval()}"><b>当前等级「{a.level}」'
                    f"（{a.total_score:.1f} 分）已落入高风险区间，以上风险需优先处置。</b></font>",
                    self.styles["BodyCN"],
                ))
        elif risky:
            elements.append(KeepTogether([
                header,
                Spacer(1, 0.3 * cm),
                Paragraph(
                    f'<font color="{self.DANGER.hexval()}"><b>▲ 暂未触发具体预警规则，但评分已落入「{a.level}」区间，'
                    f'建议主动排查：竞品介入、关键人变动、互动频次、回款与满意度。</b></font>',
                    self.styles["BodyCN"],
                ),
            ]))
        else:
            elements.append(KeepTogether([
                header,
                Spacer(1, 0.3 * cm),
                Paragraph(
                    f'<font color="{self.SUCCESS.hexval()}"><b>●</b></font> 本次排查未发现明显风险信号，客情状态良好。',
                    self.styles["BodyCN"],
                ),
            ]))

        elements.append(Spacer(1, 0.5 * cm))
        if a.suggestions:
            sugg_items = [
                Paragraph(
                    f'<font color="#1AAE39" size="9"><b>●</b></font> <font color="#333333">{s}</font>',
                    ParagraphStyle("SuggItem", fontName=self.FONT_NAME, fontSize=10.5,
                                  leading=17, textColor=self.INK_2, spaceAfter=4),
                )
                for s in a.suggestions
            ]
            # 建议块整体不跨页
            elements.append(KeepTogether([self._build_suggestion_block(sugg_items)]))
        return elements

    @staticmethod
    def _normalize_alerts(a: AssessmentResponse) -> list[tuple[str, str]]:
        """结构化预警优先；仅有纯文本 risk_alerts 时按中级别兜底。"""
        if a.alerts:
            return [
                (x.level if x.level in _AlertMixin._LEVEL_LABEL else "medium", x.message)
                for x in a.alerts
            ]
        return [("medium", m) for m in a.risk_alerts]

    def _level_badge(self, level: str) -> Table:
        """级别徽章：语义色 12% 底 + 同色文字（沿用 DESIGN 徽章规则）。"""
        fg, bg = {
            "high": (self.DANGER, self.DANGER_SOFT),
            "medium": (self.WARNING, self.WARNING_SOFT),
            "low": (self.INFO, self.INFO_SOFT),
        }.get(level, (self.WARNING, self.WARNING_SOFT))
        badge = Table(
            [[Paragraph(
                f'<font color="{fg.hexval()}"><b>{self._LEVEL_LABEL.get(level, "中")}</b></font>',
                ParagraphStyle(f"Badge_{level}", fontName=self.FONT_NAME, fontSize=9,
                              leading=13, alignment=TA_CENTER),
            )]],
            colWidths=[1.5 * cm],
            rowHeights=[0.46 * cm],
            hAlign="LEFT",
        )
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return badge

    def _build_alert_table(self, items: list[tuple[str, str]]) -> Table:
        """排查表：级别 | 排查发现，hairline 分隔的无外框行列表风格。"""
        head = ParagraphStyle("AlertHead", fontName=self.FONT_NAME, fontSize=9,
                              leading=13, textColor=self.MUTED)
        rows = [[Paragraph("<b>级别</b>", head), Paragraph("<b>排查发现</b>", head)]]
        for level, message in items:
            rows.append([
                self._level_badge(level),
                Paragraph(
                    f'<font color="#333333">{message}</font>',
                    ParagraphStyle("AlertMsg", fontName=self.FONT_NAME, fontSize=10,
                                  leading=16, textColor=self.INK_2),
                ),
            ])
        table = Table(rows, colWidths=[2.2 * cm, 14.2 * cm], repeatRows=1)
        cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), self.SURFACE_2),
            ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]
        for r in range(1, len(rows) - 1):
            cmds.append(("LINEBELOW", (0, r), (-1, r), 0.5, self.BORDER_SOFT))
        table.setStyle(TableStyle(cmds))
        return table

    def _build_suggestion_block(self, items: list) -> Table:
        """改进建议：白卡 + 左侧绿色竖条 + hairline 边框（与策略卡同一卡片语言，不用绿底块）。"""
        block = Table(
            [[Paragraph(
                '<font color="#1AAE39"><b>改进建议</b></font>',
                ParagraphStyle("SuggTitle", fontName=self.FONT_NAME, fontSize=12,
                              leading=17, textColor=self.SUCCESS, spaceAfter=4),
            )]] + [[item] for item in items],
            colWidths=[16.4 * cm],
        )
        block.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), white),
            ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LINEBEFORE", (0, 0), (0, -1), 3, self.SUCCESS),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.BORDER_SOFT),
        ]))
        return block


class _FooterMixin:
    """页脚：评估方法说明 + 免责语 + 生成时间（对外口径）。"""

    def _footer(self) -> list:
        model_line1, model_line2 = self._model_description()
        return [
            Spacer(1, 1.2 * cm),
            HRFlowable(width="100%", thickness=0.6, color=self.BRAND),
            Spacer(1, 0.35 * cm),
            Paragraph(
                '<font color="#E60012"><b>评估方法说明</b></font>',
                ParagraphStyle("FooterTitle", fontName=self.FONT_NAME, fontSize=9,
                              leading=14, textColor=self.BRAND),
            ),
            Spacer(1, 0.15 * cm),
            Paragraph(model_line1, self.styles["SmallCN"]),
            Paragraph(model_line2, self.styles["SmallCN"]),
            Spacer(1, 0.25 * cm),
            Paragraph(
                f"本报告基于评估日可得信息生成，供决策参考 · {self._now_str()}",
                self.styles["SmallCN"],
            ),
        ]
