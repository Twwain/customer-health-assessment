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
| AI 对话 | 大模型兼容适配器（对话 / 向量化模型均可配置）；自包含轻量状态机编排 Agent Loop（检索→推理→自批判→精炼） |
| 知识库 RAG | 解析（md/txt/csv 零依赖；pdf/xlsx/docx lazy import）→ 中文分句 + 滑动窗口切片 → 向量库（生产 Chroma 持久化 / InMemory 仅显式开发测试）→ MetadataReranker（默认）+ BGE Rerank（可选） |
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
- 前端依赖统一使用 **npm** 管理（仓库仅保留 `frontend/package-lock.json`）。需先 `cd frontend && npm ci`，再 `npm run dev`（或经 `start.sh` 自动拉起）。
- 配置 LLM：复制 `.env.example`（仓库根目录）为 `backend/.env` 并填入 Key（不填则自动降级；Docker 由 `docker-compose.yml` 的 `env_file: ./backend/.env` 注入，无需文件）。

### Docker 部署

```bash
# 本地构建并启动（默认 80 端口）
docker compose up -d --build

# 访问 http://localhost （后端同时托管前端静态资源）
```

- 生产依赖（chromadb / pymupdf / python-docx）由 `backend/requirements-prod.txt` 安装。`KNOWLEDGE_VECTOR_STORE=chroma` 时 Chroma 缺失或不可用会让 readiness 与向量读写返回失败，但知识列表、详情和原文下载仍可只读使用；内存向量库仅允许通过 `KNOWLEDGE_VECTOR_STORE=memory` 显式用于开发或测试。Docker 镜像默认不安装 FlagEmbedding，以避免引入 PyTorch/CUDA 巨型依赖，重排使用 MetadataReranker。
- 通过 `docker-compose.yml` 的 `env_file: ./backend/.env` 注入 LLM Key 等配置。

### 云服务器部署

```bash
# 修改 deploy.sh 中的 SERVER 地址后一键部署
bash deploy.sh
```

服务器需安装 Docker，安全组开放 80 端口。

## 配置

| 文件 | 作用 |
|------|------|
| `backend/.env` | LLM Key / 向量库 / 重排等运行配置（见 `.env.example`） |
| `backend/scoring_config.yaml` | 评分维度 / 因子 / 权重 / 打分规则 / 预警规则 / 等级阈值 |
| `backend/prompt_templates.yaml` | 场景化 Prompt 模板（free_qa / assessment / strategy / alert_analysis / session_title）+ 安全护栏 |
| `backend/data/knowledge/` | 预置知识（如 `customer_health_methodology.md` 评估方法论） |

