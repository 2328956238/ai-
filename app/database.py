"""同步数据库连接（保留给原有 CRUD / 批量任务，行为不变）。

改动只有一处：连接串改为从集中配置读取，并在未配置时回退到本地 SQLite，
避免原代码 `os.getenv("DATABASE_URL")` 返回 None 导致 create_engine 直接崩。
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

try:  # 新配置层可用时优先使用
    from .config import settings

    _URL = settings.database_url
except Exception:  # pragma: no cover - 兜底，保证老用法不炸
    _URL = os.getenv("DATABASE_URL", "sqlite:///./product.db")

if not _URL:
    _URL = "sqlite:///./product.db"

_connect_args = {}
if _URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
