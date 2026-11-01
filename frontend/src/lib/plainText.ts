// Server-owned transport for free text within an otherwise Markdown answer.
// Decoded values must remain text nodes, never be parsed as Markdown or HTML.
export function plainTextBlock(line: string): string | null {
  if (!line.startsWith(":::ai-text ")) return null;
  try {
    const value: unknown = JSON.parse(line.slice(":::ai-text ".length));
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}

export function answerPlainText(content: string): string {
  return content.split("\n").map((line) => plainTextBlock(line) ?? line).join("\n");
}
