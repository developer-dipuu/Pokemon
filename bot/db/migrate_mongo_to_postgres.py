from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from bson import json_util
from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pymongo import MongoClient

from bot.config import DATABASE_URL, _normalize_database_url


DEFAULT_MONGO_DB = "pokeplay2"
DEFAULT_BATCH_SIZE = 500
KNOWN_COLLECTIONS = ("users", "kv", "sessions", "battles")


def load_simple_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def sanitize_for_json(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json_util.dumps(value))


def to_jsonb_param(value: Any, fallback: Any = None) -> Jsonb:
    if value is None:
        value = fallback
    return Jsonb(sanitize_for_json(value))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate MongoDB collections into PostgreSQL JSONB tables.")
    parser.add_argument("--batch", dest="batch_size", type=int, default=int(os.getenv("MIGRATION_BATCH_SIZE", DEFAULT_BATCH_SIZE)))
    parser.add_argument("--truncate", action="store_true")
    parser.add_argument("--collections", default="users,kv,sessions,battles")
    parser.add_argument("--env-file", default=str(Path(".env")))
    args = parser.parse_args()
    args.batch_size = max(1, int(args.batch_size or DEFAULT_BATCH_SIZE))
    args.collections = [item.strip().lower() for item in str(args.collections).split(",") if item.strip()]
    return args


def get_mongo_config() -> dict[str, Any]:
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or ""
    if not uri:
        raise RuntimeError("Missing MONGODB_URI (or MONGO_URI) in environment.")
    return {
        "uri": uri,
        "db_name": os.getenv("MONGODB_DB") or DEFAULT_MONGO_DB,
        "max_pool_size": max(1, int(os.getenv("MONGO_POOL_MAX") or os.getenv("MONGODB_POOL_MAX") or 50)),
        "min_pool_size": max(0, int(os.getenv("MONGO_POOL_MIN") or os.getenv("MONGODB_POOL_MIN") or 5)),
    }


def get_postgres_config() -> dict[str, Any]:
    connection_string = _normalize_database_url(DATABASE_URL)
    if not connection_string:
        raise RuntimeError(
            "Missing PostgreSQL configuration. Set DATABASE_URL/PG_URI or DB_HOST, DB_NAME, DB_USER, DB_PASS, DB_PORT."
        )
    ssl_mode = str(os.getenv("DB_SSLMODE") or os.getenv("PGSSLMODE") or "").strip()
    if not ssl_mode:
        ssl_flag = str(os.getenv("DB_SSL") or os.getenv("PG_SSL") or "").strip().lower()
        if ssl_flag in {"true", "1", "yes", "on"}:
            ssl_mode = "require"
    return {
        "connection_string": connection_string,
        "sslmode": ssl_mode,
    }


