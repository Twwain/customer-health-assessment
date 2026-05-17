import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getOverview, Overview, CustomerHealthSummary } from "../api";

export default function Dashboard() {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getOverview()
      .then((r) => setData(r.data))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="text-slate-400 py-20 text-center">加载中...</div>;
  }

  if (!data || data.total_customers === 0) {
    return (
      <div className="py-20 text-center">
        <div className="text-6xl mb-5">📋</div>
        <h2 className="text-xl font-bold text-slate-800 mb-2">暂无客情数据</h2>
        <p className="text-slate-500 mb-6">请先添加客户或导入客情数据，开始评估健康度</p>
        <Link
          to="/import"
          className="inline-block px-6 py-2.5 bg-amber-600 text-white rounded-xl font-medium hover:bg-amber-700 transition"
        >
          开始导入
        </Link>
      </div>
    );
  }

  const dist = data.level_distribution;
  const levelItems = [
    { label: "优秀", count: dist["优秀"] ?? 0, color: "bg-lime-500" },
    { label: "良好", count: dist["良好"] ?? 0, color: "bg-sky-500" },
    { label: "一般", count: dist["一般"] ?? 0, color: "bg-amber-500" },
    { label: "风险", count: dist["风险"] ?? 0, color: "bg-red-500" },
  ];

  return (
    <div>
      <h1 className="text-2xl font-bold text-slate-800 mb-6">客情概览</h1>

      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
        <Card
          label="客户总数"
          value={data.total_customers}
          unit="家"
          color="text-slate-700"
          to="/customers"
        />
        <Card
          label="平均健康分"
          value={data.avg_score}
          unit="分"
          color={data.avg_score >= 70 ? "text-lime-600" : data.avg_score >= 55 ? "text-amber-600" : "text-red-500"}
        />
        <Card
          label="风险客户"
          value={data.risk_count}
          unit="家"
          color={data.risk_count > 0 ? "text-red-500" : "text-lime-600"}
          to="/customers?level=风险"
          span
        />
      </div>

      {/* Distribution */}
      <div className="bg-white rounded-2xl p-6 shadow-sm border border-slate-100">
        <h2 className="text-lg font-semibold text-slate-800 mb-5">健康等级分布</h2>
        <div className="space-y-4">
          {levelItems.map((item) => {
            const pct =
              data.total_customers > 0
                ? (item.count / data.total_customers) * 100
                : 0;
            return (
              <Link
                key={item.label}
                to={`/customers?level=${item.label}`}
                className="flex items-center gap-3 group"
              >
                <span className="w-12 text-sm text-slate-600">{item.label}</span>
                <div className="flex-1 bg-slate-100 rounded-full h-3 overflow-hidden">
                  <div
                    className={`h-full ${item.color} rounded-full transition-all duration-500`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <span className="w-8 text-sm font-medium text-slate-700 text-right group-hover:text-amber-600 transition-colors">
                  {item.count}
                </span>
              </Link>
            );
          })}
        </div>
      </div>

      {/* Recent + Risk customers panels */}
      {data.recent_customers.length > 0 || data.risk_customers.length > 0 ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-8">
          {/* 最近更新的客户 */}
          <div className="bg-white rounded-2xl p-6 shadow-sm border border-slate-100">
            <h2 className="text-lg font-semibold text-slate-800 mb-4 flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-sky-400" />
              最近更新的客户
            </h2>
            {data.recent_customers.length > 0 ? (
              <div className="space-y-2">
                {data.recent_customers.map((c) => (
                  <MiniCustomerCard key={c.customer_id} c={c} />
                ))}
              </div>
            ) : (
              <p className="text-sm text-slate-400">暂无数据</p>
            )}
          </div>

          {/* 风险客户 */}
          <div className="bg-white rounded-2xl p-6 shadow-sm border border-slate-100">
            <h2 className="text-lg font-semibold text-slate-800 mb-4 flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-red-400" />
              ⚠️ 风险客户
            </h2>
            {data.risk_customers.length > 0 ? (
              <div className="space-y-2">
                {data.risk_customers.map((c) => (
                  <MiniCustomerCard key={c.customer_id} c={c} />
                ))}
              </div>
            ) : (
              <p className="text-sm text-slate-400">暂无风险客户</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MiniCustomerCard({ c }: { c: CustomerHealthSummary }) {
  const levelStyles: Record<string, string> = {
    "优秀": "bg-lime-50 text-lime-700 border-lime-200",
    "良好": "bg-sky-50 text-sky-700 border-sky-200",
    "一般": "bg-amber-50 text-amber-700 border-amber-200",
    "风险": "bg-red-50 text-red-700 border-red-200",
  };
  const style = levelStyles[c.level] || levelStyles["一般"];
  return (
    <Link
      to={`/assessment/${c.customer_id}`}
      className="flex items-center gap-3 rounded-xl border border-slate-100 hover:shadow-sm hover:border-amber-200 transition bg-white group"
    >
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-slate-800 truncate group-hover:text-amber-700 transition-colors">
          {c.customer_name}
        </div>
        <div className="text-xs text-slate-400">{c.industry || "未填写行业"}</div>
      </div>
      <span className={`text-xs font-bold px-2.5 py-1 rounded-full shrink-0 ${style}`}>
        {c.total_score.toFixed(0)}分 {c.level}
      </span>
    </Link>
  );
}

function Card({
  label,
  value,
  unit,
  color,
  span,
  to,
}: {
  label: string;
  value: number;
  unit: string;
  color: string;
  span?: boolean;
  to?: string;
}) {
  const content = (
    <div
      className={`bg-white rounded-xl p-5 shadow-sm border border-slate-100 ${span ? "col-span-2 lg:col-span-1" : ""} ${to ? "hover:shadow-md hover:border-amber-200 transition cursor-pointer" : ""}`}
    >
      <div className="text-sm text-slate-500 mb-1">{label}</div>
      <div className={`text-3xl font-bold ${color}`}>
        {value}
        <span className="text-base font-normal text-slate-400 ml-1">{unit}</span>
      </div>
    </div>
  );
  return to ? <Link to={to} className="block">{content}</Link> : <>{content}</>;
}
