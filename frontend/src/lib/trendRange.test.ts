import { describe, expect, it } from "vitest";
import { computeTrendYRange } from "./trendRange";

describe("computeTrendYRange", () => {
  it("空数组返回默认 0-100 范围", () => {
    const r = computeTrendYRange([]);
    expect(r).toEqual({ yMin: 0, yMax: 100, ySpan: 100 });
  });

  it("普通波动数据按 18% 外扩", () => {
    const r = computeTrendYRange([40, 60]);
    expect(r.yMin).toBeCloseTo(36.4);
    expect(r.yMax).toBeCloseTo(63.6);
    expect(r.ySpan).toBeCloseTo(27.2);
  });

  it("全等值数据以中点为轴补足最小高度并居中", () => {
    const r = computeTrendYRange([50, 50, 50]);
    expect(r.yMin).toBeCloseTo(46);
    expect(r.yMax).toBeCloseTo(54);
  });

  it("接近 100 时钳制在 0-100 内", () => {
    const r = computeTrendYRange([98, 98]);
    expect(r.yMax).toBeLessThanOrEqual(100);
    expect(r.yMax).toBeGreaterThan(97);
  });

  it("接近 0 时钳制在 0-100 内", () => {
    const r = computeTrendYRange([0, 0]);
    expect(r.yMin).toBe(0);
    expect(r.yMax).toBeLessThanOrEqual(8);
  });

  it("单点数据与多点数据行为一致", () => {
    const single = computeTrendYRange([70]);
    const multi = computeTrendYRange([70, 70]);
    expect(single.yMin).toBeCloseTo(multi.yMin);
    expect(single.yMax).toBeCloseTo(multi.yMax);
  });
});