def ensure_tables(pg) -> None:
    with pg.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              user_id TEXT,
              user_id_alias TEXT,
              data JSONB NOT NULL DEFAULT '{}'::jsonb,
              reset BOOLEAN NOT NULL DEFAULT FALSE,
              created_at TIMESTAMPTZ,
              updated_at TIMESTAMPTZ,
              mongo_doc JSONB NOT NULL
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS users_user_id_idx ON users(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS users_updated_at_idx ON users(updated_at);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
              key TEXT PRIMARY KEY,
              value JSONB,
              updated_at TIMESTAMPTZ,
              mongo_doc JSONB NOT NULL
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS kv_updated_at_idx ON kv(updated_at);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              key TEXT PRIMARY KEY,
              value JSONB,
              user_id TEXT,
              chat_id TEXT,
              updated_at TIMESTAMPTZ,
              mongo_doc JSONB NOT NULL
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS sessions_chat_id_idx ON sessions(chat_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS sessions_updated_at_idx ON sessions(updated_at);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS battles (
              id TEXT PRIMARY KEY,
              data JSONB,
              updated_at TIMESTAMPTZ,
              mongo_doc JSONB NOT NULL
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS battles_updated_at_idx ON battles(updated_at);")
    pg.commit()


def truncate_tables(pg, collections: list[str]) -> None:
    valid = [name for name in collections if name in KNOWN_COLLECTIONS]
    if not valid:
        return
    quoted = ", ".join(valid)
    with pg.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {quoted};")
    pg.commit()


def map_user_doc(doc: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(doc["_id"]),
        str(doc["user_id"]) if doc.get("user_id") is not None else None,
        str(doc["userId"]) if doc.get("userId") is not None else None,
        to_jsonb_param(doc.get("data") or {}, {}),
        bool(doc.get("reset")),
        doc.get("createdAt"),
        doc.get("updatedAt"),
        to_jsonb_param(doc, {}),
    )


def map_kv_doc(doc: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(doc["_id"]),
        to_jsonb_param(doc.get("value"), None),
        doc.get("updatedAt"),
        to_jsonb_param(doc, {}),
    )


def map_session_doc(doc: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(doc["_id"]),
        to_jsonb_param(doc.get("value"), None),
        str(doc["userId"]) if doc.get("userId") is not None else None,
        str(doc["chatId"]) if doc.get("chatId") is not None else None,
        doc.get("updatedAt"),
        to_jsonb_param(doc, {}),
    )


def map_battle_doc(doc: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(doc["_id"]),
        to_jsonb_param(doc.get("data"), None),
        doc.get("updatedAt"),
        to_jsonb_param(doc, {}),
    )


def upsert_batch(pg, name: str, docs: list[dict[str, Any]]) -> None:
    if not docs:
        return

    with pg.cursor() as cur:
        if name == "users":
            for doc in docs:
                cur.execute(
                    """
                    INSERT INTO users (id, user_id, user_id_alias, data, reset, created_at, updated_at, mongo_doc)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      user_id = EXCLUDED.user_id,
                      user_id_alias = EXCLUDED.user_id_alias,
                      data = EXCLUDED.data,
                      reset = EXCLUDED.reset,
                      created_at = EXCLUDED.created_at,
                      updated_at = EXCLUDED.updated_at,
                      mongo_doc = EXCLUDED.mongo_doc;
                    """,
                    map_user_doc(doc),
                )
            return

        if name == "kv":
            for doc in docs:
                cur.execute(
                    """
                    INSERT INTO kv (key, value, updated_at, mongo_doc)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key)
                    DO UPDATE SET
                      value = EXCLUDED.value,
                      updated_at = EXCLUDED.updated_at,
                      mongo_doc = EXCLUDED.mongo_doc;
                    """,
                    map_kv_doc(doc),
                )
            return

        if name == "sessions":
            for doc in docs:
                cur.execute(
                    """
                    INSERT INTO sessions (key, value, user_id, chat_id, updated_at, mongo_doc)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key)
                    DO UPDATE SET
                      value = EXCLUDED.value,
                      user_id = EXCLUDED.user_id,
                      chat_id = EXCLUDED.chat_id,
                      updated_at = EXCLUDED.updated_at,
                      mongo_doc = EXCLUDED.mongo_doc;
                    """,
                    map_session_doc(doc),
                )
            return

        if name == "battles":
            for doc in docs:
                cur.execute(
                    """
                    INSERT INTO battles (id, data, updated_at, mongo_doc)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      data = EXCLUDED.data,
                      updated_at = EXCLUDED.updated_at,
                      mongo_doc = EXCLUDED.mongo_doc;
                    """,
                    map_battle_doc(doc),
                )


def migrate_collection(mongo_db, pg, name: str, batch_size: int) -> int:
    if name not in KNOWN_COLLECTIONS:
        print(f'[skip] Unknown collection "{name}"')
        return 0

    collection = mongo_db[name]
    total = collection.count_documents({})
    print(f"[{name}] total docs: {total}")
    if total == 0:
        return 0

    moved = 0
    batch: list[dict[str, Any]] = []
    cursor = collection.find({})

    for doc in cursor:
        if not doc or doc.get("_id") is None:
            continue
        batch.append(doc)
        if len(batch) >= batch_size:
            upsert_batch(pg, name, batch)
            pg.commit()
            moved += len(batch)
            print(f"[{name}] migrated {moved}/{total}")
            batch = []

    if batch:
        upsert_batch(pg, name, batch)
        pg.commit()
        moved += len(batch)
        print(f"[{name}] migrated {moved}/{total}")

    return moved


def main() -> None:
    args = parse_args()
    load_simple_env_file(Path(args.env_file))

    mongo_config = get_mongo_config()
    pg_config = get_postgres_config()

    mongo = MongoClient(
        mongo_config["uri"],
        maxPoolSize=mongo_config["max_pool_size"],
        minPoolSize=mongo_config["min_pool_size"],
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=45000,
        retryWrites=True,
        retryReads=True,
    )
    connect_kwargs: dict[str, Any] = {
        "row_factory": dict_row,
    }
    if pg_config["sslmode"]:
        connect_kwargs["sslmode"] = pg_config["sslmode"]
    pg = connect(pg_config["connection_string"], **connect_kwargs)

    try:
        mongo_db = mongo[mongo_config["db_name"]]
        ensure_tables(pg)
        if args.truncate:
            print(f"[truncate] Clearing selected tables: {', '.join(args.collections)}")
            truncate_tables(pg, args.collections)

        total_moved = 0
        for name in args.collections:
            moved = migrate_collection(mongo_db, pg, name, args.batch_size)
            total_moved += moved

        print(f"Migration complete. Total migrated documents: {total_moved}")
    finally:
        mongo.close()
        pg.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Migration failed: {error}")
        raise SystemExit(1) from error
