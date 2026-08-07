"""客情评估 PDF 报告生成器（reportlab）。

样式与前端 DESIGN.md 一致：白底 + 中性灰分层 + 品牌红 #E60012 小面积点缀。
等级颜色、维度明细、预警与建议全部来自评分配置与评估结果，不写死。

章节渲染按职责拆到 ``pdf_report_sections``（封面/评分/维度/风险/页脚）与
``pdf_report_strategy_trend``（策略/趋势），本模块保留生成编排与通用设施。
"""

from __future__ import annotations

import io
import os
import threading

import matplotlib.font_manager as fm

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus.flowables import HRFlowable

from schemas import AssessmentResponse, AssessmentTrendResponse

from services.pdf_report_sections import (
    _CoverMixin,
    _OverviewMixin,
    _DimensionMixin,
    _AlertMixin,
    _FooterMixin,
)
from services.pdf_report_strategy_trend import _StrategyMixin, _TrendMixin


class PdfReportGenerator(
    _CoverMixin,
    _OverviewMixin,
    _DimensionMixin,
    _AlertMixin,
    _StrategyMixin,
    _TrendMixin,
    _FooterMixin,
):
    FONT_NAME = "ChineseFont"

    # ── 设计 Token（与 frontend/src/index.css @theme 对齐）────────────────────
    BRAND = HexColor("#E60012")
    BRAND_DEEP = HexColor("#C5000F")
    BRAND_SOFT = HexColor("#FDEBEC")
    INK = HexColor("#1A1A1A")
    INK_2 = HexColor("#333333")
    MUTED = HexColor("#666666")
    BORDER = HexColor("#E5E5E5")
    BORDER_SOFT = HexColor("#EFEFEF")
    SURFACE_2 = HexColor("#F5F5F5")
    DANGER = HexColor("#C62828")
    DANGER_SOFT = HexColor("#FBE9E9")
    SUCCESS = HexColor("#1AAE39")
    SUCCESS_SOFT = HexColor("#E4F4E8")
    WARNING = HexColor("#DD5B00")
    WARNING_SOFT = HexColor("#FAEDE1")
    INFO = HexColor("#0075DE")
    INFO_SOFT = HexColor("#E4F0FB")

    # 兜底色板：仅在评分配置加载失败时使用（正常路径以 scoring_config.yaml 的 levels 为准）
    COLORS = {
        "健康": HexColor("#22c55e"),
        "亚健康": HexColor("#eab308"),
        "风险": HexColor("#f97316"),
        "高危": HexColor("#ef4444"),
    }
    # 兜底分段：与默认配置一致（80/60/40 三档线）
    _FALLBACK_LEVELS = [
        ("高危", 0, 39, "#ef4444"),
        ("风险", 40, 59, "#f97316"),
        ("亚健康", 60, 79, "#eab308"),
        ("健康", 80, 100, "#22c55e"),
    ]

    def __init__(self):
        self._register_fonts()
        self.styles = self._build_styles()
        # matplotlib 的 rcParams / 字体缓存与 savefig 非线程安全：
        # 同步端点与异步任务可能并发导出，统一串行化生成过程。
        self._gen_lock = threading.Lock()

    # ── 评分配置（scoring_config.yaml）─────────────────────────────────────

    @staticmethod
    def _levels() -> list[tuple[str, int, int, str]]:
        """从评分配置构建等级分段 [(name, lo, hi, color_hex)]，按分数升序。"""
        try:
            from services.scoring import load_scoring_config

            config = load_scoring_config()
            total = int(config.total_max_score)
            ordered = sorted(config.levels, key=lambda lv: lv.min_score)
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

    def _level_is_risky(self, a: AssessmentResponse) -> bool:
        """当前分数是否落入风险区间（如 风险/高危，即低于第二高等级下限）。"""
        try:
            from services.scoring import load_scoring_config

            config = load_scoring_config()
            ordered = sorted(config.levels, key=lambda lv: lv.min_score, reverse=True)
            if len(ordered) >= 2:
                return a.total_score < ordered[1].min_score
        except Exception:
            pass
        return a.total_score < 60

    @staticmethod
    def _model_description() -> tuple[str, str]:
        """评分模型说明文案（页脚用），从评分配置动态生成。"""
        try:
            from services.scoring import load_scoring_config

            config = load_scoring_config()
            dims = [d for d in config.dimensions if d.enabled]
            n = len(dims)
            total = config.total_max_score
            line1 = f"综合客情评分由 {n} 个维度加权求和得出（满分 {total:.0f} 分）："
            parts = [f"{d.name}（{d.max_score:.0f} 分）" for d in dims]
            return line1, "、".join(parts) + "。"
        except Exception:
            return (
                "综合客情评分由 7 个维度加权求和得出（满分 100 分）：",
                "关键客户关系（30分）、普遍客户关系（18分）、组织客户关系（14分）、"
                "客户洞察与业务理解（9分）、历史合作与经营结果（12分）、"
                "竞争态势与风险信号（12分）、服务与支持健康度（5分）。",
            )

    def _register_fonts(self):
        explicit = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/System/Library/Fonts/PingFang.ttc",
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
        cjk_kw = ["hei", "song", "ming", "yuan", "cjk", "noto", "yahei", "pingfang", "wqy"]
        for fp in fm.findSystemFonts():
            if any(kw in os.path.basename(fp).lower() for kw in cjk_kw):
                try:
                    pdfmetrics.registerFont(TTFont(self.FONT_NAME, fp))
                    return
                except Exception:
                    continue
        for base in ["C:/Windows/Fonts", "/System/Library/Fonts", "/usr/share/fonts"]:
            if not os.path.exists(base):
                continue
            for root, _, files in os.walk(base):
                for f in files:
                    if f.lower().endswith((".ttf", ".ttc", ".otf")):
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
        canvas.setStrokeColor(self.BRAND)
        canvas.setLineWidth(1.6)
        canvas.line(2 * cm, A4[1] - 1.1 * cm, A4[0] - 2 * cm, A4[1] - 1.1 * cm)
        canvas.setFont(self.FONT_NAME, 8)
        canvas.setFillColor(self.MUTED)
        canvas.drawString(2 * cm, 1.4 * cm, "客情评估智能体")
        canvas.setFillColor(self.BRAND)
        canvas.drawRightString(A4[0] - 2 * cm, 1.4 * cm, f"第 {doc.page - 1} 页")
        canvas.restoreState()

    def _build_styles(self):
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(
            "CoverBrand", fontName=self.FONT_NAME, fontSize=11, leading=16,
            alignment=TA_CENTER, textColor=self.MUTED, tracking=2,
        ))
        styles.add(ParagraphStyle(
            "CoverTitle", fontName=self.FONT_NAME, fontSize=30, leading=40,
            alignment=TA_CENTER, textColor=self.INK, spaceAfter=12,
        ))
        styles.add(ParagraphStyle(
            "CoverSubtitle", fontName=self.FONT_NAME, fontSize=12, leading=18,
            alignment=TA_CENTER, textColor=self.MUTED,
        ))
        styles.add(ParagraphStyle(
            "SectionTitle", fontName=self.FONT_NAME, fontSize=15, leading=22,
            spaceBefore=16, spaceAfter=10, textColor=self.INK,
        ))
        styles.add(ParagraphStyle(
            "BodyCN", fontName=self.FONT_NAME, fontSize=10.5, leading=17,
            spaceAfter=6, textColor=self.INK_2,
        ))
        styles.add(ParagraphStyle(
            "SmallCN", fontName=self.FONT_NAME, fontSize=8.5, leading=13,
            textColor=self.MUTED,
        ))
        styles.add(ParagraphStyle(
            "ScoreBig", fontName=self.FONT_NAME, fontSize=54, leading=62,
            alignment=TA_CENTER, textColor=self.INK,
        ))
        styles.add(ParagraphStyle(
            "CardTitle", fontName=self.FONT_NAME, fontSize=11.5, leading=17,
            textColor=self.INK,
        ))
        styles.add(ParagraphStyle(
            "CardScore", fontName=self.FONT_NAME, fontSize=14, leading=20,
            textColor=self.MUTED,
        ))
        styles.add(ParagraphStyle(
            "CardDetail", fontName=self.FONT_NAME, fontSize=9.5, leading=16,
            textColor=self.INK_2,
        ))
        styles.add(ParagraphStyle(
            "MetaLabel", fontName=self.FONT_NAME, fontSize=9.5, leading=15,
            textColor=self.MUTED,
        ))
        styles.add(ParagraphStyle(
            "MetaValue", fontName=self.FONT_NAME, fontSize=11, leading=16,
            textColor=self.INK,
        ))
        return styles

    # ── 通用小部件 ───────────────────────────────────────────────────────────

    @staticmethod
    def _section_header(number: str, title: str):
        """红条 + 标题的分节标题（与前端「红色细杠」一致）。"""
        return Table(
            [[
                Table([[""]], colWidths=[0.14 * cm], rowHeights=[0.62 * cm],
                      style=[("BACKGROUND", (0, 0), (0, 0), PdfReportGenerator.BRAND)]),
                Paragraph(
                    f'<font color="#1A1A1A"><b>{number}　{title}</b></font>',
                    ParagraphStyle(
                        "Sec", fontName=PdfReportGenerator.FONT_NAME, fontSize=15,
                        leading=22, textColor=PdfReportGenerator.INK,
                    ),
                ),
            ]],
            colWidths=[0.3 * cm, 16.4 * cm],
            hAlign="LEFT",
        )

    def generate(
        self,
        a: AssessmentResponse,
        *,
        industry: str = "",
        strategy_items: list[dict] | None = None,
        references: list[dict] | None = None,
        trend: AssessmentTrendResponse | None = None,
    ) -> bytes:
        with self._gen_lock:
            buf = io.BytesIO()
            doc = SimpleDocTemplate(
                buf, pagesize=A4,
                topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                onPage=self._on_page,
            )
            story = []
            story.extend(self._cover(a, industry=industry))
            story.append(PageBreak())
            story.extend(self._overview(a, trend))
            story.extend(self._dimension_detail(a))
            story.extend(self._alerts(a))
            story.extend(self._ai_strategy_section(strategy_items, references))
            story.extend(self._trend_section_pdf(trend))
            story.extend(self._footer())
            doc.build(story)
            return buf.getvalue()

    def _now_str(self):
        import datetime
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
