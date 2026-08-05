# 客情评估智能体

> 基于客情因子自动评分，融合 RAG 知识库与 AI 策略建议，输出可溯源的客情分析报告。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/Node.js-18%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-Internal-blue)](./README.md#license)

## 目录

- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
  - [环境要求](#环境要求)
  - [本地开发](#本地开发)
  - [Docker 部署](#docker-部署)
  - [云服务器部署](#云服务器部署)
- [配置](#配置)
- [评分模型](#评分模型)
- [AI 对话与知识库](#ai-对话与知识库)
- [报告与预警](#报告与预警)
- [项目结构](#项目结构)
- [API 概览](#api-概览)
- [测试](#测试)
- [部署要点](#部署要点)
- [降级与可用性](#降级与可用性)
- [设计决策](#设计决策)
- [许可证](#许可证)

## 功能特性

- **客户库（默认首页 `/customers`）** — 客户列表 + 统计/筛选 + 因子编辑（桌面可编辑、移动只读）+ 历史趋势抽屉 + 新建/导入；对每行实时补全分数、等级、趋势与预警。
- **AI 对话（`/chat`）** — SSE 流式多轮对话；四个场景：综合评估 / 生成策略 / 风险排查 / 自由问答；策略建议分推荐/备选/长期三层并带知识溯源；消息可点赞/点踩、采纳、复制；健康卡内「✨ AI 一键解读预警」直接触发预警解读（即"风险排查"场景）。
- **知识库（`/knowledge`）** — 知识条目浏览 / 语义检索 / 上传文档（md/txt/csv/pdf/xlsx/docx）/ 审核（proposed→canonical）/ 元数据编辑 / 删除 / 重索引；知识分层（结构化指标走 SQLite，文档向量走向量库）。
- **评分引擎配置化** — 维度 / 因子 / 权重 / 规则 / 预警 / 等级阈值全部在 `backend/scoring_config.yaml`，改配置重启即生效，无需改代码。
- **报告与预警** — PDF 报告整合 AI 动态策略建议 + 知识溯源 + 健康分趋势图（matplotlib）；预警带趋势箭头（↑↓→）+ 迷你 sparkline + AI 一键解读。
- **LLM 降级** — LLM 不可用 / 超时 / 限流时自动降级为规则引擎回复（横幅 + 量化事实 + 三层策略），基础功能不受影响。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python FastAPI + SQLAlchemy + SQLite + Uvicorn |
| 评分 | 配置驱动引擎（`scoring_config.yaml`，维度/因子/权重/阈值见下文「评分模型」） |
| AI 对话 | OpenAI 兼容适配器（DeepSeek-V4-Flash 对话 / 智谱 GLM embedding-3 向量化）；自包含轻量状态机编排 Agent Loop（检索→推理→自批判→精炼） |
| 知识库 RAG | 解析（md/txt/csv 零依赖；pdf/xlsx/docx lazy import）→ 中文分句 + 滑动窗口切片 → 向量库（Chroma 可选 / InMemory 零依赖回退）→ MetadataReranker（默认）+ BGE Rerank（可选） |
| PDF | ReportLab + matplotlib（趋势曲线） |
| 前端 | React 19 + TypeScript + Vite 8 + TailwindCSS v4 + React Router v7；图表为手写 SVG（未用 Recharts，1:1 还原冻结原型） |
| 部署 | Docker 多阶段构建 + docker-compose；可选依赖与核心依赖分离 |

> **设计取向（详见文末「设计决策」）**：Agent Loop 与 RAG 检索均为自包含轻量实现（语义等价于 LangGraph / LlamaIndex），依赖更轻、更易测试。

## 快速开始

### 环境要求

- **Python** ≥ 3.10（开发验证环境为 3.12 / 3.14）
- **Node.js** ≥ 18（Vite 8 构建要求）
- **Docker**（可选，用于容器化部署）

### 本地开发

```bash
# 后端（默认 8000）+ 前端（默认 5173，/api 代理到后端）
bash start.sh        # Linux / Git Bash
# 或 Windows 双击 start.bat
```

- 首次运行自动创建 SQLite 数据库并 seed 演示数据（13 条示例客户）。
- 后端依赖安装：`pip install -r backend/requirements.txt`。
- 前端需先 `cd frontend && npm install`，再 `npm run dev`（或经 `start.sh` 自动拉起）。
- 配置 LLM：复制 `.env.example`（仓库根目录）为 `backend/.env` 并填入 Key（不填则自动降级；Docker 由 `docker-compose.yml` 的 `env_file: .env` 注入，无需文件）。

### Docker 部署

```bash
# 本地构建并启动（默认 80 端口）
docker compose up -d --build

# 访问 http://localhost （后端同时托管前端静态资源）
```

- 生产可选依赖（chromadb / pymupdf / python-docx / FlagEmbedding）由 `backend/requirements-prod.txt` 安装；任一缺失时镜像仍正常构建并回退内存实现。
- 通过 `docker-compose.yml` 的 `env_file: .env` 注入 LLM Key 等配置。

### 云服务器部署

```bash
# 修改 deploy.sh 中的 SERVER 地址后一键部署
bash deploy.sh
```

服务器需安装 Docker，安全组开放 80 端口。

## 配置

| 文件 | 作用 |
|------|------|
| `.env` | LLM Key / 向量库 / 重排等运行配置（见 `.env.example`） |
| `backend/scoring_config.yaml` | 评分维度 / 因子 / 权重 / 打分规则 / 预警规则 / 等级阈值 |
| `backend/prompt_templates.yaml` | 场景化 Prompt 模板（free_qa / assessment / strategy / alert_analysis / session_title）+ 安全护栏 |
| `backend/data/knowledge/` | 预置知识（如 `customer_health_methodology.md` 评估方法论） |

关键环境变量：`LLM_ENABLED` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_EMBEDDING_*` / `LLM_TOOLS_ENABLED`（函数调用工具开关，默认开）/ `KNOWLEDGE_VECTOR_STORE`（chroma|memory）/ `RERANKER`（metadata|bge）/ `RAG_TOP_K` / `RAG_WINDOW`（命中切片窗口扩展，默认 1）/ `CHAT_TREND_POINTS` / `DB_PATH`。

## 评分模型

4 个维度各 25 分，满分 100 分：关系深度 / 客户满意度 / 商业价值 / 风险水平。
等级：优秀 ≥85 · 良好 70-84 · 一般 55-69 · 风险 <55。

新增因子 = 3 步：① 加客户列或 `custom_fields` ② 在 YAML 注册字段 + 权重 + `input` + `rule` ③ 重启（前端因子表单由 `GET /api/customers/factor-config` 动态渲染）。

## AI 对话与知识库

- 对话流式输出（SSE），场景化 Prompt 注入量化评估 + 趋势 + 知识上下文；策略以 ```` ```json ```` 结构化块输出，前端 `StrategyItem` 直接渲染。
- Agent Loop 自批判/精炼；模型可主动调用工具补充信息：客户横向对比（`customer_compare`）与知识库补充检索（`knowledge_search`），输出含知识溯源（📎 可定位原文切片）。工具由 `LLM_TOOLS_ENABLED` 控制，不支持的网关会自动去掉 tools 重试。策略消息支持 ⭐ 采纳标记（当前为前端本地标记，入库沉淀待实现）。
- 知识检索链路：metadata 过滤 + 分类权重 → dense（智谱 embedding-3）向量召回 → Rerank 重排；命中切片默认扩展相邻 ±1 切片（`RAG_WINDOW`，可关）缓解跨切片截断；结构化指标（行业基准等精确数值）走 SQLite 精确查询，评估时按客户行业注入。

## 报告与预警

- `GET /api/assessment/{id}/pdf` 导出 PDF（含 `?include_ai=true` 默认整合 AI 建议）。报告含：综合评分 / 分维度明细 / 风险提示与改进建议 / **AI 智能策略建议（三层分组 + 知识溯源）** / **健康分趋势图**。
- 预警趋势箭头基于最近 2 次评分差值；曲线基于 `AssessmentHistory` 全量记录。
- LLM 不可用 / 生成异常时，报告内 AI 章节降级为规则引擎建议并标注，保证 PDF 一定能导出。

## 项目结构

```
backend/
  main.py                 # FastAPI 入口 + 生产态托管前端静态资源
  models.py / database.py / schemas.py / config.py
  scoring_config.yaml     # 评分配置（维度/因子/权重/规则/预警）
  seed_data.py            # 演示数据
  services/
    scoring/              # 配置驱动评分引擎（config_loader / rules / config_driven / rule_based）
    assessment_history.py # 评估历史快照 + 趋势构造
    pdf_report.py         # PDF 报告（含 AI 策略章节 + 趋势图）
    report_builder.py     # 报告数据聚合（AI 策略 + 知识溯源 + 趋势，含降级）
    ai/                   # LLM 适配 + Prompt 模板 + 护栏 + 上下文 + 策略 + 降级 + 对话编排 + Agent Loop
    rag/                  # 解析 / 切片 / 向量化 / 向量库 / 重排 / 检索 / 知识库服务 / 结构化指标
  routers/                # customers / assessment / chat / knowledge
  tests/                  # pytest（评估/评分/对话/RAG/报告）
frontend/
  src/
    pages/                # CustomerList / Chat / Knowledge（仅 3 页）
    components/           # Layout / Sidebar / CustomerForm / HealthCard / Charts / Badges / StrategyList
    lib/ui.tsx            # 等级/色彩/趋势/预警映射 + 轻量 Markdown 渲染
    api/                  # API 调用封装（含 SSE 流式）
Dockerfile / docker-compose.yml / deploy.sh / start.sh / start.bat / .env.example
```

## API 概览

所有接口以 `/api` 为前缀。完整交互式文档见运行后的 `GET /api/docs`（Swagger UI）。

### 客户（`/api/customers`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/customers` | 客户列表（不含分数/等级/趋势，需逐行再请求评估） |
| GET | `/api/customers/industries` | 行业枚举 |
| GET | `/api/customers/factor-config` | 因子表单动态配置 |
| GET | `/api/customers/{id}` | 客户详情 |
| POST | `/api/customers` | 新建客户 |
| PUT | `/api/customers/{id}` | 更新客户基础信息 |
| PUT | `/api/customers/{id}/factors` | 编辑评分因子 |
| DELETE | `/api/customers/{id}` | 删除客户 |
| POST | `/api/customers/import` | 批量导入（模板驱动，自动计分） |

### 评估（`/api/assessment`、`/api/customers`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/assessment/{id}` | 当前评估（分数/等级/维度/预警/趋势） |
| POST | `/api/assessment/{id}/snapshot` | 写入评估历史快照 |
| GET | `/api/assessment/{id}/pdf` | 导出 PDF 报告（`?include_ai=true`） |
| GET | `/api/assessment/all/overview` | 全量概览统计 |
| GET | `/api/customers/{id}/assessment-history` | 评估历史记录 |
| GET | `/api/customers/{id}/assessment-trend` | 健康分趋势数据 |

### AI 对话（`/api/chat`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/sessions` | 创建会话 |
| GET | `/api/chat/sessions` | 会话列表 |
| GET | `/api/chat/sessions/{id}` | 会话详情 |
| DELETE | `/api/chat/sessions/{id}` | 删除会话 |
| POST | `/api/chat/sessions/{id}/messages` | 发送消息（SSE 流式） |
| POST | `/api/chat/sessions/{id}/evaluate` | 快捷评估（AI 综合评估结论） |
| POST | `/api/chat/sessions/{id}/strategy` | 生成策略建议 |
| POST | `/api/chat/sessions/{id}/alert-analysis` | 预警解读 |
| POST | `/api/chat/sessions/{id}/regenerate` | 重新生成 |
| POST | `/api/chat/messages/{id}/feedback` | 消息反馈（赞/踩） |
| GET | `/api/chat/status` | LLM 可用性状态 |

### 知识库（`/api/knowledge`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/knowledge/items` | 知识条目列表 |
| GET | `/api/knowledge/items/{id}` | 条目详情 |
| POST | `/api/knowledge/search` | 语义检索 |
| POST | `/api/knowledge/upload` | 上传文档 |
| PUT | `/api/knowledge/items/{id}` | 编辑元数据 |
| DELETE | `/api/knowledge/items/{id}` | 删除条目 |
| POST | `/api/knowledge/items/{id}/approve` | 审核（proposed→canonical） |
| POST | `/api/knowledge/reindex` | 重新索引 |
| GET | `/api/knowledge/metrics` | 结构化指标列表 |
| POST | `/api/knowledge/metrics` | 新增结构化指标 |
| DELETE | `/api/knowledge/metrics/{id}` | 删除指标 |
| GET | `/api/knowledge/status` | 知识库状态 |

## 测试

```bash
cd backend && python -m pytest tests/ -v
```

覆盖：评分配置化、评估历史与趋势、LLM 适配与 SSE 对话、RAG 检索、Agent Loop（检索→推理→自批判→精炼）、报告整合（AI 解析 / 离线降级 / 趋势图）。前端以 `npm run build` + `npm run lint` 验证。

## 部署要点

- **依赖分层**：`requirements.txt`（核心，Python 3.10+ 可直接装，3.12 验证通过）；`requirements-prod.txt`（chromadb / pymupdf / python-docx / FlagEmbedding，Docker 安装，缺失自动回退）。
- **向量库**：`KNOWLEDGE_VECTOR_STORE=chroma` 需安装 chromadb；未装自动回退 `memory`（基础功能可用）。
- **中文 PDF 字体**：Docker 装 `fonts-wqy-microhei`；开发环境自动探测系统 CJK 字体（Windows 微软雅黑 / macOS PingFang / Linux 文泉驿）。
- **数据持久化**：挂载 `./data:/app/data`，数据库与向量库落盘。

## 降级与可用性

LLM 故障（无 Key / 网络 / 限流 / 重试耗尽）→ 规则引擎降级回复；流式中途故障保留已输出文本 + `warning`；未预期异常完全降级。`GET /api/chat/status` 暴露可用性，前端据此置灰 AI 入口。Embedding 不可用 / 无命中 / 异常时检索静默降级，对话不崩溃。

## 设计决策

| 常见方案 | 本项目实现 | 理由 |
|----------|------------|------|
| LangGraph Agent Loop | 自包含轻量状态机（retrieve→reason→critique→refine） | LangGraph 为重量级依赖、不利于轻量自包含；等价语义、更易测、依赖更轻；可平滑替换 |
| LlamaIndex + Chroma | 自包含检索管线：Chroma（可选）+ InMemory 零依赖回退；MetadataReranker 默认 | 功能等价、不引入 LlamaIndex 抽象层；生产装 chromadb 即生效 |
| Recharts 图表 | 手写 SVG | 与冻结原型 1:1 还原、避免主题漂移、依赖更轻 |
| 规则自定义 UI（预警） | 仅内置规则 + AI 解读 | 当前聚焦内置规则 + AI 解读，规则引擎保持简单 |

## 许可证

本项目为**内部评估工具**，保留所有权利，不对外开放授权。如需在组织内使用或二次开发，请联系项目维护者。
