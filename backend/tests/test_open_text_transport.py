"""The checked-in synthetic wire fixture is shared with the real UI renderer."""
import json
from pathlib import Path

from services.ai.renderer import render


def rendered_fixture():
    value = 'CI-05、10-20%、<>&、*星号*、\\路径\n## 伪造事实标题\n[链接](https://invalid.example) <img src=x>\n:::ai-text "嵌套注入"'
    plan = dict(schema_version="2.4", summary_refs=[], detail_refs=[], risks=[],
                strength_refs=[], projections=[], actions=[], citation_refs=[],
                analysis=dict(summary=value, risks=[value], strengths=[value]),
                hypotheses=[dict(statement=value, how_to_check=value)],
                verification_questions=[value])
    package = dict(assessment={"alerts": []}, trend={}, retrieval_status="no_hits", evidence=[])
    result = render(plan, package, {"render_nodes": {}}, {"scenario": "evaluate", "intents": []})
    return {"original": value, "content": result["content"], "strategy_items": result["strategy_items"]}


FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/lib/openText.fixture.json"


def test_actual_renderer_matches_frontend_wire_fixture():
    assert rendered_fixture() == json.loads(FIXTURE.read_text(encoding="utf-8"))
    blocks = [json.loads(line.removeprefix(":::ai-text "))
              for line in rendered_fixture()["content"].splitlines()
              if line.startswith(":::ai-text ")]
    assert len(blocks) == 6
    assert blocks[0] == rendered_fixture()["original"]


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(rendered_fixture(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
