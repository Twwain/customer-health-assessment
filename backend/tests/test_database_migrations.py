from sqlalchemy import create_engine, inspect, text

from database import migrate_drop_legacy_customer_columns


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
