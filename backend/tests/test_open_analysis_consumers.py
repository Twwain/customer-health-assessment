import io

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate
from reportlab.platypus import Paragraph, Table, KeepTogether

from services.pdf_report import PdfReportGenerator
from schemas import StrategyItem


def test_strategy_response_preserves_open_steps_on_refresh():
    item = StrategyItem(title="价值评审", steps=["确定口径", "提交评审"],
                        depends_on=["a:0"], deliverable="测算表")
    restored = StrategyItem.model_validate(item.model_dump())
    assert restored.steps == ["确定口径", "提交评审"]
    assert restored.depends_on == ["a:0"]
    assert restored.deliverable == "测算表"


def paragraphs(story):
    for item in story:
        if isinstance(item, Paragraph):
            yield item.getPlainText()
        elif isinstance(item, Table):
            for row in item._cellvalues:
                yield from paragraphs(row)
        elif isinstance(item, KeepTogether):
            yield from paragraphs(item._content)


def test_open_pdf_long_text_and_empty_actions_keep_references():
    gen = PdfReportGenerator()
    story = gen._analysis_section(
        {"summary": "判断 <待评审> & 条件", "risks": ["风险判断"], "strengths": ["机会判断"]},
        [{"statement": "尚待验证", "how_to_check": "评审确认"}],
    )
    story += gen._ai_strategy_section([], [{"title": "合成资料", "category": "制度", "snippet": "来源原文终点"}])
    output = io.BytesIO()
    text = "".join(paragraphs(story))
    for expected in ("判断 <待评审> & 条件", "尚待验证", "评审确认", "来源原文终点"):
        assert expected in text
    SimpleDocTemplate(output, pagesize=A4).build(story)
    assert output.getvalue().startswith(b"%PDF")


def test_long_term_card_can_span_pages_and_preserves_tail():
    gen = PdfReportGenerator()
    item = dict(priority="long_term", title="长期行动", urgency="medium", reason="条件 <不确定>",
                action="推进步骤和交付物。\n" * 350 + "最终交付物标记", expected_outcome="待客户评审", reference="")
    output = io.BytesIO()
    story = gen._ai_strategy_section([item], [])
    text = "".join(paragraphs(story))
    assert "长期行动" in text and "最终交付物标记" in text
    SimpleDocTemplate(output, pagesize=A4).build(story)
    assert output.getvalue().startswith(b"%PDF")
