"""PDF 报告章节渲染 mixin：封面 / 综合评分 / 维度明细 / 风险建议 / 页脚。

从 ``pdf_report.PdfReportGenerator`` 中按职责拆出，各 mixin 通过 MRO 访问
核心类提供的 ``self.styles`` / ``self.FONT_NAME`` / 设计 Token 与辅助方法。
"""

from __future__ import annotations

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

from schemas import AssessmentResponse


class _CoverMixin:
    """封面：品牌条 + 客户元信息 + 综合评分 + AI 参与标记。"""

    def _cover(
        self,
        a: AssessmentResponse,
        *,
        degraded: bool = False,
        has_strategy: bool = False,
        industry: str = "",
    ) -> list:
        color = self._color_for(a.level, self.BRAND)
        if degraded:
            ai_mark = f'<font color="{self.WARNING.hexval()}"><b>●</b></font>'
            ai_text = "AI 未参与：服务暂不可用，本报告由规则引擎兜底生成"
        elif has_strategy:
            ai_mark = f'<font color="{self.SUCCESS.hexval()}"><b>●</b></font>'
            ai_text = "AI 已参与：策略建议与知识溯源由 AI 生成"
        else:
            ai_mark = f'<font color="{self.MUTED.hexval()}"><b>○</b></font>'
            ai_text = "本次报告未包含 AI 策略建议"
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
                Paragraph("报告版本", self.styles["MetaLabel"]),
                Paragraph(a.config_version or "—", self.styles["MetaValue"]),
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
            Spacer(1, 1.2 * cm),
            Paragraph(
                f"{ai_mark}　{ai_text}",
                ParagraphStyle("CoverAI", fontName=self.FONT_NAME, fontSize=10.5,
                              leading=16, alignment=TA_CENTER, textColor=self.MUTED),
            ),
            Spacer(1, 0.5 * cm),
            Paragraph(f"生成时间：{self._now_str()}", self.styles["CoverSubtitle"]),
            Spacer(1, 1 * cm),
        ]


