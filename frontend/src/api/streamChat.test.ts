import { afterEach, expect, it, vi } from "vitest";
import { streamChat } from "./index";

afterEach(() => vi.unstubAllGlobals());

it("同一网络批次中的done等待正文展示完成，解析错误不会吞掉展示异常", async () => {
  const response = () => new Response(new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"text":"已验证全文"}\n\nevent: done\ndata: {}\n\n'));
    controller.close();
  } }));
  vi.stubGlobal("fetch", vi.fn(async () => response()));
  const events: string[] = [];
  let release!: () => void;
  const pause = new Promise<void>((resolve) => { release = resolve; });
  const playing = streamChat("/test", {}, async (event) => {
    events.push(event.type);
    if (event.type === "delta") await pause;
  });
  await vi.waitFor(() => expect(events).toEqual(["delta"]));
  release();
  await playing;
  expect(events).toEqual(["delta", "done"]);
  await expect(streamChat("/test", {}, async () => { throw new Error("展示失败"); })).rejects.toThrow("展示失败");
});
