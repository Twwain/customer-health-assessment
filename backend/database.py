import datetime
import os
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy import inspect, text

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


LEGACY_CUSTOMER_COLUMNS = (
    "last_contact_date",
    "payment_status",
    "risk_signals",
    "competitor_involvement",
)


def migrate_drop_legacy_customer_columns(target_engine: Engine = engine) -> int:
    """幂等删除已被新版客情因子取代的客户旧字段。"""
    inspector = inspect(target_engine)
    if "customers" not in inspector.get_table_names():
        return 0

    existing = {column["name"] for column in inspector.get_columns("customers")}
    removed = 0
    with target_engine.begin() as connection:
        for column in LEGACY_CUSTOMER_COLUMNS:
            if column in existing:
                connection.execute(text(f'ALTER TABLE customers DROP COLUMN "{column}"'))
                removed += 1
    return removed


def migrate_add_chat_message_superseded_column(target_engine: Engine = engine) -> int:
    """幂等补充重新生成审计指针，并保证升级库具备真实自引用外键。"""
    inspector = inspect(target_engine)
    if "chat_messages" not in inspector.get_table_names():
        return 0
    existing = {column["name"] for column in inspector.get_columns("chat_messages")}
    added = 0
    if "superseded_by_id" not in existing:
        with target_engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE chat_messages ADD COLUMN superseded_by_id INTEGER "
                "REFERENCES chat_messages(id) ON DELETE SET NULL"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_superseded_by_id "
                "ON chat_messages (superseded_by_id)"
            ))
        return 1

    with target_engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list(chat_messages)"
        ).fetchall()
    has_superseded_fk = any(
        str(row[2]) == "chat_messages"
        and str(row[3]) == "superseded_by_id"
        and str(row[6]).upper() == "SET NULL"
        for row in foreign_keys
    )
    if has_superseded_fk:
        with target_engine.begin() as connection:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_superseded_by_id "
                "ON chat_messages (superseded_by_id)"
            ))
        return 0

    # 3f0ba0b 曾以普通 INTEGER 补列；SQLite 不能给已有列追加外键，只能在事务内
    # 重建表。关闭外键仅覆盖该连接，数据复制完成后立即校验并恢复。
    raw = target_engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN")
        cursor.execute("""
            CREATE TABLE chat_messages__fk_migration (
                id INTEGER NOT NULL PRIMARY KEY,
                session_id INTEGER NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                "references" JSON NOT NULL,
                strategy_items JSON NOT NULL,
                tokens_used INTEGER NOT NULL,
                feedback VARCHAR(10) NOT NULL,
                degraded BOOLEAN NOT NULL,
                superseded_by_id INTEGER,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(superseded_by_id) REFERENCES chat_messages__fk_migration(id)
                    ON DELETE SET NULL
            )
        """)
        cursor.execute("""
            INSERT INTO chat_messages__fk_migration (
                id, session_id, role, content, "references", strategy_items,
                tokens_used, feedback, degraded, superseded_by_id, created_at
            )
            SELECT id, session_id, role, content, "references", strategy_items,
                   tokens_used, feedback, degraded, superseded_by_id, created_at
            FROM chat_messages
        """)
        cursor.execute("DROP TABLE chat_messages")
        cursor.execute("ALTER TABLE chat_messages__fk_migration RENAME TO chat_messages")
        cursor.execute(
            "CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id)"
        )
        cursor.execute(
            "CREATE INDEX ix_chat_messages_superseded_by_id "
            "ON chat_messages (superseded_by_id)"
        )
        violations = cursor.execute("PRAGMA foreign_key_check(chat_messages)").fetchall()
        if violations:
            raise RuntimeError(f"chat_messages 外键迁移校验失败：{violations[:3]}")
        cursor.execute("COMMIT")
        added = 1
    except Exception:
        raw.rollback()
        raise
    finally:
        # PRAGMA 是连接级状态；即使重建/校验失败也必须恢复，避免把关闭外键的
        # 连接放回连接池后污染后续请求。
        try:
            raw.cursor().execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        raw.close()
    return added


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
