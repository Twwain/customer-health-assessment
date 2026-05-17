from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

import os
DB_PATH = os.getenv("DB_PATH", "sqlite:///./customer_health.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}" if not DB_PATH.startswith("sqlite") else DB_PATH

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
