# Stage 1 — 构建前端
FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2 — 运行后端
FROM python:3.12-slim
WORKDIR /app

# 中文字体（PDF 报告用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（核心）
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 后端代码（含可选依赖清单 requirements-prod.txt）
COPY backend/ ./

# 生产可选依赖：缺失时系统自动回退内存实现，不影响核心功能
# （chromadb 向量库 / pymupdf / python-docx 文档解析 / FlagEmbedding 重排）
RUN pip install --no-cache-dir -r requirements-prod.txt || { \
      echo "===================== WARNING ====================="; \
      echo "可选依赖安装失败！RAG 向量库 / PDF 解析 / 重排将回退内存实现，"; \
      echo "知识库检索能力显著降级。请检查网络或私有 PyPI 源后重建镜像。"; \
      echo "==================================================="; \
    }

# 前端构建产物
COPY --from=frontend-builder /app/frontend/dist ./static

# 数据持久化
VOLUME ["/app/data"]

# 生产环境默认配置（可被 .env / docker-compose env_file 覆盖）
ENV DB_PATH=/app/data/customer_health.db \
    LLM_ENABLED=true \
    KNOWLEDGE_VECTOR_STORE=chroma \
    RAG_TOP_K=5 \
    CHAT_TREND_POINTS=6

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
