import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { listCustomers, listIndustries, deleteCustomer, Customer, CustomerListResponse } from "../api";

export default function CustomerList() {
  const [searchParams] = useSearchParams();
  const [data, setData] = useState<CustomerListResponse | null>(null);
  const [industries, setIndustries] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const search = searchParams.get("search") ?? "";
  const industry = searchParams.get("industry") ?? "";
  const level = searchParams.get("level") ?? "";
  const page = parseInt(searchParams.get("page") ?? "1");

  useEffect(() => {
    listIndustries().then((r) => setIndustries(r.data)).catch(() => {});
  }, []);

  const fetchData = () => {
    setLoading(true);
    setError(null);
    listCustomers({ search, industry, level, page })
      .then((r) => setData(r.data))
      .catch((e) => setError(e instanceof Error ? e.message : "加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchData(); }, [search, industry, level, page]);

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`确定删除「${name}」吗？`)) return;
    await deleteCustomer(id);
    listCustomers({ search, industry, page })
      .then((r) => setData(r.data));
  };

  const buildUrl = (updates: Record<string, string>) => {
    const p = new URLSearchParams(searchParams);
    Object.entries(updates).forEach(([k, v]) => (v ? p.set(k, v) : p.delete(k)));
    return `/customers?${p.toString()}`;
  };

  return (
    <div>
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-6">
        <h1 className="text-2xl font-bold text-slate-800">客情列表</h1>
        <Link
          to="/customers/new"
          className="inline-flex items-center justify-center px-4 py-2 bg-amber-600 text-white rounded-xl text-sm font-medium hover:bg-amber-700 transition"
        >
          + 新增客户
        </Link>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 mb-4 flex flex-col sm:flex-row gap-3">
        <input
          type="text"
          defaultValue={search}
          placeholder="搜索客户名称、行业、对接人..."
          className="flex-1 px-3 py-2 border border-slate-300 rounded-xl text-sm outline-none focus:ring-2 focus:ring-amber-500 focus:border-amber-500"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              window.location.href = buildUrl({ search: (e.target as HTMLInputElement).value, page: "" });
            }
          }}
        />
        <select
          value={industry}
          onChange={(e) => {
            window.location.href = buildUrl({ industry: e.target.value, page: "" });
          }}
          className="px-3 py-2 border border-slate-300 rounded-xl text-sm outline-none focus:ring-2 focus:ring-amber-500"
        >
          <option value="">全部行业</option>
          {industries.map((ind) => (
            <option key={ind} value={ind}>{ind}</option>
          ))}
        </select>
      </div>

      {loading ? (
        <div className="text-slate-400 py-20 text-center">加载中...</div>
      ) : error ? (
        <div className="py-20 text-center">
          <div className="text-4xl mb-4">⚠️</div>
          <div className="text-red-600 font-medium mb-2">数据加载失败</div>
          <div className="text-slate-400 text-sm mb-4">{error}</div>
          <button onClick={fetchData} className="px-4 py-2 bg-amber-600 text-white rounded-xl text-sm hover:bg-amber-700 transition">重试</button>
        </div>
      ) : !data || data.items.length === 0 ? (
        <div className="py-20 text-center text-slate-400">
          {search || industry ? "没有匹配的客户" : "暂无客户数据"}
        </div>
      ) : (
        <>
          {/* Desktop table */}
          <div className="hidden md:block bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
            <table className="w-full">
              <thead className="bg-slate-50 border-b border-slate-100">
                <tr>
                  <TH>客户名称</TH>
                  <TH>行业</TH>
                  <TH>合作年限</TH>
                  <TH>满意度</TH>
                  <TH>回款情况</TH>
                  <TH>最近联系</TH>
                  <TH>操作</TH>
                </tr>
              </thead>
              <tbody>
                {data.items.map((c) => (
                  <tr key={c.id} className="border-b border-slate-50 hover:bg-amber-50/30 transition-colors">
                    <td className="px-4 py-3">
                      <Link to={`/customers/${c.id}`} className="text-amber-700 font-medium hover:underline">
                        {c.customer_name}
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-600">{c.industry || "-"}</td>
                    <td className="px-4 py-3 text-sm text-slate-600">{c.cooperation_years}年</td>
                    <td className="px-4 py-3 text-sm text-slate-600">{c.customer_satisfaction}/10</td>
                    <td className="px-4 py-3 text-sm">
                      <span className="inline-flex items-center gap-1.5">
                        <span className={`inline-block w-2 h-2 rounded-full ${c.payment_status === "正常" ? "bg-lime-500" : "bg-red-500"}`} />
                        <span className={c.payment_status === "正常" ? "text-lime-700" : "text-red-600"}>
                          {c.payment_status}
                        </span>
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-500">
                      {c.last_contact_date?.split("T")[0] ?? "无记录"}
                    </td>
                    <td className="px-4 py-3 text-sm space-x-2">
                      <Link to={`/customers/${c.id}`} className="text-amber-600 hover:underline font-medium">
                        查看
                      </Link>
                      <button
                        onClick={() => handleDelete(c.id, c.customer_name)}
                        className="text-red-500 hover:underline"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <div className="md:hidden space-y-3">
            {data.items.map((c) => (
              <Link
                key={c.id}
                to={`/customers/${c.id}`}
                className="block bg-white rounded-2xl p-4 shadow-sm border border-slate-100 hover:shadow transition-shadow"
              >
                <div className="flex justify-between items-start mb-2">
                  <span className="font-semibold text-slate-800">{c.customer_name}</span>
                  <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full ${
                    c.payment_status === "正常" ? "bg-lime-50 text-lime-700" : "bg-red-50 text-red-600"
                  }`}>
                    <span className={`inline-block w-1.5 h-1.5 rounded-full ${c.payment_status === "正常" ? "bg-lime-500" : "bg-red-500"}`} />
                    {c.payment_status}
                  </span>
                </div>
                <div className="text-xs text-slate-500 space-y-0.5">
                  <div>{c.industry || "未填写行业"} · 合作{c.cooperation_years}年</div>
                  <div>满意度 {c.customer_satisfaction}/10 · 最近联系 {c.last_contact_date?.split("T")[0] ?? "无"}</div>
                </div>
              </Link>
            ))}
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between mt-4 text-sm">
            <span className="text-slate-500">共 {data.total} 条</span>
            <div className="flex gap-2">
              <a
                href={buildUrl({ page: String(Math.max(1, page - 1)) })}
                className={`px-3 py-1.5 rounded-xl border text-sm font-medium transition ${
                  page <= 1
                    ? "text-slate-300 border-slate-200 cursor-not-allowed"
                    : "text-slate-600 border-slate-300 hover:bg-amber-50 hover:border-amber-300"
                }`}
              >
                上一页
              </a>
              <a
                href={buildUrl({ page: String(page + 1) })}
                className={`px-3 py-1.5 rounded-xl border text-sm font-medium transition ${
                  data.items.length < data.page_size
                    ? "text-slate-300 border-slate-200 cursor-not-allowed"
                    : "text-slate-600 border-slate-300 hover:bg-amber-50 hover:border-amber-300"
                }`}
              >
                下一页
              </a>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function TH({ children }: { children: React.ReactNode }) {
  return (
    <th className="px-4 py-3 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">
      {children}
    </th>
  );
}
