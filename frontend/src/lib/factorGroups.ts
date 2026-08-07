export interface SubGroup<T> {
  name: string;
  factors: T[];
}

/** 把因子按后端下发的 sub_dimension 分组（无标注的归入“其他”）。 */
export function groupFactors<T>(factors: T[]): SubGroup<T>[] {
  const map = new Map<string, T[]>();
  for (const f of factors) {
    const name = (f as { sub_dimension?: string }).sub_dimension || "其他";
    const arr = map.get(name) ?? [];
    arr.push(f);
    map.set(name, arr);
  }
  return [...map.entries()].map(([name, items]) => ({ name, factors: items }));
}
