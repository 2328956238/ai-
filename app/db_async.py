"""异步数据库连接。

新增代码（工具、Agent 编排、会话）全部走异步 Session；
原有同步 engine / SessionLocal 原样保留，互不影响，共用同一个 Base 与 metadata。
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .database import Base

_async_url = settings.async_url

_async_connect_args = {}
if _async_url.startswith("sqlite"):
    _async_connect_args = {"check_same_thread": False}

async_engine = create_async_engine(
    _async_url,
    echo=False,
    pool_pre_ping=not _async_url.startswith("sqlite"),
    connect_args=_async_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_async_db():
    """FastAPI 依赖：每个请求一个异步 Session。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """开发环境一键建表；生产请使用 alembic 迁移。"""
    # 确保所有模型都已导入并注册到 Base.metadata
    from . import models  # noqa: F401

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def reset_engine(url: str) -> None:
    """测试辅助：切换数据库地址后重建引擎。"""
    global async_engine, AsyncSessionLocal
    async_engine = create_async_engine(url, connect_args={"check_same_thread": False})
    AsyncSessionLocal = async_sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
