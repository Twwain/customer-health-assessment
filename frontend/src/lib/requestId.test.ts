import { afterEach, expect, it, vi } from "vitest";
import { createRequestId } from "./requestId";

afterEach(() => vi.unstubAllGlobals());

it("安全环境沿用浏览器 UUID", () => {
  const id = "b80c2cd6-7e98-4b19-bd88-934eae3397f3";
  vi.stubGlobal("crypto", { randomUUID: () => id });
  expect(createRequestId()).toBe(id);
});

it("HTTP 环境使用安全随机字节生成带版本和变体位的 UUID v4", () => {
  vi.stubGlobal("crypto", {
    getRandomValues: (bytes: Uint8Array) => {
      bytes.set([0, 1, 2, 3, 4, 5, 0xff, 7, 0xff, 9, 10, 11, 12, 13, 14, 15]);
      return bytes;
    },
  });
  expect(createRequestId()).toBe("00010203-0405-4f07-bf09-0a0b0c0d0e0f");
});
