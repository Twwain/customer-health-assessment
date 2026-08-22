import asyncio

import pytest
from sqlalchemy import create_engine, inspect, text

from database import migrate_drop_legacy_customer_columns
import main


def test_drop_legacy_customer_columns_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE customers (
                    id INTEGER PRIMARY KEY,
                    customer_name VARCHAR(100) NOT NULL,
                    last_contact_date DATE,
                    payment_status VARCHAR(20),
                    risk_signals VARCHAR(500),
                    competitor_involvement BOOLEAN
                )
                """
            )
        )

    assert migrate_drop_legacy_customer_columns(engine) == 4
    assert migrate_drop_legacy_customer_columns(engine) == 0
    assert {column["name"] for column in inspect(engine).get_columns("customers")} == {
        "id",
        "customer_name",
    }


def test_lifespan_fails_when_schema_migration_fails(monkeypatch):
    monkeypatch.setattr(main.Base.metadata, "create_all", lambda **_: None)

    def schema_migration():
        raise RuntimeError("drop failed")

    monkeypatch.setattr(main, "migrate_drop_legacy_customer_columns", schema_migration)
    data_migration_called = False

    def data_migration():
        nonlocal data_migration_called
        data_migration_called = True

    monkeypatch.setattr(main, "_migrate_legacy_data", data_migration)

    async def start_app():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError, match="drop failed"):
        asyncio.run(start_app())
    assert data_migration_called is False
