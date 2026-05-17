import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { getAssessment, Assessment, getPdfUrl } from "../api";
import ScoreGauge from "../components/ScoreGauge";

export default function AssessmentView() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<Assessment | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getAssessment(Number(id))
      .then((r) => setData(r.data))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) {
    return <div className="text-slate-400 py-20 text-center">评估计算中...</div>;
  }

  if (!data) {
    return (
      <div className="py-20 text-center">
        <div className="text-slate-400">评估数据获取失败</div>
        <Link to="/customers" className="text-amber-600 hover:underline mt-2 inline-block">
          返回列表
        </Link>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <Link to={`/customers/${id}`} className="text-sm text-slate-400 hover:text-slate-600 mb-1 inline-block transition-colors">
            &larr; {data.customer_name}
          </Link>
          <h1 className="text-2xl font-bold text-slate-800">健康度评估报告</h1>
        </div>
        <a
          href={getPdfUrl(Number(id))}
          className="px-4 py-2 bg-slate-700 text-white rounded-xl text-sm font-medium hover:bg-slate-800 transition flex items-center gap-2"
        >
          <span>📄</span> 下载 PDF
        </a>
      </div>

      {/* Score overview */}
      <div className="mb-6">
        <ScoreGauge score={data.total_score} level={data.level} />
      </div>

      {/* Dimension detail */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
        {data.dimensions.map((d) => (
          <div key={d.name} className="bg-white rounded-2xl p-5 shadow-sm border border-slate-100">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-semibold text-slate-800">{d.name}</h3>
              <span className="text-lg font-bold text-amber-600">
                {d.score.toFixed(1)}<span className="text-sm text-slate-400 font-normal"> 分</span>
              </span>
            </div>
            <ul className="space-y-1">
              {d.details.map((detail, i) => (
                <li key={i} className="text-sm text-slate-600 leading-relaxed">{detail}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {/* Risk & Suggestions */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {data.risk_alerts.length > 0 && (
          <div className="bg-red-50 border border-red-200 rounded-2xl p-5">
            <h3 className="font-semibold text-red-800 mb-3">⚠️ 风险提示</h3>
            <ul className="space-y-2">
              {data.risk_alerts.map((alert, i) => (
                <li key={i} className="text-sm text-red-700">{alert}</li>
              ))}
            </ul>
          </div>
        )}
        {data.suggestions.length > 0 && (
          <div className="bg-lime-50 border border-lime-200 rounded-2xl p-5">
            <h3 className="font-semibold text-lime-800 mb-3">💡 改进建议</h3>
            <ul className="space-y-2">
              {data.suggestions.map((s, i) => (
                <li key={i} className="text-sm text-lime-700">✓ {s}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* 评分模型说明 */}
      <div className="mt-6 bg-slate-50 border border-slate-200 rounded-2xl p-5">
        <h3 className="font-semibold text-slate-600 mb-2 text-sm">评分模型说明</h3>
        <p className="text-xs text-slate-400 leading-relaxed mb-1">
          综合健康分由 4 个维度各 25 分加权求和得出（满分 100 分）：
        </p>
        <p className="text-xs text-slate-400 leading-relaxed">
          关系深度（合作年限 + 联系频率 + 最近联系时间）、
          客户满意度（1-10 分 × 2.5）、
          商业价值（合同金额 + 回款状态）、
          风险水平（基础 25 分 − 风险扣分 + 增长潜力加分）。
        </p>
      </div>

      <div className="mt-6 text-xs text-slate-400 text-center">
        评估时间：{new Date(data.assessed_at).toLocaleString("zh-CN")}
      </div>
    </div>
  );
}