关键环境变量：`LLM_ENABLED` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_EMBEDDING_*` / `LLM_TOOLS_ENABLED`（函数调用工具开关，默认开）/ `LLM_CHAT_THINKING_ENABLED`（前端对话思考模式，默认关）/ `LLM_REPORT_THINKING_ENABLED`（PDF AI 策略思考模式，默认开）/ `KNOWLEDGE_VECTOR_STORE`（chroma|memory）/ `RERANKER`（metadata|bge）/ `RAG_TOP_K` / `RAG_WINDOW`（命中切片窗口扩展，默认 1）/ `CHAT_TREND_POINTS` / `DB_PATH`。

对话资源边界可通过 `CHAT_TURN_TIMEOUT_SECONDS`、`CHAT_TURN_TOKEN_BUDGET`、
`CHAT_TOOL_CALLS_PER_ROUND`、`CHAT_TOOL_CALLS_PER_TURN` 和
`CHAT_TOOL_RESULT_CHARS_PER_TURN` 和 `CHAT_AGENT_CANCEL_GRACE_SECONDS` 调整；运行指标默认保留 90 天
（`CHAT_METRICS_RETENTION_DAYS`），可通过 `GET /api/chat/metrics` 查看分布。

`CHAT_TURN_TOKEN_BUDGET` 与 `REPORT_TURN_TOKEN_BUDGET` 是探索软预算，累计输入和输出 token。
预算达到上限后停止模型探索，当前已请求的工具仍受工具次数/字符预算限制；随后允许一次不带工具的最终回答，
独立使用该场景的单次生成额度（聊天为模板与 `LLM_MAX_TOKENS` 的较小值，报告为 `LLM_REPORT_MAX_TOKENS`），
不再压缩到剩余 token。收尾仍遵守总时限、取消及全局并发限制，实际超额用量正常记录。
收尾后事实核对明确提示未完成，不再继续修订；若核对本身耗尽预算，即使普通修订次数用完，仍允许唯一一次最终回答。

评估、策略和风险分析默认“生成一次 → 独立核对关键事实 → 有问题时最多修订一次”。
核对会比对原文中的动作、时限、责任角色、评分与引用依据，结果记录在
`timings.verification`，核对调用单独记为 `phase=verification`；核对 JSON 不作为回答推流。
核对失败、超时或证据无法定位时提示“未完成核对”，修订后仍有问题时保留提示，不继续循环。
`CHAT_AGENT_MAX_ITERATIONS` / `REPORT_AGENT_MAX_ITERATIONS` 默认 2、最大 2（含首稿）；
设为 1 可关闭修订，但仍执行核对。`AI_FACT_CHECK_TIMEOUT_SECONDS` 默认 30 秒，
`AI_FACT_CHECK_MAX_TOKENS` 默认 2000，正文超过 `AI_FACT_CHECK_MAX_DRAFT_CHARS`（默认 24000）
时明确标记未核对。核对使用非思考模式，不改变正文生成的思考配置。

PDF 与交互对话的生成预算分开：报告默认开启思考，`LLM_REPORT_REASONING_EFFORT=high`
（可选 low/high/max），`LLM_REPORT_MAX_TOKENS=32768` 为单次思考加正文的生成上限，
不再受聊天模板的 8000 上限截断。`REPORT_TURN_TOKEN_BUDGET=65536` 约束整份报告模型调用，
`REPORT_GENERATION_TIMEOUT_SECONDS=300` 覆盖生成、核对和最多一次修订；
配置 `PDF_JOB_TTL` 时应大于该时限并留出 PDF 排版时间。300 秒是最终保护边界，不是预期等待时间。
报告不因首字慢自动关闭思考；因生成 token 耗尽返回空正文时停止盲目重试，并明确提示配置问题。
降级或未完成核对的提示同时进入任务状态和 PDF，页面也会显示生成提示。

## 评分模型

V3.0 使用 7 个维度、28 个因子。每个因子先由原子指标映射为 0-10 分，
维度内取算术平均，再按维度权重折算并汇总为 0-100 分：

`总分 = Σ(维度内因子均分 × 维度权重 ÷ 10)`

| 维度 | 权重 | 因子数 | 数据来源 |
| --- | --- | --- | --- |
| KCR 关键客户关系 | 30 | 6 | AR 为主 |
| ER 普遍客户关系 | 18 | 4 | AR+SR+FR 协同 |
| OR 组织客户关系 | 14 | 3 | AR+管理层 |
| CI 客户洞察与业务理解 | 9 | 3 | AR+SR |
| HIS 历史合作与经营结果 | 12 | 4 | 系统自动抓取 |
| RISK 竞争态势与风险信号 | 12 | 5 | AR+FR+系统 |
| SVC 服务与支持健康度 | 5 | 3 | FR+系统 |

等级：健康 ≥80 · 亚健康 60-79 · 风险 40-59 · 高危 <40。

批量录入使用 `GET /api/customers/import-template` 下载的 `.xlsx` 文件；系统读取其中
“客户数据表”页，兼容 V3.0 的两行表头（第 1 行填写提示、第 2 行因子编号/名称）。
新增因子 = 3 步：① 加客户列或 `custom_fields` ② 在 YAML 注册字段 + 权重 + `input` + `rule` ③ 重启（前端因子表单由 `GET /api/customers/factor-config` 动态渲染）。

## AI 对话与知识库

- 对话流式输出（SSE），场景化 Prompt 注入量化评估 + 趋势 + 知识上下文；策略以 ```` ```json ```` 结构化块输出，前端 `StrategyItem` 直接渲染。
- Agent Loop 自批判/精炼；模型可主动调用工具补充信息：客户横向对比（`customer_compare`）与知识库补充检索（`knowledge_search`），输出含知识溯源（📎 可定位原文切片）。工具由 `LLM_TOOLS_ENABLED` 控制，不支持的网关会自动去掉 tools 重试。策略消息支持 ⭐ 采纳标记（当前为前端本地标记，入库沉淀待实现）。
- 工具结果在进入模型前统一执行 DLP 并标记为不可信数据；客户横向对比只返回匿名评分事实，不返回其他客户 ID 或名称。重新生成成功后以 `superseded_by_id` 原子切换，旧回复保留用于审计但不再进入对话历史。
- 知识检索链路：metadata 过滤 + 分类权重 → dense 向量召回 → Rerank 重排；命中切片默认扩展相邻 ±1 切片（`RAG_WINDOW`，可关）缓解跨切片截断；结构化指标（行业基准等精确数值）走 SQLite 精确查询，评估时按客户行业注入。SQLite 是向量 ID 与 metadata 的最终权威，Chroma metadata 写入失败会进入持久化 outbox 并在启动对账时重试。

