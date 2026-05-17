import { useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { importCustomers, createCustomer, Customer } from "../api";
import CustomerForm from "../components/CustomerForm";

type Tab = "file" | "manual";

export default function ImportData() {
  // 文件导入相关 state
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<{ created: number; errors: string[] } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Tab state
  const [tab, setTab] = useState<Tab>("file");

  // 手动录入 state
  const [formData, setFormData] = useState<Partial<Customer>>({});
  const [saving, setSaving] = useState(false);
  const navigate = useNavigate();

  const handleUpload = async () => {
    if (!file) return;
    setUploading(true);
    setResult(null);
    try {
      const r = await importCustomers(file);
      setResult(r.data);
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "导入失败";
      alert(msg);
    } finally {
      setUploading(false);
    }
  };

  const handleManualSubmit = async () => {
    if (!formData.customer_name) {
      alert("请填写客户名称");
      return;
    }
    setSaving(true);
    try {
      const r = await createCustomer(formData);
      alert(`客户「${r.data.customer_name}」创建成功`);
      setFormData({});
    } catch (e: unknown) {
      alert("创建失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-bold text-slate-800 mb-6">数据导入</h1>

      {/* Tab 切换 */}
      <div className="flex gap-6 mb-6 border-b border-slate-200">
        {(["file", "manual"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-1 py-3 text-sm font-medium transition border-b-2 -mb-px ${
              tab === t
                ? "border-amber-500 text-amber-700"
                : "border-transparent text-slate-500 hover:text-slate-700"
            }`}
          >
            {t === "file" ? "📁 文件导入" : "✏️ 手动录入"}
          </button>
        ))}
      </div>

      {/* 文件导入 Tab */}
      {tab === "file" && (
        <>
          {/* Template download */}
          <div className="bg-sky-50 border border-sky-200 rounded-2xl p-5 mb-6">
            <h3 className="font-semibold text-sky-800 mb-2">导入说明</h3>
            <ul className="text-sm text-sky-700 space-y-1 mb-4">
              <li>1. 下载模板，按格式填写客情数据</li>
              <li>2. 保存为 .xlsx 或 .csv 格式</li>
              <li>3. 上传文件，系统自动导入</li>
              <li>4. <b>客户名称</b>为必填字段</li>
            </ul>
            <button
              onClick={() => {
                const headers = [
                  "客户名称", "行业", "对接人", "联系电话", "合作年限",
                  "沟通频率", "最近联系日期", "客户满意度", "合同金额(万元)",
                  "回款情况", "风险信号", "竞品介入", "增长潜力", "备注",
                ];
                const row = [
                  "示例客户", "信息技术", "张三", "13800138000", "2.5",
                  "每月", "2026-05-01", "8", "100",
                  "正常", "", "否", "高", "",
                ];
                const csv = [headers, row].map((r) => r.join(",")).join("\n");
                const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = "客情数据导入模板.csv";
                a.click();
                URL.revokeObjectURL(url);
              }}
              className="px-4 py-2 bg-sky-600 text-white rounded-xl text-sm font-medium hover:bg-sky-700 transition"
            >
              下载导入模板
            </button>
          </div>

          {/* Upload area */}
          <div className="bg-white rounded-2xl p-8 shadow-sm border border-slate-100">
            <div
              className="border-2 border-dashed border-slate-300 rounded-2xl p-8 text-center hover:border-amber-400 transition cursor-pointer"
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const f = e.dataTransfer.files[0];
                if (f) setFile(f);
              }}
            >
              {file ? (
                <div>
                  <div className="text-3xl mb-2">📄</div>
                  <div className="text-slate-700 font-medium">{file.name}</div>
                  <div className="text-slate-400 text-sm">{(file.size / 1024).toFixed(1)} KB</div>
                </div>
              ) : (
                <div>
                  <div className="text-4xl mb-3">📁</div>
                  <div className="text-slate-600 font-medium mb-1">点击选择文件或拖拽到此处</div>
                  <div className="text-slate-400 text-sm">支持 .xlsx / .csv 格式</div>
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".xlsx,.xls,.csv"
                className="hidden"
                onChange={(e) => {
                  if (e.target.files?.[0]) setFile(e.target.files[0]);
                }}
              />
            </div>

            {file && (
              <div className="mt-4 flex justify-center">
                <button
                  onClick={handleUpload}
                  disabled={uploading}
                  className="px-6 py-2.5 bg-amber-600 text-white rounded-xl font-medium hover:bg-amber-700 transition disabled:opacity-50"
                >
                  {uploading ? "导入中..." : "开始导入"}
                </button>
              </div>
            )}

            {/* Result */}
            {result && (
              <div className={`mt-6 rounded-2xl p-4 ${result.errors.length > 0 ? "bg-amber-50 border border-amber-200" : "bg-lime-50 border border-lime-200"}`}>
                <div className="font-semibold text-lg mb-2">
                  {result.errors.length > 0 ? "⚠️" : "✅"} 成功导入 {result.created} 条记录
                </div>
                {result.errors.length > 0 && (
                  <div className="text-sm text-red-600">
                    <div className="font-medium mb-1">以下行导入失败：</div>
                    {result.errors.map((err, i) => (
                      <div key={i}>{err}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      )}

      {/* 手动录入 Tab */}
      {tab === "manual" && (
        <div className="bg-white rounded-2xl p-6 shadow-sm border border-slate-100">
          <CustomerForm data={formData} onChange={setFormData} />
          <div className="mt-6 flex gap-3">
            <button
              onClick={handleManualSubmit}
              disabled={saving}
              className="px-6 py-2.5 bg-amber-600 text-white rounded-xl font-medium hover:bg-amber-700 transition disabled:opacity-50"
            >
              {saving ? "保存中..." : "创建客户"}
            </button>
            <button
              onClick={() => setFormData({})}
              className="px-4 py-2 border border-slate-300 text-slate-600 rounded-xl text-sm font-medium hover:bg-slate-50 transition"
            >
              重置
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
