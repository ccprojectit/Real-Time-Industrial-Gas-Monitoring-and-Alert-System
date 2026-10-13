import asyncpg
from .config import settings

pool: asyncpg.Pool | None = None


async def connect() -> None:
    global pool
    if settings.database_url:
        pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5, command_timeout=10)


async def close() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


def require_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database is not configured. Set DATABASE_URL and apply database/migrations/001_initial.sql.")
    return pool
