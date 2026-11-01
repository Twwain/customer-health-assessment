// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { answerFrames, hasPlaybackText, playAnswer } from "./answerPlayback";
import { assessmentSections } from "./assessmentPresentation";
import { answerPlainText, plainTextBlock } from "./plainText";

const value = "业务判断需要逐步展示。<script>不会执行</script>\n### 不是真正标题 😀".repeat(3);
const content = '本次按以下任务回答：综合评估\n### 总览\n事实\n## AI 分析（业务判断，可能有误）\n### 总体评估\n:::ai-text ' + JSON.stringify(value);

describe("已验证答案逐步呈现", () => {
  it("无报告标题的自由问答也逐段展示，所有中间帧保持安全文本封装", () => {
    const direct = ':::ai-text ' + JSON.stringify(value) + '\n:::ai-text "补充事实。"';
    expect(hasPlaybackText(direct)).toBe(true);
    expect(hasPlaybackText(':::ai-text broken')).toBe(false);
    const frames = [...answerFrames(direct)];
    expect(frames.length).toBeGreaterThan(3);
    expect(frames.at(-1)).toBe(direct);
    let previousLength = 0;
    for (const frame of frames) {
      for (const line of frame.split('\n')) expect(plainTextBlock(line)).not.toBeNull();
      const text = answerPlainText(frame);
      expect(text.length).toBeGreaterThanOrEqual(previousLength);
      expect(text).not.toContain('�');
      previousLength = text.length;
    }
    expect(answerPlainText(frames[0]).length).toBeLessThan(value.length);
  });

  it("中间帧可解码且内容逐步增长，最终原文完整保留", () => {
    const frames = [...answerFrames(content)];
    expect(frames.length).toBeGreaterThan(3);
    expect(frames.at(-1)).toBe(content);
    for (const frame of frames) {
      const sections = assessmentSections(frame);
      expect(sections).not.toBeNull();
      expect(sections![0].title).toBe("总体评估");
      expect(frame).not.toContain("�");
    }
    expect(frames[0].length).toBeLessThan(content.length);
    expect([...answerFrames("普通增量")]).toEqual(["普通增量"]);
  });

  it("逐帧让出绘制时间，离开会话后取消剩余展示", async () => {
    vi.useFakeTimers();
    try {
      const show = vi.fn();
      let active = true;
      const playing = playAnswer(content, show, () => active);
      expect(show).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(20);
      expect(show).toHaveBeenCalledTimes(2);
      active = false;
      await vi.runAllTimersAsync();
      await playing;
      expect(show).toHaveBeenCalledTimes(2);
    } finally { vi.useRealTimers(); }
  });
});
