# 客情健康度系统 v2 设计

## 1. 后端：动态元数据

- `Customer` 模型新增 `custom_fields` 列（JSON, default `{}`）
- Pydantic schema 同步增加 `custom_fields: dict = {}`
- 导入时：`field_map` 中没有映射的列自动归入 `custom_fields`
- API 往返都包含此字段，前端自由渲染

## 2. 后端：评分策略模式

- 新建 `backend/services/scoring/` 包：
  - `base.py` — `ScoringStrategy` 抽象接口，`evaluate(c) -> AssessmentResponse`
  - `rule_based.py` — 当前规则引擎（作为默认实现）
  - `ml_placeholder.py` — 预留占位，抛 NotImplementedError
  - `factory.py` — 工厂函数，从配置读取策略名并创建实例
- `backend/config.py` — `SCORING_STRATEGY = "rule_based"`，后续改为 `"ml"` 即可切换
- 路由层只依赖接口，不感知具体实现

## 3. 前端：导入页面 Tab 化

- `/import` 页面用 Tab 组件：「文件导入」|「手动录入」
- 文件导入 tab 保留现有上传逻辑
- 手动录入 tab 嵌入 `CustomerForm` 组件，提交后提示成功/继续录入

## 4. 前端：全部页面重设计

使用 frontend-design skill 统一重构全部 5 个页面的视觉风格（仪表盘、客情列表、客户详情/编辑、导入、评估结果）。
