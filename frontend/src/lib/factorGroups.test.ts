import { describe, expect, it } from "vitest";
import { groupFactors } from "./factorGroups";

describe("groupFactors", () => {
  it("按 sub_dimension 分组并保持因子顺序", () => {
    const items: Array<{ field: string; sub_dimension?: string }> = [
      { field: "a", sub_dimension: "决策链覆盖度" },
      { field: "b", sub_dimension: "信息互通" },
      { field: "c", sub_dimension: "决策链覆盖度" },
    ];
    const groups = groupFactors(items);
    expect(groups.map((g) => g.name)).toEqual(["决策链覆盖度", "信息互通"]);
    expect(groups[0].factors.map((f) => f.field)).toEqual(["a", "c"]);
  });

  it("无 sub_dimension 的因子归入“其他”", () => {
    const groups = groupFactors([{ field: "x" }]);
    expect(groups).toHaveLength(1);
    expect(groups[0].name).toBe("其他");
  });

  it("空列表返回空数组", () => {
    expect(groupFactors([])).toEqual([]);
  });
});
