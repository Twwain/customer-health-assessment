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

# 生产可选依赖开关（默认安装）：
#   INSTALL_PROD_DEPS=1（默认）→ 安装 chromadb / pymupdf / python-docx
#   INSTALL_PROD_DEPS=0      → 跳过，仅适用于显式使用 memory 向量库的开发测试。
# 生产 Chroma 依赖缺失时失败关闭，不自动回退内存库。
ARG INSTALL_PROD_DEPS=1
# PyPI 镜像源：国内网络建议用清华源等镜像加速，可被 docker compose build --build-arg 覆盖
ARG PIP_INDEX_URL=https://pypi.org/simple

# 中文字体（PDF 报告用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（核心）
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 后端代码（含可选依赖清单 requirements-prod.txt）
COPY backend/ ./

# 生产依赖：缺失会影响 Chroma 和相应文档解析能力，须检查构建日志
# （chromadb 向量库 / pymupdf / python-docx 文档解析）
RUN if [ "$INSTALL_PROD_DEPS" = "1" ]; then \
      pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements-prod.txt || { \
        echo "===================== WARNING ====================="; \
        echo "生产依赖安装失败！Chroma 不会自动回退内存，相关知识检索或解析不可用。"; \
        echo "请检查网络或私有 PyPI 源并重建镜像，确认 readiness 后再部署。"; \
        echo "==================================================="; \
      }; \
    else \
      echo "INSTALL_PROD_DEPS=0：已跳过生产依赖，仅适用于显式 memory 开发测试配置"; \
    fi

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
