# 客情健康度评估系统

客户健康度评估与管理工具，支持客户数据导入、健康度评分、PDF 报告导出和 Docker 部署。

## 功能概览

- **仪表盘** — 客户总数、平均健康分、风险客户数、等级分布图、最近更新及风险客户快捷入口
- **客户管理** — 客户信息增删改查，支持 Excel 批量导入
- **健康度评估** — 4 维度评分（关系深度、客户满意度、商业价值、风险水平），满分 100 分
- **PDF 报告** — 综合评分、分维度明细、风险提示与改进建议，中文排版
- **Docker 部署** — 多阶段构建，一键部署到云服务器

## 技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python FastAPI + SQLAlchemy + SQLite |
| 前端 | React 19 + TypeScript + Vite + TailwindCSS v4 |
| PDF | ReportLab + matplotlib |
| 部署 | Docker + docker-compose |

## 快速开始

### 本地开发

```bash
# 启动后端 (8000) 和前端 (5173)
bash start.sh
```

首次运行会自动创建 SQLite 数据库。前端开发服务器已配置 `/api` 代理到后端。

### Docker 部署

```bash
# 本地构建并启动
docker compose up -d --build

# 访问 http://localhost:8000
```

### 部署到云服务器

```bash
# 修改 deploy.sh 中的 SERVER 地址
# 然后一键部署
bash deploy.sh
```

服务器需要安装 Docker，且安全组开放 80 端口。

## 评分模型

4 个维度各 25 分，满分 100 分：

| 维度 | 评估指标 |
|------|----------|
| 关系深度 | 合作年限 + 联系频率 + 最近联系时间 |
| 客户满意度 | 满意度评分（1-10）× 2.5 |
| 商业价值 | 合同金额 + 回款状态 |
| 风险水平 | 基础 25 分 − 风险扣分 + 增长潜力加分 |

等级划分：优秀 ≥85 · 良好 70-84 · 一般 55-69 · 风险 <55

## 项目结构

```
├── backend/
│   ├── main.py              # FastAPI 入口 + 静态文件服务
│   ├── models.py            # SQLAlchemy 数据模型
│   ├── database.py          # 数据库连接配置
│   ├── schemas.py           # Pydantic 请求/响应模型
│   ├── config.py            # 应用配置
│   ├── seed_data.py         # 示例数据初始化
│   ├── routers/
│   │   ├── assessment.py    # 评估与 PDF 导出接口
│   │   └── customers.py     # 客户 CRUD + 导入接口
│   └── services/
│       ├── pdf_report.py    # PDF 报告生成
│       ├── health_score.py  # 健康度评分服务
│       └── scoring/         # 评分引擎（策略模式）
├── frontend/
│   └── src/
│       ├── pages/           # Dashboard, CustomerList 等页面
│       ├── components/      # Layout, ScoreGauge 等组件
│       └── api/             # API 调用封装
├── Dockerfile
├── docker-compose.yml
├── deploy.sh                # 一键部署到云服务器
├── start.sh                 # 本地开发启动
└── start.bat                # Windows 本地开发启动
```

## 升级预留

### 客情因子

当前评分使用固定规则引擎（`scoring/rule_based.py`），已预留策略模式接口：

```python
# scoring/base.py — 评分策略抽象基类
class ScoringStrategy(ABC):
    def evaluate(self, customer: Customer) -> AssessmentResponse: ...
```

添加新评分因子只需实现该接口并在 `scoring/factory.py` 中注册即可，无需修改路由或前端。

### 评分模型升级

`scoring/ml_placeholder.py` 已预留机器学习模型插槽，可替换规则引擎：

```python
# scoring/ml_placeholder.py — ML 模型占位
class MLScoringStrategy(ScoringStrategy):
    def evaluate(self, customer: Customer) -> AssessmentResponse:
        # 此处接入模型推理
        pass
```

### 技术升级方向

- **数据库** — SQLite 可平滑升级为 PostgreSQL，仅需修改 `database.py` 连接字符串
- **评分引擎** — 策略模式支持热切换（环境变量或配置切换规则/ML 引擎）
- **客情因子** — 维度配置集中在 `config.py`，可扩展为动态配置面板
- **前端** — 组件已拆分为独立模块，新增评估维度或图表只需添加对应组件
