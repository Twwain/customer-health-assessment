import ntpath
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from database import engine, Base, SessionLocal, migrate_drop_legacy_customer_columns
import config
from request_limits import AIRateLimitMiddleware, UploadBodyLimitMiddleware
from routers import assessment, chat, customers, knowledge


class SPAStaticFiles(StaticFiles):
    """安全地托管前端构建产物，并为客户端路由回退到固定入口文件。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # 使用 Windows 路径语义做跨平台预检，避免盘符、UNC、设备路径进入
        # ntpath.realpath 后触发跨盘异常或访问攻击者控制的 SMB 地址；控制字符与
        # Windows 非法文件名字符也必须提前拦截，避免 realpath/stat 抛出 500。
        has_invalid_char = any(ord(char) < 32 or char in '<>:"|?*' for char in path)
        if ntpath.splitdrive(path)[0] or ntpath.isabs(path) or has_invalid_char:
            raise StarletteHTTPException(status_code=404)

        # API 路由必须保持 JSON 404，不能让浏览器入口 HTML 掩盖接口拼写错误。
        # StaticFiles 在 Windows 上会先把 URL 路径规范化为反斜杠，因此统一后再判断。
        url_path = path.replace("\\", "/")
        if url_path == "api" or url_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # 构建产物引用的缺失资源必须保持 404，避免把 HTML 当作 JS/CSS 返回。
            if url_path == "assets" or url_path.startswith("assets/"):
                raise

        # StaticFiles 已完成静态根目录边界校验；未命中的路径一律使用固定文件名，
        # 不再把用户输入拼接后交给 FileResponse。
        return await super().get_response("index.html", scope)


def _migrate_legacy_data() -> None:
    """启动时执行幂等数据迁移（历史知识分类名校正等）。失败不阻断启动。

    顺序约定：先补 schema 列（迁移后的 ORM 查询会引用新列），再做数据迁移；
    每个迁移独立 try/except，单个失败不影响后续步骤，避免"缺列库带病启动"。
    """
    import logging

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    from services import assessment_history as assessment_history_service

    steps: list[tuple[str, Any]] = [
        (
            "补当前评分配置快照",
            lambda: assessment_history_service.backfill_current_config_snapshots(db),
        ),
    ]
    try:
        from services.rag.knowledge_base import (
            migrate_add_industry_column,
            migrate_legacy_categories,
            migrate_seed_knowledge_status,
        )
        from services.rag.vector_store import get_vector_store

        store = get_vector_store()
        steps.extend([
            ("补 knowledge_items.industry 列", lambda: migrate_add_industry_column(db, store=store)),
            ("校正历史知识分类名", lambda: migrate_legacy_categories(db, store=store)),
            ("seed 知识状态提升为 canonical", lambda: migrate_seed_knowledge_status(db, store=store)),
        ])
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
    # 结构迁移是当前模型契约的一部分，失败时必须阻止服务启动，不能带病运行。
    migrate_drop_legacy_customer_columns()
    _migrate_legacy_data()
    yield


app = FastAPI(title="客情评估智能体", lifespan=lifespan)

# 必须在 FastAPI 解析 multipart 之前执行，防止超大上传先写满内存/临时磁盘。
app.add_middleware(
    UploadBodyLimitMiddleware,
    max_body_bytes=config.UPLOAD_MAX_REQUEST_BYTES,
)
# 公网匿名演示的轻量突发保护；当前单 Uvicorn 进程下为整套应用共享。
app.add_middleware(
    AIRateLimitMiddleware,
    requests_per_minute=config.AI_RATE_LIMIT_PER_MINUTE,
)

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
    # API 路由均在此 mount 之前注册；StaticFiles 只可能接到静态资源或前端路由。
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR), name="frontend")
