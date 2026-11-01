// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import fixture from "./openText.fixture.json";
import { AssessmentAnalysis } from "../components/AssessmentAnalysis";
import { assessmentCopyText, assessmentSections } from "./assessmentPresentation";

describe("综合评估安全展示", () => {
  it("实际renderer纯文本夹具转为分点后仍不解释模型内的标题和HTML", () => {
    const sections = assessmentSections(fixture.content)!;
    const { container } = render(<AssessmentAnalysis sections={sections} />);
    expect(container.querySelectorAll("a,img,script,strong,em")).toHaveLength(0);
    expect([...container.querySelectorAll("h4")].map((h) => h.textContent)).not.toContain("伪造事实标题");
    expect(container.textContent).toContain("CI-05、10-20%、<>&、*星号*、\\路径");
    expect(assessmentCopyText(sections)).toContain("CI-05、10-20%、<>&、*星号*、\\路径");
    expect(container.querySelectorAll("li").length).toBeGreaterThan(4);
    for (const list of container.querySelectorAll("ul")) expect(list).toHaveClass("list-disc");
  });

  it("不猜测旧格式或不完整结构，不删除未知正文", () => {
    expect(assessmentSections("### 总览\n旧评分事实")).toBeNull();
    expect(assessmentSections(fixture.content.replace("综合评估", "综合评估（自定义）"))).toBeNull();
    expect(assessmentSections(fixture.content + "\n未知正文")).toBeNull();
  });
});