## 报告与预警

- `GET /api/assessment/{id}/pdf` 导出 PDF（含 `?include_ai=true` 默认整合 AI 建议）。报告含：综合评分 / 分维度明细 / 风险提示与改进建议 / **AI 智能策略建议（三层分组 + 知识溯源）** / **健康分趋势图**。
- 预警趋势箭头基于最近 2 次评分差值；曲线基于 `AssessmentHistory` 全量记录。
- V3.0 的 7 条自动预警均已配置化；跨季度规则按每季度最后一次快照判断，连续两个季度下降且累计降幅严格大于 10 分时触发。
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

## 测试

```bash
cd backend && python -m pytest tests/ -v
```

覆盖：评分配置化、评估历史与趋势、LLM 适配与 SSE 对话、RAG 检索、Agent Loop（检索→推理→自批判→精炼）、报告整合（AI 解析 / 离线降级 / 趋势图）。前端以 `npm run build` + `npm run lint` + `npm run test`（vitest）验证。

## 部署要点

- **依赖分层**：`requirements.txt`（核心，Python 3.10+ 可直接装，3.12 验证通过）；`requirements-prod.txt`（chromadb / pymupdf / python-docx，Docker 安装）。
- **向量库**：`KNOWLEDGE_VECTOR_STORE=chroma` 需安装 chromadb；缺失或异常时向量读写与 readiness 失败关闭，知识列表、详情和原文下载仍可用。`memory` 仅能通过配置显式启用。Embedding provider、网关、模型或维度任一变更都会进入指纹隔离的影子 collection；整库构建和校验成功后才切换，期间 readiness 返回 503。metadata outbox 和 tombstone 默认每 5 秒自动重试，失败时指数退避并在日志/readiness 告警；完整 SQLite/Chroma ID 对账默认每 5 分钟在后台执行，readiness 不再每次扫描全库。
- **中文 PDF 字体**：Docker 装 `fonts-wqy-microhei`；开发环境自动探测系统 CJK 字体（Windows 雅黑 / macOS PingFang / Linux 文泉驿）。
- **历史索引回收**：新索引完成切换并与 SQLite 对账一致后回收同一知识库的旧 collection。清理失败会在 readiness 告警并由后台重试；删除文档覆盖迁移前后的所有版本，任一副本删除失败会保留 tombstone。同步重索引的整体失败返回 503 和原因，逐文档部分成功仍返回成功数与失败列表。
- **数据持久化**：挂载 `./data:/app/data`，数据库与向量库落盘。
- **访问边界（风险接受）**：系统不提供登录、RBAC 或租户隔离，客户、会话和知识数据在所有访问者之间共享；全部知识管理接口（下载、编辑、删除、审核、批量状态、重索引和指标管理）保持开放，仅通过项目管理约定供项目组成员使用。该约定不是技术访问控制，因此部署内容只能使用虚构或预先脱敏、可公开、可修改、可删除、可重置的数据；不得录入真实或敏感数据，并应将 `.env`、`AGENTS.md`、`dev_process/` 等敏感文件排除出发布内容。若未来需要对外开放、录入真实数据或区分访问者，必须重新实施认证、授权及租户/工作区隔离。
- **资源护栏**：Chat LLM 默认最多同时执行 8 个模型 HTTP 调用，Embedding 默认 1，AI PDF 任务默认 1；每客户端 IP 每分钟最多 60 次 AI 重型请求。知识上传/解析全局并发默认 1，ASGI 入口在 multipart 解析前限制请求体约 21MB，文件本身限制 20MB，并限制 50 万抽取字符、500 个切片、20 万预计 Embedding token、PDF 200 页、Office 解压后 100MB/2000 个 ZIP 条目。具体值可用 `.env.example` 中的配置覆盖；多 Uvicorn worker/多副本部署时并发和限流额度会按进程倍增，模型供应商侧仍需设置费用告警和余额/费用上限。

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
