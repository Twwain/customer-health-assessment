import { useEffect, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { getCustomer, createCustomer, updateCustomer, Customer } from "../api";
import CustomerForm from "../components/CustomerForm";

export default function CustomerDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const isNew = id === "new";
  const [customer, setCustomer] = useState<Partial<Customer>>({});
  const [original, setOriginal] = useState<Customer | null>(null);
  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(isNew);
  const [error, setError] = useState<string | null>(null);

  const fetchData = () => {
    if (isNew) return;
    setLoading(true);
    setError(null);
    getCustomer(Number(id))
      .then((r) => {
        setCustomer(r.data);
        setOriginal(r.data);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchData(); }, [id, isNew]);

  const handleSave = async () => {
    if (!customer.customer_name) {
      alert("请填写客户名称");
      return;
    }
    setSaving(true);
    try {
      if (isNew) {
        const r = await createCustomer(customer);
        navigate(`/customers/${r.data.id}`, { replace: true });
      } else {
        await updateCustomer(Number(id), customer);
        setEditing(false);
        // Refresh
        const r = await getCustomer(Number(id));
        setCustomer(r.data);
        setOriginal(r.data);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "保存失败";
      alert(msg);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div className="text-slate-400 py-20 text-center">加载中...</div>;
  }

  if (error) {
    return (
      <div className="py-20 text-center">
        <div className="text-4xl mb-4">⚠️</div>
        <div className="text-red-600 font-medium mb-2">客户数据加载失败</div>
        <div className="text-slate-400 text-sm mb-4">{error}</div>
        <div className="flex justify-center gap-3">
          <button onClick={fetchData} className="px-4 py-2 bg-amber-600 text-white rounded-xl text-sm hover:bg-amber-700 transition">重试</button>
          <Link to="/customers" className="px-4 py-2 border border-slate-300 text-slate-600 rounded-xl text-sm hover:bg-slate-50 transition">返回列表</Link>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-6">
        <div>
          <Link to="/customers" className="text-sm text-slate-400 hover:text-slate-600 mb-1 inline-block transition-colors">
            &larr; 返回列表
          </Link>
          <h1 className="text-2xl font-bold text-slate-800">
            {isNew ? "新增客户" : customer.customer_name}
          </h1>
        </div>
        <div className="flex gap-2">
          {!isNew && !editing && (
            <>
              <Link
                to={`/assessment/${id}`}
                className="px-4 py-2 bg-amber-600 text-white rounded-xl text-sm font-medium hover:bg-amber-700 transition"
              >
                生成评估报告
              </Link>
              <button
                onClick={() => setEditing(true)}
                className="px-4 py-2 bg-amber-500 text-white rounded-xl text-sm font-medium hover:bg-amber-600 transition"
              >
                编辑
              </button>
            </>
          )}
          {editing && (
            <>
              <button
                onClick={() => {
                  if (isNew) navigate("/customers");
                  else { setEditing(false); setCustomer(original!); }
                }}
                className="px-4 py-2 border border-slate-300 text-slate-600 rounded-xl text-sm font-medium hover:bg-slate-50 transition"
              >
                取消
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-4 py-2 bg-amber-600 text-white rounded-xl text-sm font-medium hover:bg-amber-700 transition disabled:opacity-50"
              >
                {saving ? "保存中..." : "保存"}
              </button>
            </>
          )}
        </div>
      </div>

      {/* Form */}
      <div className="bg-white rounded-2xl p-6 shadow-sm border border-slate-100">
        <CustomerForm
          data={customer}
          onChange={setCustomer}
          readOnly={!editing}
        />
      </div>

      {/* View mode: show assessment button too */}
      {!isNew && !editing && original && (
        <div className="mt-6 bg-white rounded-2xl p-6 shadow-sm border border-slate-100">
          <h2 className="text-lg font-semibold text-slate-800 mb-3">更多操作</h2>
          <div className="flex flex-wrap gap-3">
            <Link
              to={`/assessment/${id}`}
              className="px-4 py-2 bg-amber-600 text-white rounded-xl text-sm font-medium hover:bg-amber-700 transition"
            >
              📊 查看健康度评估
            </Link>
            <a
              href={`/api/assessment/${id}/pdf`}
              className="px-4 py-2 bg-slate-700 text-white rounded-xl text-sm font-medium hover:bg-slate-800 transition"
            >
              📄 下载 PDF 报告
            </a>
          </div>
        </div>
      )}
    </div>
  );
}
