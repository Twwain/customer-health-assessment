# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start & Stop

```bash
bash start.sh   # Start backend (8000) + frontend (5173), Ctrl+C to stop
```

Or double-click `start.bat` on Windows.

## Project Overview

Customer health assessment system (客情健康度评估系统) — internal training project. Import customer data, view/edit records, generate health score (0-100 across 4 dimensions), and export PDF reports.

- **Backend**: Python FastAPI + SQLite + ReportLab PDF + matplotlib
- **Frontend**: React 19 + TypeScript + Vite 8 + TailwindCSS v4 + React Router v7 + Recharts

## Architecture

```
backend/          → FastAPI on port 8000
  routers/        → REST endpoints
  services/       → Business logic (scoring, PDF)
frontend/         → Vite dev server on port 5173, proxies /api → backend
  components/     → Shared UI (Layout, CustomerForm, ScoreGauge)
  pages/          → Route pages (Dashboard, CustomerList, CustomerDetail, ImportData, Assessment)
```

Frontend dev server proxies `/api` requests to `http://127.0.0.1:8000` (configured in `vite.config.ts`). No CORS issues in dev mode.

## Database

SQLite file at `backend/customer_health.db`, auto-created on first run. Models defined in `backend/models.py`. Schema changes: delete the db file and restart backend (tables auto-create via `Base.metadata.create_all`).

## Scoring Engine

`backend/services/health_score.py` — 4 dimensions × 25 points each = 100 total:
1. Relationship depth (cooperation years + contact frequency + recency)
2. Customer satisfaction (1-10 × 2.5)
3. Business value (contract amount + payment status)
4. Risk level (base 25 minus risk factors + growth potential bonus)

Levels: 优秀 ≥85 · 良好 70-84 · 一般 55-69 · 风险 <55

## PDF Generation

`backend/services/pdf_report.py` uses ReportLab. Chinese font auto-detected from Windows/macOS/Linux paths (TrueType-only, PostScript outlines are not supported). Docker uses WenQuanYi Micro Hei. Font registration order: Microsoft YaHei → SimHei → PingFang → WenQuanYi Micro Hei.

Content-Disposition header uses RFC 5987 encoding (`filename*=UTF-8''...`) because Chinese characters can't appear raw in HTTP headers.

## Key Dependencies

Backend: `fastapi`, `uvicorn`, `sqlalchemy`, `openpyxl`, `reportlab`, `matplotlib`
Frontend: `react-router-dom@7`, `recharts`, `axios`, `tailwindcss@4`

React Router v7 uses the legacy `<BrowserRouter>` / `<Routes>` / `<Route>` declarative API (not the new data router).

TailwindCSS v4 configured via `@import "tailwindcss"` in `index.css` + `@tailwindcss/vite` plugin in `vite.config.ts`. No `tailwind.config.js` file needed.

## Known Issues

- Windows CMD: `start.bat` needs `chcp 65001` for UTF-8 before printing Chinese text. `start` command titles should use ASCII to avoid garbled output.
- Git Bash: browser auto-open uses `start` directly, not `cmd.exe /c start` (which opens a cmd window).
- matplotlib on Windows Python 3.14 requires `--only-binary :all:` flag (`pip install matplotlib --only-binary :all:`).
- `frontend/tsconfig.app.json` has `verbatimModuleSyntax: false` and `erasableSyntaxOnly: false` — Vite 8's rolldown bundler doesn't handle type-only interface exports from `.ts` files otherwise.

## 测试

```bash
cd backend && python -m pytest tests/ -v
```

## 协作建议

- **开始前先对齐边界**：想清楚要什么，更重要的是**不要什么**——用一句话说清楚范围
- **完成后在浏览器里验证**：别只看代码，打开页面确认交互和状态都符合预期
- **交代任务时加一句 "为什么"**：帮助对方理解意图，而不是盲从指令
