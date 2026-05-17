# 客情健康度系统 v2 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 动态元数据支持 + 评分策略模式 + 导入页手动录入 + 全页面 UI 重设计

**Architecture:** 后端新增 JSON 扩展字段和策略模式评分引擎包；前端导入页 Tab 化并复用 CustomerForm，5 个页面统一用 frontend-design skill 重设计

**Tech Stack:** Python FastAPI + SQLAlchemy + React 19 + TypeScript + TailwindCSS v4

---

### Task 1: 后端 — Customer 模型增加 custom_fields JSON 列

**Files:**
- Modify: `backend/models.py:11-27`
- Modify: `backend/schemas.py:5-48`

- [ ] **Step 1: 修改 models.py，添加 custom_fields 列**

在 `models.py` 第 26 行 `notes` 之后添加：

```python
custom_fields: Mapped[dict] = mapped_column(JSON, default=dict, comment="自定义扩展字段")
```

顶部确保导入 JSON 类型：

```python
from sqlalchemy import String, Integer, Float, Boolean, Text, Date, DateTime, JSON, func
```

- [ ] **Step 2: 修改 schemas.py，CustomerBase/CustomerUpdate 增加 custom_fields**

在 `CustomerBase` 类末尾（`notes` 之后）添加：

```python
custom_fields: dict = Field(default_factory=dict)
```

在 `CustomerUpdate` 类末尾（`notes` 之后）添加：

```python
custom_fields: dict | None = None
```

- [ ] **Step 3: 删除旧数据库并重启后端验证**

```bash
rm -f backend/customer_health.db
cd backend && python main.py
```

验证 API 返回的数据中包含 `custom_fields: {}`。

---

### Task 2: 后端 — 导入逻辑适配 custom_fields

**Files:**
- Modify: `backend/routers/customers.py:131-191`

- [ ] **Step 1: 修改 _process_rows，未知列归入 custom_fields**

修改 `_process_rows` 函数中的映射逻辑。将第 155 行 `data = {}` 之后的部分改为：

```python
data = {}
custom_fields = {}
for cn_key, en_key in field_map.items():
    val = row.get(cn_key, None)
    if val is None or val == "":
        continue
    # ... 保持现有类型转换逻辑 ...
    # (cooperation_years, customer_satisfaction, contract_amount, competitor_involvement, last_contact_date 处理不变)

# 不在 field_map 中的列归入 custom_fields
for key, val in row.items():
    if key not in field_map and val is not None and val != "":
        # 处理日期类型
        if isinstance(val, (datetime.datetime, datetime.date)):
            custom_fields[key] = val.isoformat() if isinstance(val, datetime.datetime) else str(val)
        else:
            custom_fields[key] = str(val).strip()

if custom_fields:
    data["custom_fields"] = custom_fields
```

注意：需要将 `data = {}` 移到 `custom_fields = {}` 之前，并且在创建 Customer 时保留原有逻辑。

- [ ] **Step 2: 更新 field_map 和 customer 创建逻辑**

在 `_process_rows` 中将 `data = {}` 替换为：

```python
data: dict = {}
```

在创建 `Customer(**data)` 之前，确保 `custom_fields` 的默认值存在（如果不传则 SQLAlchemy 会用 default=dict）:

实际上 `mapped_column(JSON, default=dict)` 已经保证了默认值，所以如果没有 unknown columns 就不需要传 `custom_fields`。

---

### Task 3: 后端 — 评分策略模式重构

**Files:**
- Create: `backend/config.py`
- Create: `backend/services/scoring/__init__.py`
- Create: `backend/services/scoring/base.py`
- Create: `backend/services/scoring/rule_based.py`
- Create: `backend/services/scoring/ml_placeholder.py`
- Create: `backend/services/scoring/factory.py`
- Modify: `backend/routers/assessment.py:10-24`

- [ ] **Step 1: 创建 backend/config.py**

```python
SCORING_STRATEGY = "rule_based"  # 后续改为 "ml" 切换模型
```