class _OverviewMixin:
    """综合评分：大数字 + 等级标尺（分数游标 + 等级色块 + 区间标注）。"""

    def _overview(self, a: AssessmentResponse) -> list:
        color = self._color_for(a.level)
        pct = min(100, max(0, a.total_score / max(a.max_score, 1) * 100))
        bar_w = 15 * cm
        elements = [
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
        ]
        elements.extend(self._build_level_scale(a, color, bar_w, pct))
        return elements

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
    """分维度明细：得分占比总览表 + 逐维度因子明细卡。"""

    def _dimension_detail(self, a: AssessmentResponse) -> list:
        elements = [
            self._section_header("二", "分维度得分明细"),
            Spacer(1, 0.3 * cm),
            Paragraph(
                "先看各维度得分占比总览，再逐维度展开因子打分明细。",
                self.styles["SmallCN"],
            ),
            Spacer(1, 0.4 * cm),
        ]
        score_color = self._color_for(a.level, self.BRAND)
        name_w, bar_w, score_w = 4.8 * cm, 9.2 * cm, 3.2 * cm
        elements.append(self._build_dimension_summary(a, score_color, name_w, bar_w, score_w))
        elements.append(Spacer(1, 0.6 * cm))
        elements.extend(self._dimension_cards(a, score_color))
        return elements

    def _build_dimension_summary(
        self, a: AssessmentResponse, score_color: HexColor, name_w, bar_w, score_w
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
            bar = Table(
                [["", ""]],
                colWidths=[bar_w * clamped, bar_w * (1 - clamped)],
                rowHeights=[0.34 * cm],
            )
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), score_color),
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
                    f'<b>{d.score:.1f}</b> / {d.max_score:.0f}',
                    ParagraphStyle("SumScore", fontName=self.FONT_NAME, fontSize=10,
                                  leading=14, textColor=self.INK_2),
                ),
            ])
        summary = Table(summary_rows, colWidths=[name_w, bar_w, score_w])
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

    def _dimension_cards(self, a: AssessmentResponse, score_color: HexColor) -> list:
        """逐维度因子明细卡（两列排布，压缩纵向长度）。"""
        elements: list = []
        for d in a.dimensions:
            details = d.details or []
            half = (len(details) + 1) // 2
            left_items = details[:half]
            right_items = details[half:]
            left = "<br/>".join(f'<font color="#333333">· {x}</font>' for x in left_items) or "暂无明细"
            right = "<br/>".join(f'<font color="#333333">· {x}</font>' for x in right_items) or ""
            card_data = [
                [
                    Paragraph(f"<b>{d.name}</b>", self.styles["CardTitle"]),
                    Paragraph(
                        f'<font color="{score_color.hexval()}"><b>{d.score:.1f}</b></font>'
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
            elements.append(card)
            elements.append(Spacer(1, 0.4 * cm))
        return elements


class _AlertMixin:
    """风险提示与改进建议：红块 / 绿块。"""

    def _alerts(self, a: AssessmentResponse) -> list:
        elements = [
            Spacer(1, 0.5 * cm),
            self._section_header("三", "风险提示与建议"),
            Spacer(1, 0.3 * cm),
        ]

        risky = self._level_is_risky(a)
        if risky:
            elements.append(Paragraph(
                f'<font color="{self.DANGER.hexval()}"><b>当前等级「{a.level}」· 客情评分 {a.total_score:.1f} 分，'
                f'已落入高风险区间，需重点跟进。</b></font>',
                self.styles["BodyCN"],
            ))
        else:
            elements.append(Paragraph(
                f'<font color="{self.SUCCESS.hexval()}"><b>当前等级「{a.level}」· 客情评分 {a.total_score:.1f} 分，整体可控。</b></font>',
                self.styles["BodyCN"],
            ))
        elements.append(Spacer(1, 0.3 * cm))

        if a.risk_alerts:
            alert_items = [
                Paragraph(
                    f'<font color="#C62828"><b>⚠ {alert}</b></font>',
                    ParagraphStyle("AlertItem", fontName=self.FONT_NAME, fontSize=10.5,
                                  leading=17, textColor=self.DANGER, spaceAfter=4),
                )
                for alert in a.risk_alerts
            ]
            elements.append(self._build_alert_block(alert_items))
        elif risky:
            elements.append(Paragraph(
                f'<font color="{self.DANGER.hexval()}"><b>⚠ 暂未触发具体预警规则，但评分已落入「{a.level}」区间，'
                f'建议主动排查：竞品介入、关键人变动、互动频次、回款与满意度。</b></font>',
                self.styles["BodyCN"],
            ))
        else:
            elements.append(Paragraph(
                f'<font color="{self.SUCCESS.hexval()}"><b>✓</b></font> 目前未发现明显风险信号，客情状态良好。',
                self.styles["BodyCN"],
            ))

        elements.append(Spacer(1, 0.5 * cm))
        if a.suggestions:
            sugg_items = [
                Paragraph(
                    f'<font color="#1AAE39"><b>· {s}</b></font>',
                    ParagraphStyle("SuggItem", fontName=self.FONT_NAME, fontSize=10.5,
                                  leading=17, textColor=self.SUCCESS, spaceAfter=4),
                )
                for s in a.suggestions
            ]
            elements.append(self._build_suggestion_block(sugg_items))
        return elements

    def _build_alert_block(self, alert_items: list) -> Table:
        """风险提示红块：标题行 + 预警条目列表。"""
        block = Table(
            [[Paragraph(
                '<font color="#C62828"><b>风险提示</b></font>',
                ParagraphStyle("AlertTitle", fontName=self.FONT_NAME, fontSize=12,
                              leading=17, textColor=self.DANGER, spaceAfter=4),
            )]] + [[item] for item in alert_items],
            colWidths=[16.4 * cm],
        )
        block.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.DANGER_SOFT),
            ("BOX", (0, 0), (-1, -1), 1, HexColor("#C6282840")),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#C6282822")),
        ]))
        return block

    def _build_suggestion_block(self, items: list) -> Table:
        """改进建议绿块：标题行 + 建议条目列表。"""
        block = Table(
            [[Paragraph(
                '<font color="#1AAE39"><b>改进建议</b></font>',
                ParagraphStyle("SuggTitle", fontName=self.FONT_NAME, fontSize=12,
                              leading=17, textColor=self.SUCCESS, spaceAfter=4),
            )]] + [[item] for item in items],
            colWidths=[16.4 * cm],
        )
        block.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.SUCCESS_SOFT),
            ("BOX", (0, 0), (-1, -1), 1, HexColor("#1AAE3940")),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#1AAE3922")),
        ]))
        return block


class _FooterMixin:
    """页脚：评分模型说明 + 生成时间。"""

    def _footer(self) -> list:
        model_line1, model_line2 = self._model_description()
        return [
            Spacer(1, 1.2 * cm),
            HRFlowable(width="100%", thickness=0.6, color=self.BRAND),
            Spacer(1, 0.35 * cm),
            Paragraph(
                '<font color="#E60012"><b>评分模型说明</b></font>',
                ParagraphStyle("FooterTitle", fontName=self.FONT_NAME, fontSize=9,
                              leading=14, textColor=self.BRAND),
            ),
            Spacer(1, 0.15 * cm),
            Paragraph(model_line1, self.styles["SmallCN"]),
            Paragraph(model_line2, self.styles["SmallCN"]),
            Spacer(1, 0.25 * cm),
            Paragraph(
                f"本报告由客情评估智能体自动生成 · {self._now_str()}",
                self.styles["SmallCN"],
            ),
        ]
