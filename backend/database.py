import datetime
import os
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = os.getenv("DB_PATH", "sqlite:///./customer_health.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}" if not DB_PATH.startswith("sqlite") else DB_PATH

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite 默认不校验外键，需显式打开。

    ON DELETE CASCADE 依赖此开关：删除客户时级联清理评估历史，
    删除知识文档时级联清理切片与条目（避免向量库出现孤儿记录）。

    同时开启 WAL 与 busy_timeout：SSE 流式会话与普通请求并发写时，
    默认 rollback journal 容易报 "database is locked"。
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def utcnow() -> datetime.datetime:
    """返回 UTC 当前时间（naive），与模型列的 server_default=func.now() 保持一致。

    业务代码写入 DateTime 列时一律使用本函数，避免混入本地时区。
    """
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