- [ ] **Step 2: 创建 backend/services/scoring/__init__.py**

```python
from .factory import get_scoring_strategy
```

- [ ] **Step 3: 创建 backend/services/scoring/base.py**

```python
from abc import ABC, abstractmethod
from models import Customer
from schemas import AssessmentResponse


class ScoringStrategy(ABC):
    @abstractmethod
    def evaluate(self, customer: Customer) -> AssessmentResponse:
        ...
```

- [ ] **Step 4: 创建 backend/services/scoring/rule_based.py**

将 `backend/services/health_score.py` 的全部逻辑迁移到此类，实现 `ScoringStrategy` 接口：

```python
import datetime
from .base import ScoringStrategy
from models import Customer
from schemas import AssessmentResponse, DimensionScore


class RuleBasedStrategy(ScoringStrategy):
    def evaluate(self, c: Customer) -> AssessmentResponse:
        # 原有 HealthScoreEngine.evaluate() 全部逻辑
        ...
```

（完整代码包含原有 4 个维度的评分方法 `_relationship_score`, `_satisfaction_score`, `_business_score`, `_risk_score`, `_level`）

- [ ] **Step 5: 创建 backend/services/scoring/ml_placeholder.py**

```python
from .base import ScoringStrategy
from models import Customer
from schemas import AssessmentResponse


class MLPlaceholderStrategy(ScoringStrategy):
    def evaluate(self, customer: Customer) -> AssessmentResponse:
        raise NotImplementedError(
            "ML 模型尚未接入，请在此实现模型调用逻辑。"
            "完成后将 backend/config.py 中的 SCORING_STRATEGY 改为 'ml' 即可启用。"
        )
```

- [ ] **Step 6: 创建 backend/services/scoring/factory.py**

```python
from backend.config import SCORING_STRATEGY
from .base import ScoringStrategy
from .rule_based import RuleBasedStrategy
from .ml_placeholder import MLPlaceholderStrategy


_strategies = {
    "rule_based": RuleBasedStrategy,
    "ml": MLPlaceholderStrategy,
}


def get_scoring_strategy() -> ScoringStrategy:
    cls = _strategies.get(SCORING_STRATEGY)
    if cls is None:
        raise ValueError(f"未知评分策略: {SCORING_STRATEGY}，可用: {list(_strategies.keys())}")
    return cls()
```

- [ ] **Step 7: 修改 backend/routers/assessment.py**

删除第 11 行 `engine = HealthScoreEngine()`，替换为：

```python
from services.scoring import get_scoring_strategy

engine = get_scoring_strategy()
```

- [ ] **Step 8: 可选 — 清理原 health_score.py**

原 `backend/services/health_score.py` 可以删除（逻辑已迁移到 `rule_based.py`），或保留作为参考。

---

### Task 4: 前端 — API 类型和 CustomerForm 适配 custom_fields

**Files:**
- Modify: `frontend/src/api/index.ts:5-23`
- Modify: `frontend/src/components/CustomerForm.tsx`

- [ ] **Step 1: 更新 api/index.ts Customer 接口**

在 `Customer` 接口的 `notes` 之后添加：

```typescript
custom_fields: Record<string, string>;
```

- [ ] **Step 2: 更新 CustomerForm 组件，渲染扩展字段**

在 `CustomerForm.tsx` 表单末尾（`备注` Field 之后）添加动态字段渲染块：

```tsx
{/* 自定义扩展字段 */}
{data.custom_fields && Object.keys(data.custom_fields).length > 0 && (
  <>
    <div className="md:col-span-2 mt-2">
      <h3 className="text-sm font-semibold text-slate-700 border-t border-slate-200 pt-4 pb-2">
        扩展字段
      </h3>
    </div>
    {Object.entries(data.custom_fields).map(([key, value]) => (
      <Field key={key} label={key}>
        <input
          type="text"
          value={value}
          onChange={(e) => {
            const newFields = { ...data.custom_fields, [key]: e.target.value };
            onChange({ ...data, custom_fields: newFields });
          }}
          disabled={readOnly}
          className={inputClass}
        />
      </Field>
    ))}
  </>
)}
```

