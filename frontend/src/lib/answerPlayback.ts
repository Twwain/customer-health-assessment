import { plainTextBlock } from "./plainText";

export function hasPlaybackText(content: string): boolean {
  return content.split("\n").some((line) => plainTextBlock(line) !== null);
}

// Playback starts only after the server publishes a validated answer. Each
// frame keeps the JSON text envelope intact; model text never becomes markup.
export function* answerFrames(content: string): Generator<string> {
  const lines = content.split("\n");
  const reportStart = lines.indexOf("## AI 分析（业务判断，可能有误）");
  const start = reportStart >= 0 ? reportStart : lines.findIndex((line) => plainTextBlock(line) !== null);
  if (start < 0) { yield content; return; }
  const size = Math.max(12, Math.ceil(lines.slice(start).reduce((n, line) =>
    n + Array.from(plainTextBlock(line) ?? "").length, 0) / 100));
  const prefix = lines.slice(0, start);
  for (const line of lines.slice(start)) {
    const plain = plainTextBlock(line);
    if (plain !== null) {
      const chars = Array.from(plain);
      for (let end = size; end < chars.length; end += size) {
        yield [...prefix, ":::ai-text " + JSON.stringify(chars.slice(0, end).join(""))].join("\n");
      }
      prefix.push(line);
      yield prefix.join("\n");
    } else prefix.push(line);
  }
  yield content;
}

export async function playAnswer(content: string, show: (text: string) => void, active: () => boolean) {
  for (const frame of answerFrames(content)) {
    if (!active()) return;
    show(frame);
    if (frame !== content) await new Promise<void>((resolve) => window.setTimeout(resolve, 20));
  }
}
