import io
import os
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus.flowables import HRFlowable

from schemas import AssessmentResponse


class PdfReportGenerator:
    FONT_NAME = "ChineseFont"
    COLORS = {
        "优秀": HexColor("#84cc16"),
        "良好": HexColor("#0ea5e9"),
        "一般": HexColor("#d97706"),
        "风险": HexColor("#ef4444"),
    }

    def __init__(self):
        self._register_fonts()
        self.styles = self._build_styles()

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

    def generate(self, a: AssessmentResponse) -> bytes:
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
        story.extend(self._footer())
        doc.build(story)
        return buf.getvalue()

    def _cover(self, a: AssessmentResponse) -> list:
        return [
            HRFlowable(width="100%", thickness=3, color=self.COLORS.get("优秀", HexColor("#d97706"))),
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
        color = self.COLORS.get(a.level, HexColor("#64748b"))
        pct = min(100, max(0, a.total_score))
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

        # 等级标识条 — 上方标注分数位置
        level_order = [("风险", 0, 54), ("一般", 55, 69), ("良好", 70, 84), ("优秀", 85, 100)]
        range_pcts = [0.54, 0.15, 0.15, 0.16]
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
            lvl_color = self.COLORS.get(lvl_name, HexColor("#64748b"))
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
            lvl_color = self.COLORS.get(lvl_name, HexColor("#64748b"))
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

    def _radar_chart(self, a: AssessmentResponse) -> Image:
        if self.FONT_NAME == "Helvetica":
            return Spacer(1, 0)

        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "PingFang SC", "WenQuanYi Micro Hei", "Noto Sans CJK SC"]
        plt.rcParams["axes.unicode_minus"] = False

        categories = [d.name for d in a.dimensions]
        values = [d.score for d in a.dimensions]
        max_vals = [d.max_score for d in a.dimensions]

        N = len(categories)
        angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
        angles += angles[:1]
        values_full = values + values[:1]
        max_full = max_vals + max_vals[:1]

        # 缩小图表尺寸，更精致的比例
        fig, ax = plt.subplots(figsize=(4.2, 4.2), subplot_kw=dict(polar=True))
        ax.set_theta_offset(np.pi / 2)
        ax.set_theta_direction(-1)

        max_val = max(max_vals) + 3
        ax.set_ylim(0, max_val)

        # 填充区域
        ax.fill(angles, max_full, alpha=0.06, color="#d1d5db")
        ax.fill(angles, values_full, alpha=0.18, color="#d97706")
        ax.plot(angles, values_full, "o-", linewidth=2.5, color="#d97706", markersize=5,
                markerfacecolor="#ffffff", markeredgewidth=2, markeredgecolor="#d97706")

        # 数据标注：偏移量按比例计算，避免遮挡
        for angle, val, max_val_item in zip(angles, values_full, max_full):
            offset = 2.5  # 固定小偏移
            ax.text(angle, val + offset, f"{val:.0f}", ha="center", va="bottom",
                    fontsize=12, fontweight="bold", color="#d97706")

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(categories, fontsize=14, fontweight="medium", color="#1e293b")
        ax.set_ylim(0, max_val)
        ax.set_yticks(np.linspace(0, max_val, 4))
        ax.set_yticklabels([f"{x:.0f}" for x in np.linspace(0, max_val, 4)], fontsize=8, color="#cbd5e1")
        ax.grid(True, color="#e2e8f0", linewidth=0.6)

        # 去掉外围边框
        ax.spines["polar"].set_visible(False)

        img_buf = io.BytesIO()
        plt.savefig(img_buf, dpi=180, bbox_inches="tight", transparent=True, format="png")
        plt.close()
        img_buf.seek(0)
        return Image(img_buf, width=10.5 * cm, height=10.5 * cm)

    def _dimension_detail(self, a: AssessmentResponse) -> list:
        elements = [Paragraph("二、分维度得分明细", self.styles["SectionTitle"]), Spacer(1, 0.3 * cm)]

        score_color = self.COLORS.get(a.level, HexColor("#d97706"))
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

    def _footer(self) -> list:
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
                "综合健康分由 4 个维度各 25 分加权求和得出（满分 100 分）：",
                ParagraphStyle("FooterBody1", fontName=self.FONT_NAME, fontSize=8,
                              leading=13, textColor=HexColor("#94a3b8")),
            ),
            Paragraph(
                "关系深度（合作年限 + 联系频率 + 最近联系时间）、"
                "客户满意度（1-10 分 × 2.5）、",
                ParagraphStyle("FooterBody2", fontName=self.FONT_NAME, fontSize=8,
                              leading=13, textColor=HexColor("#94a3b8")),
            ),
            Paragraph(
                "商业价值（合同金额 + 回款状态）、"
                "风险水平（基础 25 分 − 风险扣分 + 增长潜力加分）。",
                ParagraphStyle("FooterBody3", fontName=self.FONT_NAME, fontSize=8,
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