---

### Task 5: 前端 — 导入页面增加手动录入 Tab

**Files:**
- Modify: `frontend/src/pages/ImportData.tsx`

- [ ] **Step 1: 重写 ImportData.tsx，添加 Tab 结构**

完整逻辑：页面顶部两个 Tab 按钮（文件导入 / 手动录入），文件导入 tab 保留现有内容，手动录入 tab 嵌入 CustomerForm 组件并支持提交。

关键代码结构：

```tsx
import { useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { importCustomers, createCustomer } from "../api";
import CustomerForm from "../components/CustomerForm";

type Tab = "file" | "manual";

export default function ImportData() {
  const [tab, setTab] = useState<Tab>("file");
  // ... 文件导入相关 state 不变 ...
  const [formData, setFormData] = useState<Partial<Customer>>({});
  const [saving, setSaving] = useState(false);
  const navigate = useNavigate();

  const handleManualSubmit = async () => {
    if (!formData.customer_name) { alert("请填写客户名称"); return; }
    setSaving(true);
    try {
      const r = await createCustomer(formData);
      alert(`客户「${r.data.customer_name}」创建成功`);
      setFormData({}); // 重置表单
    } catch { alert("创建失败"); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <h1 className="text-2xl font-bold text-slate-800 mb-6">数据导入</h1>
      {/* Tab 切换 */}
      <div className="flex gap-1 mb-6 bg-slate-100 rounded-lg p-1 w-fit">
        {(["file", "manual"] as Tab[]).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 rounded-md text-sm font-medium transition ${
              tab === t ? "bg-white text-slate-800 shadow-sm" : "text-slate-500 hover:text-slate-700"
            }`}>
            {t === "file" ? "📁 文件导入" : "✏️ 手动录入"}
          </button>
        ))}
      </div>

      {tab === "file" ? (
        /* 现有文件导入 UI 代码 */
      ) : (
        <div className="bg-white rounded-xl p-6 shadow-sm border border-slate-200">
          <CustomerForm data={formData} onChange={setFormData} />
          <div className="mt-6 flex gap-3">
            <button onClick={handleManualSubmit} disabled={saving}
              className="px-6 py-2.5 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50">
              {saving ? "保存中..." : "创建客户"}
            </button>
            <button onClick={() => setFormData({})}
              className="px-4 py-2 border border-slate-300 rounded-lg text-sm hover:bg-slate-50">
              重置
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
```

---

### Task 6: 前端 — 全部页面 UI 重设计

**Approach:** 使用 frontend-design skill 对每个页面进行视觉升级。先启动 dev 服务器查看当前效果，然后逐个页面重设计。

**涉及文件:**
- `frontend/src/components/Layout.tsx`
- `frontend/src/pages/Dashboard.tsx`
- `frontend/src/pages/CustomerList.tsx`
- `frontend/src/pages/CustomerDetail.tsx`
- `frontend/src/components/CustomerForm.tsx`
- `frontend/src/pages/ImportData.tsx`
- `frontend/src/pages/Assessment.tsx`
- `frontend/src/components/ScoreGauge.tsx`
- `frontend/src/components/HealthRadar.tsx`
- `frontend/src/index.css`

- [ ] **Step 1: 启动 dev 服务器查看当前 UI**

```bash
bash start.sh
```

- [ ] **Step 2: 使用 frontend-design skill 重设计每个页面**

依次对 Layout → Dashboard → CustomerList → CustomerDetail + CustomerForm → ImportData → Assessment + chart components 进行视觉重设计，保持现有功能不变。

- [ ] **Step 3: 验证全部功能**

确认所有页面功能正常，包括：搜索筛选、分页、CRUD、表单提交、评估计算、PDF 下载、导入。

---
