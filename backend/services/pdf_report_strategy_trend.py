"""PDF 报告策略建议与趋势章节渲染 mixin。

策略章节解析 LLM 结构化条目并按推荐/备选/长期分组；趋势章节用 matplotlib
渲染评分曲线（等级背景带 / 参考线 / 数据点标注），由核心类持有线程锁串行化。
"""

from __future__ import annotations

import io

import matplotlib.font_manager as fm

from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image as RLImage,
    KeepTogether,
)

from schemas import AssessmentTrendResponse


class _StrategyMixin:
    """推荐策略 + 参考依据：三层分组卡片与来源列表。

    对外展示口径：不在报告中暴露内部引擎状态；降级产出的规则建议同样是
    可执行的推荐策略。
    """

    _GROUP_TITLES = {
        "recommended": "推荐策略",
        "alternative": "备选策略",
        "long_term": "长期建议",
    }
    _URGENCY_CN = {"high": "高", "medium": "中", "low": "低"}
    _URGENCY_COLOR = {"high": "#C62828", "medium": "#DD5B00", "low": "#666666"}
    _PRIORITY_ORDER = ("recommended", "alternative", "long_term")

    def _ai_strategy_section(self, strategy_items, references) -> list:
        header = self._section_header("四", "推荐策略")
        if not strategy_items:
            return [
                Spacer(1, 0.5 * cm),
                KeepTogether([
                    header,
                    Spacer(1, 0.25 * cm),
                    Paragraph(
                        "本次评估未生成策略建议，可结合「风险排查」中的改进建议制定跟进计划。",
                        self.styles["BodyCN"],
                    ),
                ]),
            ]

        elements = [Spacer(1, 0.5 * cm)]
        header_pending = True
        for priority in self._PRIORITY_ORDER:
            group = [i for i in strategy_items if (i.get("priority") or "recommended") == priority]
            if not group:
                continue
            mark = "●" if priority == "recommended" else "○" if priority == "alternative" else "·"
            title_p = Paragraph(
                f'<font color="{self.BRAND.hexval()}">{mark}</font> '
                f'<b>{self._GROUP_TITLES.get(priority, priority)}</b>',
                ParagraphStyle(f"Grp_{priority}", fontName=self.FONT_NAME, fontSize=12.5,
                              leading=19, textColor=self.INK, spaceBefore=10, spaceAfter=5),
            )
            # 章节标题 / 分组标题与首张卡片绑定，卡片本身不跨页拆分
            lead = [header, Spacer(1, 0.25 * cm)] if header_pending else []
            header_pending = False
            elements.append(KeepTogether(
                lead + [title_p, self._strategy_card(group[0]), Spacer(1, 0.28 * cm)]
            ))
            for item in group[1:]:
                elements.append(KeepTogether([self._strategy_card(item), Spacer(1, 0.28 * cm)]))

        if references:
            ref_lines = []
            for i, ref in enumerate(references, 1):
                title = ref.get("title") or ref.get("item_title") or "未命名知识"
                category = ref.get("category") or ""
                cat_txt = f"（{category}）" if category else ""
                ref_lines.append(f"{i}. 《{title}》{cat_txt}")
            elements.append(Spacer(1, 0.3 * cm))
            elements.append(KeepTogether([
                Paragraph(
                    f'<font color="{self.BRAND.hexval()}">▍</font> <b>参考依据</b>',
                    ParagraphStyle("RefTitle", fontName=self.FONT_NAME, fontSize=12,
                                  leading=18, textColor=self.INK),
                ),
                Paragraph(
                    "<br/>".join(ref_lines),
                    ParagraphStyle("RefBody", fontName=self.FONT_NAME, fontSize=9.5,
                                  leading=15, textColor=self.INK_2),
                ),
            ]))
        return elements

    @staticmethod
    def _external_text(text: str) -> str:
        """对外口径：策略字段中的内部实现措辞替换为中性表述。"""
        return (
            (text or "")
            .replace("规则引擎（LLM 不可用时的兜底建议）", "内置评估规则")
            .replace("规则引擎", "评估规则")
        )

    def _strategy_card(self, item: dict):
        urgency = item.get("urgency") or "medium"
        urg_cn = self._URGENCY_CN.get(urgency, "中")
        urg_hex = self._URGENCY_COLOR.get(urgency, "#DD5B00")
        title = self._external_text(item.get("title")) or "（未命名策略）"
        rows = [
            Paragraph(
                f'<b>{title}</b>'
                f'<font color="#94a3b8">　</font>'
                f'<font color="{urg_hex}" size="9">紧急度：{urg_cn}</font>',
                ParagraphStyle("StrTitle", fontName=self.FONT_NAME, fontSize=11,
                              leading=16, textColor=self.INK),
            ),
        ]
        for label, key in (("原因", "reason"), ("行动", "action"), ("预期成效", "expected_outcome")):
            val = self._external_text(item.get(key) or "")
            if val:
                rows.append(Paragraph(
                    f'<font color="#666666">{label}：</font>{val}',
                    ParagraphStyle(f"Str_{key}", fontName=self.FONT_NAME, fontSize=9.5,
                                  leading=15, textColor=self.INK_2),
                ))
        ref = self._external_text(item.get("reference") or "")
        if ref:
            rows.append(Paragraph(
                f'<font color="#666666">参考：{ref}</font>',
                ParagraphStyle("StrRef", fontName=self.FONT_NAME, fontSize=9,
                              leading=14, textColor=self.MUTED),
            ))
        card = Table([[r] for r in rows], colWidths=[16.4 * cm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), white),
            ("BOX", (0, 0), (-1, -1), 1, self.BORDER),
            ("ROUNDEDCORNERS", [8, 8, 8, 8]),
            ("LINEBEFORE", (0, 0), (0, -1), 3, HexColor(urg_hex)),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return card


