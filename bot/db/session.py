from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Callable, TypeVar

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.config import (
    DATABASE_URL,
    DB_AUTO_FALLBACK_TO_SQLITE,
    SQLITE_DATABASE_URL,
    ensure_runtime_dirs,
)
from bot.db.models import Base

logger = logging.getLogger("PokemonBot")
_sqlite_write_lock = threading.RLock()

ensure_runtime_dirs()


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + database_url[len("postgresql+psycopg://") :]
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url[len("postgresql://") :]
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url[len("postgres://") :]
    return database_url

def _build_engine_kwargs(database_url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {
            "timeout": 30,
            "check_same_thread": False,
        }
    else:
        kwargs.update(
            {
                "pool_size": 20,
                "max_overflow": 40,
                "pool_timeout": 30,
                "pool_recycle": 1800,
                "pool_use_lifo": True,
            }
        )
    return kwargs


sync_database_url = DATABASE_URL
async_database_url = _async_database_url(DATABASE_URL)
engine: Engine
async_engine: AsyncEngine | None
SessionLocal: sessionmaker[Session]
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None


def _configure_engines(database_url: str) -> None:
    global sync_database_url, async_database_url, engine, async_engine, SessionLocal, AsyncSessionLocal

    sync_database_url = database_url
    async_database_url = _async_database_url(database_url)
    engine = create_engine(sync_database_url, **_build_engine_kwargs(sync_database_url))
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)

    async_engine = None
    AsyncSessionLocal = None
    if not sync_database_url.startswith("sqlite"):
        async_engine = create_async_engine(async_database_url, **_build_engine_kwargs(sync_database_url))
        AsyncSessionLocal = async_sessionmaker(
            bind=async_engine,
            autoflush=False,
            expire_on_commit=False,
            class_=AsyncSession,
        )


def _fallback_to_sqlite(reason: Exception) -> None:
    global engine, async_engine
    if sync_database_url.startswith("sqlite"):
        raise reason
    if not DB_AUTO_FALLBACK_TO_SQLITE:
        raise reason
    logger.warning(
        "PostgreSQL is unavailable for %s. Falling back to SQLite at %s. Original error: %s",
        DATABASE_URL,
        SQLITE_DATABASE_URL,
        reason,
    )
    if async_engine is not None:
        try:
            asyncio.create_task(async_engine.dispose())
        except Exception:
            pass
    try:
        engine.dispose()
    except Exception:
        pass
    _configure_engines(SQLITE_DATABASE_URL)


_configure_engines(DATABASE_URL)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    if not sync_database_url.startswith("sqlite"):
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
    except Exception:
        return

T = TypeVar("T")


def _ensure_owned_pokemon_columns(connection) -> None:
    owned_pokemon_columns = {column["name"] for column in inspect(connection).get_columns("owned_pokemon")}
    if "move_history_json" not in owned_pokemon_columns:
        connection.execute(text("ALTER TABLE owned_pokemon ADD COLUMN move_history_json TEXT DEFAULT '{}' NOT NULL"))
    if "form_state_json" not in owned_pokemon_columns:
        connection.execute(text("ALTER TABLE owned_pokemon ADD COLUMN form_state_json TEXT"))


def init_database() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        _ensure_owned_pokemon_columns(connection)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            trainer_columns = {column["name"] for column in inspect(connection).get_columns("trainers")}
            if "current_location" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN current_location VARCHAR(96)"))
            if "last_safari_entered_at" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN last_safari_entered_at DATETIME"))
            if "gender" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN gender VARCHAR(16)"))
            if "sort_descending" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN sort_descending BOOLEAN DEFAULT 1 NOT NULL"))
            if "challenge_mode" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN challenge_mode VARCHAR(16) DEFAULT 'owned' NOT NULL"))
            if "challenge_generation" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN challenge_generation INTEGER DEFAULT 9 NOT NULL"))
            if "battle_visuals" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN battle_visuals BOOLEAN DEFAULT 0 NOT NULL"))
            if "trainer_level" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN trainer_level INTEGER DEFAULT 1 NOT NULL"))
            if "trainer_exp" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN trainer_exp INTEGER DEFAULT 0 NOT NULL"))
            if "total_wins" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN total_wins INTEGER DEFAULT 0 NOT NULL"))
            if "total_losses" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN total_losses INTEGER DEFAULT 0 NOT NULL"))
            if "total_caught" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN total_caught INTEGER DEFAULT 0 NOT NULL"))
            if "pending_move_learning" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN pending_move_learning TEXT"))
            if "daycare_state_json" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN daycare_state_json TEXT"))
            if "eggs_json" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN eggs_json TEXT"))
            if "shop_state_json" not in trainer_columns:
                connection.execute(text("ALTER TABLE trainers ADD COLUMN shop_state_json TEXT"))
            inventory_columns = {column["name"] for column in inspect(connection).get_columns("inventories")}
            if "pokecoins" in inventory_columns and "victory_points" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories RENAME COLUMN pokecoins TO victory_points"))
            elif "victory_points" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN victory_points INTEGER DEFAULT 0 NOT NULL"))
            if "league_points" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN league_points INTEGER DEFAULT 0 NOT NULL"))
            if "season_points" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN season_points INTEGER DEFAULT 0 NOT NULL"))
            if "special_balls_json" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN special_balls_json TEXT DEFAULT '{}' NOT NULL"))
            if "held_items_json" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN held_items_json TEXT DEFAULT '{}' NOT NULL"))
            if "tm_inventory_json" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN tm_inventory_json TEXT DEFAULT '{}' NOT NULL"))
            if "medicine_inventory_json" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN medicine_inventory_json TEXT DEFAULT '{}' NOT NULL"))
            if "key_items_json" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN key_items_json TEXT DEFAULT '{}' NOT NULL"))
            if "egg_energy" not in inventory_columns:
                connection.execute(text("ALTER TABLE inventories ADD COLUMN egg_energy INTEGER DEFAULT 100 NOT NULL"))

async def init_database_async() -> None:
    if async_engine is None:
        await asyncio.to_thread(init_database)
        return
    try:
        async with async_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(_ensure_owned_pokemon_columns)
    except (OSError, OperationalError) as exc:
        _fallback_to_sqlite(exc)
        await asyncio.to_thread(init_database)


def clear_database() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def db_session(*, read_only: bool = False) -> Session:
    if read_only or not sync_database_url.startswith("sqlite"):
        session = SessionLocal()
        try:
            yield session
            if read_only:
                session.rollback()
            else:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return

    with _sqlite_write_lock:
        session = SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def run_db_work(work: Callable[[Session], T], *, read_only: bool = False) -> T:
    with db_session(read_only=read_only) as session:
        return work(session)


async def run_db_work_async(work: Callable[[Session], T], *, read_only: bool = False) -> T:
    if AsyncSessionLocal is None:
        return await asyncio.to_thread(run_db_work, work, read_only=read_only)

    async with AsyncSessionLocal() as session:
        try:
            result = await session.run_sync(work)
            if read_only:
                await session.rollback()
            else:
                await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def async_db_session(*, read_only: bool = False):
    if AsyncSessionLocal is None:
        raise RuntimeError("Async DB sessions are unavailable for this database URL.")
    async with AsyncSessionLocal() as session:
        try:
            yield session
            if read_only:
                await session.rollback()
            else:
                await session.commit()
        except Exception:
            await session.rollback()
            raise
