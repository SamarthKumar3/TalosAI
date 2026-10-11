from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from talos.persistence.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema_for_tests(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def ready(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.engine.dispose()