class _TrendMixin:
    """客情评分趋势：matplotlib 曲线 + 等级带/参考线 + 趋势摘要。"""

    def _trend_section_pdf(self, trend: AssessmentTrendResponse | None) -> list:
        header = self._section_header("五", "客情评分趋势")
        if not trend or len(trend.points) < 2:
            return [
                Spacer(1, 0.5 * cm),
                KeepTogether([
                    header,
                    Spacer(1, 0.25 * cm),
                    Paragraph(
                        "当前仅有一次评估记录，暂无可对比的趋势曲线。持续评估后将自动生成历史趋势。",
                        self.styles["BodyCN"],
                    ),
                ]),
            ]
        content: list = []
        try:
            content.append(self._render_trend_chart(trend))
        except Exception:
            content.append(Paragraph("趋势曲线生成失败，已略去图示。", self.styles["SmallCN"]))
        trend_cn = {"up": "↑ 上升", "down": "↓ 下降", "flat": "→ 持平"}
        delta_txt = f"{trend.delta:+.1f}" if trend.previous_score is not None else "—"
        content.append(Spacer(1, 0.2 * cm))
        content.append(Paragraph(
            f'趋势：<b>{trend_cn.get(trend.trend, trend.trend)}</b>（较上次 {delta_txt} 分）',
            self.styles["BodyCN"],
        ))
        if trend.level_lines:
            lines = " / ".join(
                f"{lv.name} {lv.min_score:g}分" for lv in trend.level_lines if lv.min_score > 0
            )
            if lines:
                content.append(Paragraph(
                    f'<font color="#666666">等级参考线：{lines}</font>',
                    self.styles["SmallCN"],
                ))
        # 标题 + 曲线 + 摘要整体不跨页
        return [Spacer(1, 0.5 * cm), KeepTogether([header, Spacer(1, 0.25 * cm)] + content)]

    def _render_trend_chart(self, trend: AssessmentTrendResponse):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # 中文字体（Windows 雅黑 / Linux 文泉驿 / macOS 苹方）
        for fname in ("Microsoft YaHei", "SimHei", "WenQuanYi Micro Hei", "PingFang SC", "Noto Sans CJK SC"):
            try:
                fm.findfont(fname, fallback_to_default=False)
                plt.rcParams["font.sans-serif"] = [fname, "DejaVu Sans"]
                break
            except Exception:
                continue
        plt.rcParams["axes.unicode_minus"] = False

        xs = [p.label for p in trend.points]
        ys = [p.total_score for p in trend.points]
        fig, ax = plt.subplots(figsize=(8.6, 3.4), dpi=160)
        # 等级背景带（来自评分配置，浅色区分区间）
        for name, lo, hi, hexc in self._levels():
            if hi <= 0 or lo >= 100:
                continue
            ax.axhspan(lo, hi, color=hexc, alpha=0.05)
        # reportlab 的 hexval() 返回 0xRRGGBB，matplotlib 只认 #RRGGBB
        brand_hex = f"#{self.BRAND.hexval()[2:]}"
        ax.plot(range(len(ys)), ys, marker="o", color=brand_hex, linewidth=2.2,
                markersize=4.5, markerfacecolor="white", markeredgecolor=brand_hex, markeredgewidth=1.4)
        ax.fill_between(range(len(ys)), ys, min(ys) - 2, color=brand_hex, alpha=0.07)
        ax.set_xticks(range(len(xs)))
        ax.set_xticklabels(xs, fontsize=8.5)
        ax.set_ylim(0, max(100, getattr(trend, "max_score", 100) or 100))
        ax.set_ylabel("客情评分", fontsize=10)
        ax.set_title("客情评分历史趋势", fontsize=12, color="#1A1A1A")
        ax.grid(True, axis="y", linestyle=":", alpha=0.5, color="#D4D4D4")
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color("#E5E5E5")
        for lv in trend.level_lines:
            try:
                yv = float(lv.min_score)
            except Exception:
                continue
            if yv <= 0:
                continue
            ax.axhline(yv, color=(lv.color or "#94a3b8"), linestyle="--", linewidth=0.9, alpha=0.7)
        # 每个数据点标注数值
        for i, v in enumerate(ys):
            ax.annotate(
                f"{v:.1f}",
                (i, v),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=8.5,
                color="#333333",
            )
        last_x, last_y = len(ys) - 1, ys[-1]
        ax.plot([last_x], [last_y], "o", color=brand_hex, markersize=9,
                markerfacecolor="white", markeredgecolor=brand_hex, markeredgewidth=2.2)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return RLImage(buf, width=16 * cm, height=6.2 * cm)
