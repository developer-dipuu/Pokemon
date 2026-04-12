from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, text

from bot.config import DATABASE_URL, LEGACY_SQLITE_PATH, _normalize_database_url
from bot.db.models import Base


TABLE_IMPORT_ORDER = [
    "trainers",
    "inventories",
    "owned_pokemon",
    "party_slots",
    "team_presets",
    "team_preset_slots",
]

SEQUENCE_TABLES = [
    "trainers",
    "owned_pokemon",
    "party_slots",
    "team_presets",
    "team_preset_slots",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import the legacy SQLite bot database into PostgreSQL.")
    parser.add_argument(
        "--sqlite-path",
        default=str(LEGACY_SQLITE_PATH),
        help="Path to the source SQLite database file.",
    )
    parser.add_argument(
        "--database-url",
        default=DATABASE_URL,
        help="Destination PostgreSQL SQLAlchemy URL. Defaults to DATABASE_URL.",
    )
    return parser.parse_args()


def _fetch_sqlite_rows(connection: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    cursor = connection.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _destination_has_rows(destination_engine) -> bool:
    with destination_engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            if connection.scalar(select(func.count()).select_from(table)):
                return True
    return False


def _reset_postgres_sequences(destination_engine) -> None:
    if destination_engine.dialect.name != "postgresql":
        return
    with destination_engine.begin() as connection:
        for table_name in SEQUENCE_TABLES:
            connection.execute(
                text(
                    f"""
                    SELECT setval(
                        pg_get_serial_sequence('{table_name}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                        (SELECT COUNT(*) > 0 FROM {table_name})
                    )
                    """
                )
            )


def main() -> None:
    args = _parse_args()
    sqlite_path = Path(args.sqlite_path).expanduser().resolve()
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")

    destination_engine = create_engine(_normalize_database_url(args.database_url), future=True, pool_pre_ping=True)
    if destination_engine.dialect.name != "postgresql":
        raise RuntimeError(
            "Destination DATABASE_URL must point to PostgreSQL. "
            f"Resolved dialect: {destination_engine.dialect.name}"
        )

    Base.metadata.create_all(destination_engine)

    if _destination_has_rows(destination_engine):
        raise RuntimeError("Destination PostgreSQL database is not empty. Refusing to import over existing data.")

    sqlite_connection = sqlite3.connect(sqlite_path)
    sqlite_connection.row_factory = sqlite3.Row

    imported_counts: dict[str, int] = {}
    try:
        with destination_engine.begin() as destination:
            for table_name in TABLE_IMPORT_ORDER:
                if not _table_exists(sqlite_connection, table_name):
                    imported_counts[table_name] = 0
                    continue
                rows = _fetch_sqlite_rows(sqlite_connection, table_name)
                imported_counts[table_name] = len(rows)
                if not rows:
                    continue
                table = Base.metadata.tables[table_name]
                destination.execute(table.insert(), rows)
    finally:
        sqlite_connection.close()

    _reset_postgres_sequences(destination_engine)

    print(f"Imported SQLite database from {sqlite_path} into PostgreSQL.")
    for table_name in TABLE_IMPORT_ORDER:
        print(f"{table_name}: {imported_counts.get(table_name, 0)} rows")


if __name__ == "__main__":
    main()
