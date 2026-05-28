"""
DB engine and session setup.
Uses SQLite by default — mounted as a Docker volume so data survives restarts.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/weather.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    # create parent dir for SQLite files so local dev works without Docker
    url = make_url(DATABASE_URL)
    if url.drivername.startswith("sqlite") and url.database not in (None, ":memory:", "/:memory:"):
        db_dir = os.path.dirname(os.path.abspath(url.database))
        os.makedirs(db_dir, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
