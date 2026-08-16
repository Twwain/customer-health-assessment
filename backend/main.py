import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from database import engine, Base, SessionLocal
from routers import assessment, chat, customers, knowledge

def _migrate_legacy_data() -> None:
    """启动时执行幂等数据迁移（历史知识分类名校正等）。失败不阻断启动。

    顺序约定：先补 schema 列（迁移后的 ORM 查询会引用新列），再做数据迁移；
    每个迁移独立 try/except，单个失败不影响后续步骤，避免"缺列库带病启动"。
    """
    import logging

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    steps: list[tuple[str, Any]] = []
    try:
        from services.rag.knowledge_base import (
            migrate_add_industry_column,
            migrate_legacy_categories,
            migrate_seed_knowledge_status,
        )
        from services.rag.vector_store import get_vector_store

        store = get_vector_store()
        steps = [
            ("补 knowledge_items.industry 列", lambda: migrate_add_industry_column(db, store=store)),
            ("校正历史知识分类名", lambda: migrate_legacy_categories(db, store=store)),
            ("seed 知识状态提升为 canonical", lambda: migrate_seed_knowledge_status(db, store=store)),
        ]
    except Exception as exc:  # pragma: no cover - 依赖导入失败不影响启动
        logger.warning("数据迁移准备失败：%s", exc)
    try:
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # 单个迁移失败不影响启动，但数据库可能不完整
                logger.warning("数据迁移「%s」执行失败（请检查数据库状态）：%s", name, exc)
                db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用启动时初始化数据库；单纯导入 ``main`` 不再产生写库副作用。"""
    Base.metadata.create_all(bind=engine)
    _migrate_legacy_data()
    yield


app = FastAPI(title="客情评估智能体", lifespan=lifespan)

# CORS：生产为同源部署，跨域主要面向本地开发（Vite 代理外的直连场景）。
# allow_origins="*" 与 allow_credentials=True 是浏览器规范禁止的组合，
# 本系统不使用 Cookie 认证，直接关闭 credentials。可用 CORS_ORIGINS 环境变量收窄。
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(customers.router, prefix="/api")
app.include_router(assessment.router, prefix="/api")
app.include_router(assessment.history_router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(knowledge.router, prefix="/api")


@app.get("/api")
def root():
    return {"message": "客情评估智能体 API"}


# 生产模式：serve 前端静态文件
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # 未匹配的 /api/* 路径返回 404 JSON，而不是 index.html（避免前端把 HTML 当成功响应）
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        file_path = os.path.join(STATIC_DIR, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
