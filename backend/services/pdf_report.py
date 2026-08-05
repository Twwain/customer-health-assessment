from __future__ import annotations

import io
import os

import matplotlib.font_manager as fm

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether, Image as RLImage
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus.flowables import HRFlowable

from schemas import AssessmentResponse, AssessmentTrendResponse


class PdfReportGenerator:
    FONT_NAME = "ChineseFont"
    # 兜底色板：仅在评分配置加载失败时使用（正常路径以 scoring_config.yaml 的 levels 为准）
    COLORS = {
        "优秀": HexColor("#84cc16"),
        "良好": HexColor("#0ea5e9"),
        "一般": HexColor("#d97706"),
        "风险": HexColor("#ef4444"),
    }
    # 兜底分段：与默认配置一致（55/70/85 三档线）
    _FALLBACK_LEVELS = [
        ("风险", 0, 54, "#ef4444"),
        ("一般", 55, 69, "#d97706"),
        ("良好", 70, 84, "#0ea5e9"),
        ("优秀", 85, 100, "#84cc16"),
    ]

    def __init__(self):
        self._register_fonts()
        self.styles = self._build_styles()

    # ── 评分配置（scoring_config.yaml）─────────────────────────────────────

    @staticmethod
    def _levels() -> list[tuple[str, int, int, str]]:
        """从评分配置构建等级分段 [(name, lo, hi, color_hex)]，按分数升序。

        等级阈值全部来自 scoring_config.yaml，配置改名/改阈值后 PDF 自动跟随，
        不再使用写死的"优秀/良好/一般/风险"。
        """
        try:
            from services.scoring import load_scoring_config

            config = load_scoring_config()
            total = int(config.total_max_score)
            ordered = sorted(config.levels, key=lambda lv: lv.min_score)  # 升序
            if not ordered:
                raise ValueError("levels 为空")
            out: list[tuple[str, int, int, str]] = []
            for i, lv in enumerate(ordered):
                lo = 0 if i == 0 else int(lv.min_score)
                hi = total if i == len(ordered) - 1 else int(ordered[i + 1].min_score) - 1
                out.append((lv.name, lo, hi, lv.color))
            return out
        except Exception:
            return list(PdfReportGenerator._FALLBACK_LEVELS)

    def _color_for(self, level_name: str, default: HexColor | None = None) -> HexColor:
        """等级颜色：优先评分配置，其次兜底色板，最后中性灰。"""
        for name, _, _, color in self._levels():
            if name == level_name:
                try:
                    return HexColor(color)
                except Exception:
                    break
        fallback = self.COLORS.get(level_name)
        if fallback is not None:
            return fallback
        return default or HexColor("#64748b")

    @staticmethod
    def _model_description() -> tuple[str, str]:
        """评分模型说明文案（页脚用），从评分配置动态生成。"""
        try:
            from services.scoring import load_scoring_config

            config = load_scoring_config()
            dims = [d for d in config.dimensions if d.enabled]
            n = len(dims)
            total = config.total_max_score
            line1 = f"综合健康分由 {n} 个维度加权求和得出（满分 {total:.0f} 分）："
            parts = [f"{d.name}（{d.max_score:.0f} 分）" for d in dims]
            return line1, "、".join(parts) + "。"
        except Exception:
            return (
                "综合健康分由 4 个维度各 25 分加权求和得出（满分 100 分）：",
                "关系深度、客户满意度、商业价值、风险水平。",
            )

    def _register_fonts(self):
        # 先尝试已知路径（快速通道）
        explicit = [
            # Windows
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            # macOS
            "/System/Library/Fonts/PingFang.ttc",
            # Docker / Linux (WenQuanYi)
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ]
        for fp in explicit:
            if os.path.exists(fp):
                try:
                    pdfmetrics.registerFont(TTFont(self.FONT_NAME, fp))
                    return
                except Exception:
                    continue

        # 动态搜索：用 matplotlib font_manager
        cjk_kw = ["hei", "song", "ming", "yuan", "cjk", "noto", "yahei", "pingfang", "wqy"]
        for fp in fm.findSystemFonts():
            if any(kw in os.path.basename(fp).lower() for kw in cjk_kw):
                try:
                    pdfmetrics.registerFont(TTFont(self.FONT_NAME, fp))
                    return
                except Exception:
                    continue

        # 最后手段：遍历系统字体目录
        for base in ["C:/Windows/Fonts", "/System/Library/Fonts", "/usr/share/fonts"]:
            if not os.path.exists(base):
                continue
            for root, _, files in os.walk(base):
                for f in files:
                    if f.lower().endswith(('.ttf', '.ttc', '.otf')):
                        try:
                            pdfmetrics.registerFont(TTFont(self.FONT_NAME, os.path.join(root, f)))
                            return
                        except Exception:
                            continue

        self.FONT_NAME = "Helvetica"

    def _on_page(self, canvas, doc):
        if doc.page == 1:
            return  # 封面不加页眉页脚
        canvas.saveState()
        canvas.setFont(self.FONT_NAME, 8)
        canvas.setFillColor(HexColor("#94a3b8"))
        canvas.drawString(2 * cm, 1.5 * cm, "客情健康度评估系统")
        canvas.drawRightString(A4[0] - 2 * cm, 1.5 * cm, f"第 {doc.page} 页")
        canvas.restoreState()

    def _build_styles(self):
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(
            "CoverTitle", fontName=self.FONT_NAME, fontSize=28, leading=36,
            alignment=TA_CENTER, textColor=black, spaceAfter=20,
        ))
        styles.add(ParagraphStyle(
            "CoverSubtitle", fontName=self.FONT_NAME, fontSize=14, leading=20,
            alignment=TA_CENTER, textColor=HexColor("#666666"),
        ))
        styles.add(ParagraphStyle(
            "SectionTitle", fontName=self.FONT_NAME, fontSize=16, leading=24,
            spaceBefore=20, spaceAfter=10, textColor=HexColor("#1e293b"),
        ))
        styles.add(ParagraphStyle(
            "BodyCN", fontName=self.FONT_NAME, fontSize=11, leading=18,
            spaceAfter=6, textColor=HexColor("#334155"),
        ))
        styles.add(ParagraphStyle(
            "SmallCN", fontName=self.FONT_NAME, fontSize=9, leading=14,
            textColor=HexColor("#64748b"),
        ))
        styles.add(ParagraphStyle(
            "ScoreBig", fontName=self.FONT_NAME, fontSize=48, leading=56,
            alignment=TA_CENTER, textColor=HexColor("#1e293b"),
        ))
        styles.add(ParagraphStyle(
            "CardTitle", fontName=self.FONT_NAME, fontSize=11, leading=16,
            textColor=HexColor("#1e293b"),
        ))
        styles.add(ParagraphStyle(
            "CardScore", fontName=self.FONT_NAME, fontSize=13, leading=18,
            textColor=HexColor("#64748b"),
        ))
        styles.add(ParagraphStyle(
            "CardDetail", fontName=self.FONT_NAME, fontSize=10, leading=17,
            textColor=HexColor("#475569"),
        ))
        return styles

    def generate(
        self,
        a: AssessmentResponse,
        *,
        strategy_items: list[dict] | None = None,
        references: list[dict] | None = None,
        trend: AssessmentTrendResponse | None = None,
        degraded: bool = False,
        ai_error: str | None = None,
    ) -> bytes:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm,
                                leftMargin=2 * cm, rightMargin=2 * cm,
                                onPage=self._on_page)
        story = []
        story.extend(self._cover(a))
        story.append(PageBreak())
        story.extend(self._overview(a))
        story.extend(self._dimension_detail(a))
        story.extend(self._alerts(a))
        story.extend(self._ai_strategy_section(strategy_items, references, degraded, ai_error))
        story.extend(self._trend_section_pdf(trend))
        story.extend(self._footer())
        doc.build(story)
        return buf.getvalue()

    def _cover(self, a: AssessmentResponse) -> list:
        return [
            HRFlowable(width="100%", thickness=3, color=self._color_for(a.level, HexColor("#d97706"))),
            Spacer(1, 4 * cm),
            Paragraph("客情健康度评估报告", self.styles["CoverTitle"]),
            Spacer(1, 1 * cm),
            HRFlowable(width="60%", thickness=1, color=HexColor("#cbd5e1")),
            Spacer(1, 1.5 * cm),
            Paragraph(f"客户名称：{a.customer_name}", self.styles["CoverSubtitle"]),
            Spacer(1, 0.5 * cm),
            Paragraph(f"评估日期：{a.assessed_at.strftime('%Y年%m月%d日')}", self.styles["CoverSubtitle"]),
            Spacer(1, 0.5 * cm),
            Paragraph("客情健康度评估系统", self.styles["CoverSubtitle"]),
            Spacer(1, 3 * cm),
        ]

    def _overview(self, a: AssessmentResponse) -> list:
        color = self._color_for(a.level)
        pct = min(100, max(0, a.total_score / max(a.max_score, 1) * 100))
        bar_w = 12 * cm
        elements = [
            Paragraph("一、综合评分", self.styles["SectionTitle"]),
            Spacer(1, 0.8 * cm),
        ]
        # 等级 + 分数并排，放大突出
        elements.append(Paragraph(
            f'<font color="{color.hexval()}"><b>{a.level}</b></font>'
            f'<font color="#cbd5e1">　</font>'
            f'<font color="{color.hexval()}"><b>{a.total_score:.1f}</b></font>'
            f'<font color="#94a3b8" size="16"> 分</font>',
            ParagraphStyle("HeroLine", fontName=self.FONT_NAME, fontSize=32,
                          leading=40, alignment=TA_CENTER, textColor=color),
        ))
        elements.append(Spacer(1, 0.1 * cm))
        # 小标签 — 置于值下方
        label_table = Table(
            [[
                Paragraph('<font color="#94a3b8">健康等级</font>',
                          ParagraphStyle("LblH", fontName=self.FONT_NAME, fontSize=8,
                                        alignment=TA_CENTER, textColor=HexColor("#94a3b8"))),
                Paragraph('<font color="#94a3b8">综合健康分</font>',
                          ParagraphStyle("LblS", fontName=self.FONT_NAME, fontSize=8,
                                        alignment=TA_CENTER, textColor=HexColor("#94a3b8"))),
            ]],
            colWidths=[4 * cm, 4 * cm],
            hAlign="CENTER",
        )
        label_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(label_table)
        elements.append(Spacer(1, 0.7 * cm))

        # 等级标识条 — 上方标注分数位置（分段来自评分配置，不写死）
        level_ranges = self._levels()  # [(name, lo, hi, color_hex)] 升序
        total_span = level_ranges[-1][2] - level_ranges[0][1] + 1 if level_ranges else 100
        level_order = [(name, lo, hi) for name, lo, hi, _ in level_ranges]
        range_pcts = [max(0.02, (hi - lo + 1) / total_span) for _, lo, hi in level_order]
        # 归一化使列宽合计恰好等于条宽
        pct_sum = sum(range_pcts)
        range_pcts = [rp / pct_sum for rp in range_pcts]
        indicator_pct = max(2, min(98, pct)) / 100
        score_w = 1.8 * cm

        # 分数位置标注行
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

        # 等级分段条
        scale_row_cells = []
        for lvl_name, lo, hi in level_order:
            lvl_color = self._color_for(lvl_name)
            is_current = lvl_name == a.level
            cell_text = "white" if is_current else lvl_color.hexval()
            scale_row_cells.append(Paragraph(
                f'<font color="{cell_text}"><b>{lvl_name}</b></font>',
                ParagraphStyle(f"Scale_{lvl_name}", fontName=self.FONT_NAME,
                              fontSize=9, leading=13, alignment=TA_CENTER),
            ))
        scale = Table(
            [scale_row_cells],
            colWidths=[bar_w * rp for rp in range_pcts],
            rowHeights=[0.55 * cm],
            hAlign="CENTER",
        )
        scale_style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        for i, (lvl_name, lo, hi) in enumerate(level_order):
            is_current = lvl_name == a.level
            lvl_color = self._color_for(lvl_name)
            bg = lvl_color if is_current else HexColor("#f8fafc")
            scale_style_cmds.append(("BACKGROUND", (i, 0), (i, 0), bg))
            if i > 0:
                scale_style_cmds.append(("LINEBEFORE", (i, 0), (i, 0), 0.5, HexColor("#e2e8f0")))
        scale.setStyle(TableStyle(scale_style_cmds))
        elements.append(scale)

        # 范围标注
        range_row_cells = []
        for lvl_name, lo, hi in level_order:
            range_row_cells.append(Paragraph(
                f'<font color="#94a3b8" size="7">{lo}-{hi}分</font>',
                ParagraphStyle(f"Range_{lvl_name}", fontName=self.FONT_NAME,
                              fontSize=7, leading=10, alignment=TA_CENTER),
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
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        elements.append(range_row)
        elements.append(Spacer(1, 0.8 * cm))
        return elements

    def _dimension_detail(self, a: AssessmentResponse) -> list:
        elements = [Paragraph("二、分维度得分明细", self.styles["SectionTitle"]), Spacer(1, 0.3 * cm)]

        score_color = self._color_for(a.level, HexColor("#d97706"))
        bar_w = 14 * cm

        for d in a.dimensions:
            pct = d.score / d.max_score
            # 卡片容器：白色背景带浅灰边框
            card_data = [
                [Paragraph(
                    f'<b>{d.name}</b>',
                    self.styles["CardTitle"],
                ),
                 Paragraph(
                     f'<font color="{score_color.hexval()}"><b>{d.score:.1f}</b></font>'
                     f'<font color="#94a3b8"> 分</font>',
                     self.styles["CardScore"],
                 )],
                [Paragraph(
                    "".join(
                        f'<font color="#475569">· {detail}</font><br/>'
                        for detail in d.details
                    ),
                    self.styles["CardDetail"],
                )],
            ]

            card = Table(
                card_data,
                colWidths=[bar_w * 0.45, bar_w * 0.55],
                hAlign="LEFT",
            )
            card.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("BACKGROUND", (0, 0), (-1, -1), white),
                ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
                ("SPAN", (0, 1), (-1, 1)),
                ("LINEBELOW", (0, 0), (-1, 0), 0, white),
            ]))

            # 进度条与卡片严格对齐：相同宽度和左对齐
            clamped_pct = max(0.02, min(0.98, pct))
            bar = Table(
                [["", ""]],
                colWidths=[bar_w * clamped_pct, bar_w * (1 - clamped_pct)],
                rowHeights=[0.28 * cm],
                hAlign="LEFT",
            )
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), score_color),
                ("BACKGROUND", (1, 0), (1, 0), HexColor("#f1f5f9")),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, 0), 0, white),
                ("LINEABOVE", (0, 0), (-1, 0), 0, white),
            ]))

            elements.append(KeepTogether([card, bar]))
            elements.append(Spacer(1, 0.4 * cm))

        return elements

    def _alerts(self, a: AssessmentResponse) -> list:
        elements = [
            Spacer(1, 0.5 * cm),
            Paragraph("三、风险提示与建议", self.styles["SectionTitle"]),
            Spacer(1, 0.3 * cm),
        ]

        # 风险提示 — 红色醒目区块
        if a.risk_alerts:
            alert_items = [
                Paragraph(
                    f'<font color="#dc2626"><b>{alert}</b></font>',
                    ParagraphStyle("AlertItem", fontName=self.FONT_NAME, fontSize=11,
                                  leading=18, textColor=HexColor("#dc2626")),
                )
                for alert in a.risk_alerts
            ]
            alert_block = Table(
                [[item] for item in alert_items],
                colWidths=[16.5 * cm],
            )
            alert_block.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#fef2f2")),
                ("BOX", (0, 0), (-1, -1), 1, HexColor("#fecaca")),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            elements.append(alert_block)
        else:
            elements.append(Paragraph(
                '<font color="#84cc16"><b>✓</b></font> 目前未发现明显风险信号，客情状态良好。',
                self.styles["BodyCN"],
            ))

        elements.append(Spacer(1, 0.5 * cm))

        # 改进建议 — 绿色区块
        if a.suggestions:
            sugg_items = [
                Paragraph(
                    f'<font color="#4d7c0f"><b>{s}</b></font>',
                    ParagraphStyle("SuggItem", fontName=self.FONT_NAME, fontSize=11,
                                  leading=18, textColor=HexColor("#4d7c0f")),
                )
                for s in a.suggestions
            ]
            sugg_block = Table(
                [[Paragraph(
                    '<font color="#4d7c0f"><b>改进建议</b></font>',
                    ParagraphStyle("SuggTitle", fontName=self.FONT_NAME, fontSize=13,
                                  leading=18, textColor=HexColor("#4d7c0f")),
                )]] + [[item] for item in sugg_items],
                colWidths=[16.5 * cm],
            )
            sugg_block.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f7fee7")),
                ("BOX", (0, 0), (-1, -1), 1, HexColor("#bef264")),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#d9f99d")),
            ]))
            elements.append(sugg_block)

        return elements

    # ── M5：AI 策略建议 + 知识溯源 ────────────────────────────────────────────
    _GROUP_TITLES = {
        "recommended": "✅ 推荐策略",
        "alternative": "□ 备选策略",
        "long_term": "💡 长期建议",
    }
    _URGENCY_CN = {"high": "高", "medium": "中", "low": "低"}
    _URGENCY_COLOR = {"high": "#dc2626", "medium": "#d97706", "low": "#0066FF"}
    _PRIORITY_ORDER = ("recommended", "alternative", "long_term")

    def _ai_strategy_section(
        self, strategy_items, references, degraded, ai_error
    ) -> list:
        if not strategy_items:
            return []
        elements = [
            Spacer(1, 0.5 * cm),
            Paragraph("四、AI 智能策略建议", self.styles["SectionTitle"]),
            Spacer(1, 0.2 * cm),
        ]
        if degraded:
            elements.append(Paragraph(
                '<font color="#d97706"><b>⚠️ 当前 LLM 暂不可用，以下为规则引擎兜底建议。</b></font>',
                self.styles["BodyCN"],
            ))
            elements.append(Spacer(1, 0.2 * cm))
        if ai_error:
            elements.append(Paragraph(
                f'<font color="#dc2626">{ai_error}</font>',
                self.styles["SmallCN"],
            ))
            elements.append(Spacer(1, 0.2 * cm))

        for priority in self._PRIORITY_ORDER:
            group = [i for i in strategy_items if (i.get("priority") or "recommended") == priority]
            if not group:
                continue
            elements.append(Paragraph(
                self._GROUP_TITLES.get(priority, priority),
                ParagraphStyle(f"Grp_{priority}", fontName=self.FONT_NAME, fontSize=13,
                              leading=20, textColor=HexColor("#1e293b"), spaceBefore=8, spaceAfter=4),
            ))
            for item in group:
                elements.append(self._strategy_card(item))
                elements.append(Spacer(1, 0.25 * cm))

        if references:
            elements.append(Spacer(1, 0.3 * cm))
            elements.append(Paragraph(
                "📎 知识溯源",
                ParagraphStyle("RefTitle", fontName=self.FONT_NAME, fontSize=12,
                              leading=18, textColor=HexColor("#1e293b")),
            ))
            ref_lines = []
            for i, ref in enumerate(references, 1):
                title = ref.get("title") or ref.get("item_title") or "未命名知识"
                category = ref.get("category") or ""
                score = ref.get("score")
                score_txt = f" · 相似度 {score:.2f}" if isinstance(score, (int, float)) else ""
                cat_txt = f"（{category}）" if category else ""
                ref_lines.append(f"{i}. 《{title}》{cat_txt}{score_txt}")
            elements.append(Paragraph(
                "<br/>".join(ref_lines),
                ParagraphStyle("RefBody", fontName=self.FONT_NAME, fontSize=9.5,
                              leading=15, textColor=HexColor("#475569")),
            ))
        return elements

    def _strategy_card(self, item: dict):
        urgency = item.get("urgency") or "medium"
        urg_cn = self._URGENCY_CN.get(urgency, "中")
        urg_hex = self._URGENCY_COLOR.get(urgency, "#d97706")
        urg_color = HexColor(urg_hex)
        title = item.get("title") or "（未命名策略）"
        rows = [
            Paragraph(
                f'<b>{title}</b>　<font color="{urg_hex}">紧急度：{urg_cn}</font>',
                ParagraphStyle("StrTitle", fontName=self.FONT_NAME, fontSize=11,
                              leading=16, textColor=HexColor("#1e293b")),
            ),
        ]
        for label, key in (("原因", "reason"), ("行动", "action"), ("预期", "expected_outcome")):
            val = item.get(key)
            if val:
                rows.append(Paragraph(
                    f'<font color="#64748b">{label}：</font>{val}',
                    ParagraphStyle(f"Str_{key}", fontName=self.FONT_NAME, fontSize=9.5,
                                  leading=15, textColor=HexColor("#475569")),
                ))
        ref = item.get("reference")
        if ref:
            rows.append(Paragraph(
                f'<font color="#0066FF">📎 来源：{ref}</font>',
                ParagraphStyle("StrRef", fontName=self.FONT_NAME, fontSize=9,
                              leading=14, textColor=HexColor("#0066FF")),
            ))
        card = Table([[r] for r in rows], colWidths=[16.5 * cm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f8fafc")),
            ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
            ("LINEBEFORE", (0, 0), (0, -1), 3, urg_color),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return card

    # ── M5：健康分趋势图 ──────────────────────────────────────────────────────
    def _trend_section_pdf(self, trend: AssessmentTrendResponse | None) -> list:
        elements = [
            Spacer(1, 0.5 * cm),
            Paragraph("五、健康分趋势", self.styles["SectionTitle"]),
            Spacer(1, 0.2 * cm),
        ]
        if not trend or len(trend.points) < 2:
            elements.append(Paragraph(
                "当前仅有一次评估记录，暂无可对比的趋势曲线。持续评估后将自动生成历史趋势。",
                self.styles["BodyCN"],
            ))
            return elements

        try:
            elements.append(self._render_trend_chart(trend))
        except Exception:
            # 绘图失败不应拖垮整份报告
            elements.append(Paragraph("趋势曲线生成失败，已略去图示。", self.styles["SmallCN"]))

        trend_cn = {"up": "↑ 上升", "down": "↓ 下降", "flat": "→ 持平"}
        delta_txt = f"{trend.delta:+.1f}" if trend.previous_score is not None else "—"
        elements.append(Spacer(1, 0.2 * cm))
        elements.append(Paragraph(
            f'趋势：<b>{trend_cn.get(trend.trend, trend.trend)}</b>（较上次 {delta_txt} 分）',
            self.styles["BodyCN"],
        ))
        if trend.level_lines:
            lines = " / ".join(
                f"{lv.name} {lv.min_score}分" for lv in trend.level_lines if lv.min_score > 0
            )
            if lines:
                elements.append(Paragraph(
                    f'<font color="#64748b">等级参考线：{lines}</font>',
                    self.styles["SmallCN"],
                ))
        return elements

    def _render_trend_chart(self, trend: AssessmentTrendResponse):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        xs = [p.label for p in trend.points]
        ys = [p.total_score for p in trend.points]
        fig, ax = plt.subplots(figsize=(8, 3.2), dpi=150)
        ax.plot(range(len(ys)), ys, marker="o", color="#0066FF", linewidth=2.2)
        ax.set_xticks(range(len(xs)))
        ax.set_xticklabels(xs, fontsize=8)
        ax.set_ylim(0, max(100, getattr(trend, "max_score", 100) or 100))
        ax.set_ylabel("Score", fontsize=9)
        ax.set_title("Health Score Trend", fontsize=11)
        ax.grid(True, axis="y", linestyle=":", alpha=0.4)
        for lv in trend.level_lines:
            try:
                yv = float(lv.min_score)
            except (TypeError, ValueError):
                continue
            if yv <= 0:
                continue
            ax.axhline(yv, color=(lv.color or "#94a3b8"), linestyle="--", linewidth=0.9, alpha=0.8)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return RLImage(buf, width=15 * cm, height=6 * cm)

    def _footer(self) -> list:
        model_line1, model_line2 = self._model_description()
        return [
            Spacer(1, 1.5 * cm),
            HRFlowable(width="100%", thickness=0.5, color=HexColor("#e2e8f0")),
            Spacer(1, 0.3 * cm),
            Paragraph(
                "<b>评分模型说明</b>",
                ParagraphStyle("FooterTitle", fontName=self.FONT_NAME, fontSize=9,
                              leading=14, textColor=HexColor("#64748b")),
            ),
            Spacer(1, 0.15 * cm),
            Paragraph(
                model_line1,
                ParagraphStyle("FooterBody1", fontName=self.FONT_NAME, fontSize=8,
                              leading=13, textColor=HexColor("#94a3b8")),
            ),
            Paragraph(
                model_line2,
                ParagraphStyle("FooterBody2", fontName=self.FONT_NAME, fontSize=8,
                              leading=13, textColor=HexColor("#94a3b8")),
            ),
            Spacer(1, 0.3 * cm),
            Paragraph(
                f"本报告由客情健康度评估系统自动生成 · {self._now_str()}",
                self.styles["SmallCN"],
            ),
        ]

    def _now_str(self):
        import datetime
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
