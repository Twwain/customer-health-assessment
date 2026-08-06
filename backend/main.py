import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from database import engine, Base, SessionLocal
from routers import assessment, chat, customers, knowledge

Base.metadata.create_all(bind=engine)


def _migrate_legacy_data() -> None:
    """启动时执行幂等数据迁移（历史知识分类名校正等）。失败不阻断启动。"""
    import logging

    db = SessionLocal()
    try:
        from services.rag.knowledge_base import (
            migrate_legacy_categories,
            migrate_seed_knowledge_status,
        )
        from services.rag.vector_store import get_vector_store

        migrate_legacy_categories(db, store=get_vector_store())
        migrate_seed_knowledge_status(db, store=get_vector_store())
    except Exception as exc:  # pragma: no cover - 迁移失败不影响启动
        logging.getLogger(__name__).warning("数据迁移执行失败：%s", exc)
    finally:
        db.close()


_migrate_legacy_data()

app = FastAPI(title="客情评估智能体")

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
