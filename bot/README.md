# Showdown Telegram Bot

This bot layer is intentionally split into modules so the RPG systems can grow without collapsing back into one giant file.

## Layout

- `bot/main.py`: Telethon entry point and handler wiring.
- `bot/config.py`: env loading, paths, runtime directories, and shared config.
- `bot/bridge/`: local bridge into the installed Pokemon Showdown simulator.
- `bot/battle/`: PvP challenge flow, owned-team battles, encounter battle rendering, and action handling.
- `bot/db/`: SQLAlchemy models/session/repositories, now configured for PostgreSQL.
- `bot/game/data/`: editable JSON data for starters, regions, encounter pools, safari pools, rarity weights, and catch tuning.
- `bot/game/services/`: DM-side trainer, team, encounter, data, and stats systems.

## What is implemented now

- DM `/start` and `/starter` flow.
- Region-first starter selection using inline buttons.
- Starter Pokemon generation through Showdown Dex data.
- Starter persistence with SQLAlchemy.
- `/mypokemons`, `/display`, `/sort`, `/mybag`, `/myteam`, `/fly`, `/walk`, `/hunt`, `/safari`, `/stats`, and `/cleardb`.
- Group `/battle` for owned active-team battles and `/randombattle` for Showdown randoms.
- Six saved team presets with active team switching, add/remove/swap/randomize, and rename prompts.
- Data-driven wild encounters and safari encounters with editable pools and catch rates.
- Hunt cards now send artwork with `Battle` and `EV Yield`, and a fresh `/hunt` invalidates the previous non-battle card.
- `/stats <name>` supports duplicate-Pokemon selection, artwork cards, IV/EV page, move page, stat page, held-item page, evolution info, and release confirmation.
- Wild battles on top of the same Showdown engine, with custom ball throws and run handling that do not spend a simulator turn.
- Cleaned public battle layout with latest-turn logs and `[ TURN ]` marker.
- Battle message render caching to reduce keyboard flicker from repeated edits.
- Imported data from the previous game now powers artwork, EV yields, EXP curves, base stats, move detail text, shiny art, safari pools, and catch rates.

## Generator fields

Generated Pokemon currently include:

- species
- level
- ability
- moves
- IVs
- EVs
- nature
- gender
- friendship
- item
- current HP / max HP
- export text / packed set
- `untradeable` and `unreleasable` flags

Wild and caught Pokemon use the same generator path, so starter and encounter data stay consistent.

## Run

Use the project root `.env` with:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_BOT_TOKEN`
- either `DATABASE_URL`
- or `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASS`, `DB_PORT`

Example PostgreSQL URL:

```text
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/pokeplay_showdown
```

Equivalent split credentials:

```text
DB_HOST=localhost
DB_NAME=showdownreal
DB_USER=myuser
DB_PASS=mypassword
DB_PORT=5432
```

Optional Redis support:

```text
REDIS_URL=redis://localhost:6379/0
```

If you cannot install Redis locally on Windows, the bot can also use an in-memory Redis-compatible fallback via `fakeredis`.
This fallback is intended for development and local use; for production, a real Redis server or managed Redis service is still recommended.

Redis is used for callback flood control and temporary ephemeral state. It is optional but recommended for higher-frequency button and battle traffic.

Install Python deps in the venv, then run:

```powershell
.\.venv\Scripts\python.exe -m bot.main
```

## Migrating old SQLite data

If you already have data in `bot/runtime/showdown_bot.sqlite3`, create an empty PostgreSQL database and run:

```powershell
.\.venv\Scripts\python.exe -m bot.db.migrate_sqlite_to_postgres
```

You can also point at a different source or destination:

```powershell
.\.venv\Scripts\python.exe -m bot.db.migrate_sqlite_to_postgres --sqlite-path C:\path\to\showdown_bot.sqlite3 --database-url postgresql+psycopg://user:pass@localhost:5432/pokeplay_showdown
```

## Next layers

- richer inventory pockets for held items and TMs
- exact region encounter/safari data replacement from your own files
- catch rewards, shops, and Pokecoin sinks
- trade / release rules using the stored `untradeable` and `unreleasable` flags
- deeper collection screens like per-Pokemon detail pages and filters
