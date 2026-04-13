from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlencode


def _load_simple_env_file(path: Path) -> None:
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


BOT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BOT_DIR.parent
_load_simple_env_file(PROJECT_DIR / ".env")

RUNTIME_DIR = BOT_DIR / "runtime"
SHOWDOWN_DIR = PROJECT_DIR / "server" / "pokemon-showdown"
SESSION_PATH = RUNTIME_DIR / "showdown_telegram_bot"
LEGACY_SQLITE_PATH = RUNTIME_DIR / "showdown_bot.sqlite3"

DATA_DIR = BOT_DIR / "game" / "data"
IMPORTED_DATA_DIR = DATA_DIR / "imported"
STARTERS_PATH = DATA_DIR / "starters.json"
REGIONS_PATH = DATA_DIR / "regions.json"
GENERATOR_DEFAULTS_PATH = DATA_DIR / "generator_defaults.json"
CATCH_SETTINGS_PATH = DATA_DIR / "catch_settings.json"
ENCOUNTER_POOLS_PATH = DATA_DIR / "encounter_pools.json"
SAFARI_POOLS_PATH = DATA_DIR / "safari_pools.json"
SPECIES_CATCH_RATES_PATH = DATA_DIR / "species_catch_rates.json"
RARITY_WEIGHTS_PATH = DATA_DIR / "rarity_weights.json"
SPECIES_REFERENCE_PATH = IMPORTED_DATA_DIR / "species_reference.json"
GROWTH_DATA_PATH = IMPORTED_DATA_DIR / "growth_data.json"
BASE_STATS_PATH = IMPORTED_DATA_DIR / "base_stats.json"
MOVE_INFO_PATH = IMPORTED_DATA_DIR / "move_info.json"
EXP_CHART_PATH = IMPORTED_DATA_DIR / "exp_chart.json"
EVOLUTION_CHAINS_PATH = IMPORTED_DATA_DIR / "evolution_chains.json"
SHINY_ART_PATH = IMPORTED_DATA_DIR / "shiny_art.json"
POKEDEX_REGIONS_PATH = IMPORTED_DATA_DIR / "pokedex_regions.json"
TM_DATA_PATH = IMPORTED_DATA_DIR / "tms.json"
TM_PRICES_PATH = IMPORTED_DATA_DIR / "tm_prices.json"
STONES_PATH = IMPORTED_DATA_DIR / "stones.json"
REGION_LOCATIONS_PATH = IMPORTED_DATA_DIR / "region_locations.json"
LOCATION_ENCOUNTERS_PATH = IMPORTED_DATA_DIR / "location_encounters.json"
POKECHAIN_PATH = DATA_DIR / "pokechain.json"

DEFAULT_RANDOM_BATTLE_FORMAT = os.getenv("SHOWDOWN_PVP_RANDOM_FORMAT", "gen9randombattle")
DEFAULT_RPG_BATTLE_FORMAT = os.getenv("SHOWDOWN_RPG_FORMAT", "gen9fullgimmicknationaldex")


def _first_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def _parse_int_list(raw: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for token in str(raw or "").replace(",", " ").split():
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_database_url(url: str) -> str:
    normalized = url.strip()
    if normalized.startswith("postgres://"):
        return "postgresql+psycopg://" + normalized[len("postgres://") :]
    if normalized.startswith("postgresql://") and not normalized.startswith("postgresql+"):
        return "postgresql+psycopg://" + normalized[len("postgresql://") :]
    return normalized


def _build_database_url_from_parts() -> str:
    host = _first_env("DB_HOST", "PGHOST", "POSTGRES_HOST")
    name = _first_env("DB_NAME", "PGDATABASE", "POSTGRES_DB")
    user = _first_env("DB_USER", "PGUSER", "POSTGRES_USER")
    password = _first_env("DB_PASS", "DB_PASSWORD", "PGPASSWORD", "POSTGRES_PASSWORD")
    port = _first_env("DB_PORT", "PGPORT", "POSTGRES_PORT", default="5432")
    sslmode = _first_env("DB_SSLMODE", "PGSSLMODE")
    ssl_flag = _first_env("DB_SSL", "PG_SSL")

    if not (host and name and user):
        return ""

    auth = quote(user, safe="")
    if password:
        auth = f"{auth}:{quote(password, safe='')}"

    query: dict[str, str] = {}
    if sslmode:
        query["sslmode"] = sslmode
    elif ssl_flag.lower() in {"1", "true", "yes", "on"}:
        query["sslmode"] = "require"

    query_string = f"?{urlencode(query)}" if query else ""
    return f"postgresql+psycopg://{auth}@{host}:{port}/{quote(name, safe='')}{query_string}"


def _build_redis_url_from_parts() -> str:
    host = _first_env("REDIS_HOST", default="localhost")
    port = _first_env("REDIS_PORT", default="6379")
    password = _first_env("REDIS_PASSWORD", "REDIS_PASS")
    db = _first_env("REDIS_DB", default="0")

    if not host or not port:
        return ""

    auth = ""
    if password:
        auth = f":{quote(password, safe='')}@"

    return f"redis://{auth}{host}:{port}/{quote(db, safe='')}"


DATABASE_URL = _normalize_database_url(
    _first_env("DATABASE_URL", "PG_URI")
    or _build_database_url_from_parts()
    or "postgresql+psycopg://postgres:flirter@localhost:5432/pokeplay"
)
SQLITE_DATABASE_URL = f"sqlite:///{LEGACY_SQLITE_PATH.as_posix()}"
DB_AUTO_FALLBACK_TO_SQLITE = _parse_bool(os.getenv("DB_AUTO_FALLBACK_TO_SQLITE"), default=True)

REDIS_URL = _first_env("REDIS_URL") or _build_redis_url_from_parts() or "redis://localhost:6379/0"

TELEGRAM_API_ID = _first_env("TELEGRAM_API_ID")
TELEGRAM_API_HASH = _first_env("TELEGRAM_API_HASH")
TELEGRAM_BOT_TOKEN = _first_env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
ADMIN_USER_IDS = _parse_int_list(_first_env("ADMIN_USER_IDS")) or [
    8551864967,
    6265981509,
    7577674783,
    6856118779,
]
ADMIN_USER_ID_SET = set(ADMIN_USER_IDS)


def ensure_runtime_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    IMPORTED_DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_api_id() -> int:
    if not TELEGRAM_API_ID:
        raise RuntimeError("Missing TELEGRAM_API_ID in environment or .env.")
    return int(TELEGRAM_API_ID)


def load_api_hash() -> str:
    if not TELEGRAM_API_HASH:
        raise RuntimeError("Missing TELEGRAM_API_HASH in environment or .env.")
    return TELEGRAM_API_HASH


def load_bot_token() -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment or .env.")
    return TELEGRAM_BOT_TOKEN
