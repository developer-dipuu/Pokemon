from __future__ import annotations

import argparse
import html
import json
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.config import DATABASE_URL, _normalize_database_url
from bot.db.models import Base, Trainer
from bot.db.repositories import (
    TeamRepository,
    TrainerRepository,
    normalize_display_mode,
    normalize_sort_mode,
)
from bot.game.balls import BALL_FIELDS, normalize_ball_kind, serialize_extra_ball_counts
from bot.game.fusion import build_effective_export_text, build_effective_packed_set
from bot.game.services.pokemon_data import PokemonDataService


UTC = timezone.utc


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_STEM = "pokeplay_backup_2026-04-08T16-14-13-602Z"
DEFAULT_SOURCE_DIR = SCRIPT_DIR / DEFAULT_SOURCE_STEM
DEFAULT_SOURCE_ZIP = SCRIPT_DIR / f"{DEFAULT_SOURCE_STEM}.zip"
SUPPORTED_REGIONS = {
    "kanto",
    "johto",
    "hoenn",
    "sinnoh",
    "unova",
    "kalos",
    "alola",
    "galar",
    "hisui",
    "paldea",
}
KNOWN_TOP_LEVEL_KEYS = {"_id", "createdAt", "updatedAt", "reset", "userId", "user_id", "data"}
KNOWN_DATA_KEYS = {"pokes", "inv", "balls", "extra", "settings", "tms", "pokecaught", "pokeseen", "teams", "refers", "user_id"}
KNOWN_INV_KEYS = {
    "region",
    "exp",
    "avtar",
    "template",
    "id",
    "data",
    "hometown",
    "name",
    "team",
    "pc",
    "win",
    "lose",
    "candy",
    "stones",
    "ring",
    "omniring",
    "gmax_band",
    "shiny_charm",
    "league_points",
    "holowear_tickets",
    "battle_boxes",
    "daycare_candy",
}
KNOWN_EXTRA_KEYS = {
    "date",
    "megas",
    "pending",
    "refer",
    "huntd",
    "hunts",
    "unlocks",
    "rankRewards",
    "rankLevel",
    "saf",
    "lastsafari",
    "hunting",
    "spinEvent",
    "itembox",
    "daycare",
    "sort",
    "display",
    "referred",
    "pendingMoveLearn",
    "sort_order",
    "location",
    "tmshop",
    "randombattle_pokes",
    "randombattle_settings",
}
KNOWN_POKEMON_KEYS = {
    "name",
    "id",
    "nature",
    "ability",
    "held_item",
    "exp",
    "pass",
    "cpass",
    "ivs",
    "symbol",
    "evs",
    "moves",
    "nickname",
    "temp_battle",
    "gmax",
    "gender",
    "shiny",
}
WARNING_SAMPLE_LIMIT = 200
UNIQUE_KEY_ITEM_NAMES = {
    "Gmax Band",
    "Mega Ring",
    "Omni Ring",
    "Shiny Charm",
}
KNOWN_HELD_ITEMS = {
    "life-orb",
    "eviolite",
    "choice-band",
    "choice-specs",
    "choice-scarf",
    "leftovers",
    "rocky-helmet",
    "assault-vest",
    "focus-sash",
    "shell-bell",
    "weakness-policy",
    "expert-belt",
    "iron-ball",
    "flame-orb",
    "toxic-orb",
    "black-sludge",
    "lum-berry",
    "sitrus-berry",
    "stone-plate",
    "black-glasses",
    "charcoal",
    "mystic-water",
    "miracle-seed",
    "never-melt-ice",
    "magnet",
    "twisted-spoon",
    "poison-barb",
    "soft-sand",
    "sharp-beak",
    "silk-scarf",
    "silver-powder",
    "spell-tag",
    "metal-coat",
    "dragon-fang",
    "hard-stone",
    "pixie-plate",
    "power-herb",
    "white-herb",
    "kings-rock",
    "scope-lens",
    "razor-claw",
    "razor-fang",
    "bright-powder",
    "quick-claw",
    "muscle-band",
    "wise-glasses",
    "thick-club",
    "deep-sea-tooth",
    "deep-sea-scale",
    "light-ball",
    "lucky-punch",
    "stick",
    "power-weight",
    "power-bracer",
    "power-belt",
    "power-lens",
    "power-band",
    "power-anklet",
}


@dataclass(slots=True)
class PreparedPokemon:
    legacy_pass: str
    payload: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(slots=True)
class PreparedTrainer:
    telegram_user_id: int
    display_name: str
    username: str | None
    trainer_fields: dict[str, Any]
    inventory_fields: dict[str, Any]
    pokemon: list[PreparedPokemon]
    teams: dict[int, list[str]]
    active_team_slot: int
    source_file: str


class MigrationReport:
    def __init__(self, *, source: Path, dry_run: bool) -> None:
        self.payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": str(source),
            "dry_run": bool(dry_run),
            "counts": Counter(),
            "defaulted_fields": Counter(),
            "legacy_only_fields": Counter(),
            "unknown_species": Counter(),
            "unknown_moves": Counter(),
            "unsupported_ball_kinds": Counter(),
            "warnings": [],
            "skipped_users": [],
            "skipped_pokemon": [],
            "fatal_error": None,
        }

    def bump(self, key: str, amount: int = 1) -> None:
        self.payload["counts"][key] += int(amount)

    def defaulted(self, field_name: str, amount: int = 1) -> None:
        self.payload["defaulted_fields"][field_name] += int(amount)

    def legacy_only(self, field_name: str, amount: int = 1) -> None:
        self.payload["legacy_only_fields"][field_name] += int(amount)

    def unknown_species(self, species: str) -> None:
        self.payload["unknown_species"][species] += 1

    def unknown_move(self, move_value: str) -> None:
        self.payload["unknown_moves"][move_value] += 1

    def unsupported_ball(self, ball_name: str, amount: int = 1) -> None:
        self.payload["unsupported_ball_kinds"][ball_name] += int(amount)

    def warn(self, message: str) -> None:
        if len(self.payload["warnings"]) < WARNING_SAMPLE_LIMIT:
            self.payload["warnings"].append(message)

    def skipped_user(self, user_id: str, reason: str, *, source_file: str) -> None:
        if len(self.payload["skipped_users"]) < WARNING_SAMPLE_LIMIT:
            self.payload["skipped_users"].append({
                "user_id": user_id,
                "reason": reason,
                "source_file": source_file,
            })

    def skipped_pokemon(self, user_id: str, species: str, reason: str, *, source_file: str) -> None:
        if len(self.payload["skipped_pokemon"]) < WARNING_SAMPLE_LIMIT:
            self.payload["skipped_pokemon"].append({
                "user_id": user_id,
                "species": species,
                "reason": reason,
                "source_file": source_file,
            })

    def set_fatal_error(self, error_text: str) -> None:
        self.payload["fatal_error"] = error_text

    def as_json(self) -> str:
        serializable = dict(self.payload)
        serializable["counts"] = dict(serializable["counts"])
        serializable["defaulted_fields"] = dict(serializable["defaulted_fields"])
        serializable["legacy_only_fields"] = dict(serializable["legacy_only_fields"])
        serializable["unknown_species"] = dict(serializable["unknown_species"])
        serializable["unknown_moves"] = dict(serializable["unknown_moves"])
        serializable["unsupported_ball_kinds"] = dict(serializable["unsupported_ball_kinds"])
        return json.dumps(serializable, indent=2, sort_keys=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a legacy pokeplay JSON backup into the current PostgreSQL schema."
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Path to the extracted backup directory or the backup .zip file.",
    )
    parser.add_argument(
        "--database-url",
        default=DATABASE_URL,
        help="Destination SQLAlchemy URL. Defaults to DATABASE_URL from the environment.",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Where to write the migration report JSON.",
    )
    parser.add_argument(
        "--limit-users",
        type=int,
        default=None,
        help="Only process the first N user files. Helpful for testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and transform the backup without writing anything to the database.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        default=True,
        help="Replace trainers that already exist in the destination database. Enabled by default.",
    )
    return parser.parse_args()


def resolve_source(source_arg: str | None) -> Path:
    if source_arg:
        source_path = Path(source_arg).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Backup source not found: {source_path}")
        return source_path
    if DEFAULT_SOURCE_DIR.exists():
        return DEFAULT_SOURCE_DIR
    if DEFAULT_SOURCE_ZIP.exists():
        return DEFAULT_SOURCE_ZIP
    raise FileNotFoundError(
        "Could not find a default backup source. "
        f"Expected either {DEFAULT_SOURCE_DIR} or {DEFAULT_SOURCE_ZIP}."
    )


def default_report_path(source: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return SCRIPT_DIR / f"migration_report_{source.stem}_{timestamp}.json"


def find_backup_root(root: Path) -> Path:
    if (root / "users").is_dir():
        return root
    for child in root.iterdir():
        if child.is_dir() and (child / "users").is_dir():
            return child
    raise FileNotFoundError(f"Could not find a backup root with a users directory under {root}")


def extracted_backup_root(source: Path) -> tuple[tempfile.TemporaryDirectory[str] | None, Path]:
    if source.is_dir():
        return None, find_backup_root(source)
    if source.suffix.lower() != ".zip":
        raise ValueError(f"Unsupported backup source: {source}. Expected a directory or .zip file.")
    temp_dir = tempfile.TemporaryDirectory(prefix="pokeplay-backup-")
    with zipfile.ZipFile(source) as archive:
        archive.extractall(temp_dir.name)
    return temp_dir, find_backup_root(Path(temp_dir.name))


def iter_user_files(backup_root: Path, limit: int | None) -> list[Path]:
    users_dir = backup_root / "users"
    files = sorted(path for path in users_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json")
    if limit is not None:
        files = files[: max(0, int(limit))]
    return files


def parse_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def parse_json_list_file(path: Path) -> list[Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return payload


def parse_legacy_datetime(value: Any) -> datetime | None:
    if isinstance(value, dict) and "$date" in value:
        value = value["$date"]
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=UTC).replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) == 10 and text.count("-") == 2:
            return datetime.fromisoformat(text)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
        for fmt in ("%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def parse_legacy_user_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    return " ".join(text.split())


def pretty_label(value: Any) -> str:
    text = clean_text(value).replace("-", " ").replace("_", " ")
    if not text:
        return ""
    parts = [part for part in text.split(" ") if part]
    return " ".join(part.upper() if len(part) == 1 else part.capitalize() for part in parts)


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def is_key_item_slug(value: Any) -> bool:
    slug = clean_text(value).lower()
    if not slug or slug in KNOWN_HELD_ITEMS:
        return False
    return (
        slug.endswith("ite")
        or slug.endswith("ite-x")
        or slug.endswith("ite-y")
        or slug.endswith("-orb")
        or slug.startswith("rusted-")
        or slug.endswith("-z")
        or slug == "baxcaliburite"
    )


def recover_key_item_slugs_from_pokemon(raw_pokemon_list: list[Any]) -> list[str]:
    recovered: list[str] = []
    for raw_pokemon in raw_pokemon_list:
        if not isinstance(raw_pokemon, dict):
            continue
        held_item = clean_text(raw_pokemon.get("held_item")).lower()
        if held_item and held_item != "none" and is_key_item_slug(held_item):
            recovered.append(held_item)
    return recovered


def normalize_region(value: Any) -> str | None:
    raw = clean_text(value).lower().replace("_", "-").replace(" ", "-")
    if not raw:
        return None
    for prefix in ("letsgo-", "letgo-", "lgpe-", "region-"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    if raw in SUPPORTED_REGIONS:
        return raw
    tail = raw.split("-")[-1]
    if tail in SUPPORTED_REGIONS:
        return tail
    for region in SUPPORTED_REGIONS:
        if region in raw:
            return region
    return None


def normalize_gender(value: Any) -> str:
    text = clean_text(value).lower()
    if text in {"male", "m", "♂"}:
        return "M"
    if text in {"female", "f", "♀"}:
        return "F"
    return ""


def trainer_level_from_exp(exp_amount: int) -> int:
    experience = max(0, int(exp_amount))
    for level in range(200, 0, -1):
        if experience >= TrainerRepository.exp_for_level(level):
            return level
    return 1


def pokemon_level_from_exp(species: str, exp_amount: int, data_service: PokemonDataService) -> int:
    experience = max(0, int(exp_amount))
    growth_rate = data_service.growth_rate(species)
    for level in range(100, 0, -1):
        if experience >= data_service.level_curve_value(growth_rate, level):
            return level
    return 1


def resolve_species_name(raw_species: Any, pokedex_id: Any, data_service: PokemonDataService) -> str | None:
    candidates = [clean_text(raw_species)]
    number = parse_int(pokedex_id, default=0)
    if number > 0:
        by_number = data_service.species_from_pokedex_number(number)
        if by_number:
            candidates.append(by_number)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = candidate.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        resolved = data_service.species_name(candidate)
        if data_service.species_entry(resolved):
            return resolved
        if data_service.species_entry(candidate):
            return data_service.species_name(candidate)
    return None


def legacy_keys_report(report: MigrationReport, prefix: str, payload: dict[str, Any], known_keys: set[str]) -> None:
    for key in payload:
        if prefix == "inv" and (key == "balls" or normalize_ball_kind(str(key))):
            continue
        if key not in known_keys:
            report.legacy_only(f"{prefix}.{key}")


def parse_json_count_map(raw: str | None) -> dict[str, int]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in payload.items():
        name = clean_text(key)
        amount = parse_int(value, default=0)
        if name and amount > 0:
            counts[name] = amount
    return counts


def count_map_to_json(counts: dict[str, int]) -> str:
    cleaned = {
        clean_text(key): int(value)
        for key, value in counts.items()
        if clean_text(key) and int(value) > 0
    }
    return json.dumps(dict(sorted(cleaned.items())), sort_keys=True)


def merge_count_json_strings(*raw_values: str) -> str:
    merged: dict[str, int] = {}
    for raw_value in raw_values:
        for key, amount in parse_json_count_map(raw_value).items():
            merged[key] = merged.get(key, 0) + amount
    return count_map_to_json(merged)


def merge_key_item_json_strings(*raw_values: str) -> str:
    merged: dict[str, int] = {}
    for raw_value in raw_values:
        for key, amount in parse_json_count_map(raw_value).items():
            if key in UNIQUE_KEY_ITEM_NAMES:
                merged[key] = 1 if amount > 0 or merged.get(key, 0) > 0 else 0
                continue
            merged[key] = merged.get(key, 0) + amount
    return count_map_to_json(merged)


def load_transfer_request_key_items(backup_root: Path, report: MigrationReport) -> dict[int, dict[str, int]]:
    kv_path = backup_root / "kv.json"
    if not kv_path.exists():
        return {}
    try:
        payload = parse_json_list_file(kv_path)
    except Exception as exc:
        report.warn(f"Could not parse kv.json for transfer requests: {type(exc).__name__}: {exc}")
        return {}

    transfer_requests: dict[str, Any] | None = None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if clean_text(entry.get("_id")).lower() == "transfer_requests":
            value = entry.get("value")
            if isinstance(value, dict):
                transfer_requests = value
            break
    if not transfer_requests:
        return {}

    grants: dict[int, dict[str, int]] = {}
    for request_id, raw_request in transfer_requests.items():
        if not isinstance(raw_request, dict):
            continue
        if clean_text(raw_request.get("status")).lower() != "approved":
            continue
        if clean_text(raw_request.get("type")).lower() != "stones":
            continue

        user_id = parse_legacy_user_id(raw_request.get("userId"))
        if user_id is None:
            report.warn(f"Skipping approved stone transfer {request_id!r}: missing numeric user id.")
            continue

        counts = grants.setdefault(user_id, {})
        raw_stones = raw_request.get("added")
        if not isinstance(raw_stones, list):
            raw_stones = raw_request.get("stones")
        if isinstance(raw_stones, list):
            for stone_name in raw_stones:
                label = pretty_label(stone_name)
                if label:
                    counts[label] = counts.get(label, 0) + 1

        keystone_status = clean_text(raw_request.get("keyStoneStatus")).lower()
        if keystone_status in {"equipped", "owned", "yes", "true"}:
            counts["Mega Ring"] = 1

    return grants


def extract_special_balls(
    raw_balls: dict[str, Any],
    raw_inv: dict[str, Any],
    report: MigrationReport,
) -> tuple[int, int, int, str]:
    normalized_counts: dict[str, int] = {}
    for ball_name, raw_count in raw_balls.items():
        name = clean_text(ball_name).lower()
        amount = parse_int(raw_count, default=0)
        if amount <= 0:
            continue
        if name == "regular":
            normalized_counts["poke"] = amount
            continue
        if name == "great":
            normalized_counts["great"] = amount
            continue
        if name == "ultra":
            normalized_counts["ultra"] = amount
            continue
        normalized = normalize_ball_kind(name)
        if not normalized:
            report.unsupported_ball(name, amount=amount)
            continue
        normalized_counts[normalized] = amount

    for key, raw_value in raw_inv.items():
        normalized = normalize_ball_kind(str(key))
        if not normalized:
            continue
        if normalized in normalized_counts:
            continue
        amount = parse_int(raw_value, default=0)
        if amount <= 0:
            continue
        normalized_counts[normalized] = amount

    raw_ball_list = raw_inv.get("balls")
    if isinstance(raw_ball_list, list):
        ball_list_counts: dict[str, int] = {}
        for value in raw_ball_list:
            normalized = normalize_ball_kind(str(value))
            if not normalized:
                continue
            ball_list_counts[normalized] = ball_list_counts.get(normalized, 0) + 1
        for normalized, amount in ball_list_counts.items():
            normalized_counts.setdefault(normalized, amount)

    poke_balls = normalized_counts.pop("poke", 0)
    great_balls = normalized_counts.pop("great", 0)
    ultra_balls = normalized_counts.pop("ultra", 0)
    extra_counts: dict[str, int] = {}
    for normalized, amount in normalized_counts.items():
        if not normalized:
            continue
        if amount <= 0:
            continue
        if normalized in BALL_FIELDS:
            report.legacy_only(f"balls.{normalized}")
            continue
        extra_counts[normalized] = extra_counts.get(normalized, 0) + amount
    return poke_balls, great_balls, ultra_balls, serialize_extra_ball_counts(extra_counts)


def convert_tm_inventory(raw_tms: Any, data_service: PokemonDataService, report: MigrationReport) -> str:
    if not isinstance(raw_tms, dict):
        if raw_tms not in (None, {}):
            report.legacy_only("data.tms")
        return "{}"
    counts: dict[str, int] = {}
    for move_id, raw_count in raw_tms.items():
        amount = parse_int(raw_count, default=0)
        if amount <= 0:
            continue
        move_name = data_service.move_name_from_id(str(move_id))
        if not move_name:
            move_name = f"TM {move_id}"
            report.unknown_move(f"tm:{move_id}")
        counts[move_name] = counts.get(move_name, 0) + amount
    return json.dumps(dict(sorted(counts.items())), sort_keys=True)


def convert_key_items(
    raw_inv: dict[str, Any],
    raw_extra: dict[str, Any],
    raw_pokemon_list: list[Any] | None = None,
) -> str:
    counts: dict[str, int] = {}
    if raw_inv.get("ring"):
        counts["Mega Ring"] = 1
    if raw_inv.get("omniring"):
        counts["Omni Ring"] = 1
    if raw_inv.get("gmax_band"):
        counts["Gmax Band"] = 1
    if raw_inv.get("shiny_charm"):
        counts["Shiny Charm"] = 1
    for name, field in (
        ("Holowear Ticket", "holowear_tickets"),
        ("Battle Box", "battle_boxes"),
        ("Daycare Candy", "daycare_candy"),
    ):
        amount = parse_int(raw_inv.get(field) if field in raw_inv else raw_extra.get(field), default=0)
        if amount > 0:
            counts[name] = counts.get(name, 0) + amount
    stones = raw_inv.get("stones")
    if isinstance(stones, list):
        for stone in stones:
            label = pretty_label(stone)
            if label:
                counts[label] = counts.get(label, 0) + 1
    for held_item_slug in recover_key_item_slugs_from_pokemon(raw_pokemon_list or []):
        label = pretty_label(held_item_slug)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return json.dumps(dict(sorted(counts.items())), sort_keys=True)


def convert_medicine_inventory(raw_inv: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    rare_candy = parse_int(raw_inv.get("candy"), default=0)
    if rare_candy > 0:
        counts["Rare Candy"] = rare_candy
    vitamin = parse_int(raw_inv.get("vitamin"), default=0)
    if vitamin > 0:
        counts["Vitamin"] = vitamin
    berry = parse_int(raw_inv.get("berry"), default=0)
    if berry > 0:
        counts["Berry"] = berry
    return json.dumps(dict(sorted(counts.items())), sort_keys=True)


def legacy_itembox_payload(raw_extra: dict[str, Any]) -> tuple[str, str, str | None]:
    held_items: dict[str, int] = {}
    key_items: dict[str, int] = {}
    shop_backup: dict[str, Any] = {}

    itembox = raw_extra.get("itembox")
    if isinstance(itembox, dict):
        held_item_map = itembox.get("heldItems")
        if isinstance(held_item_map, dict):
            for item_name, raw_count in held_item_map.items():
                label = pretty_label(item_name)
                amount = parse_int(raw_count, default=0)
                if label and amount > 0:
                    if is_key_item_slug(item_name):
                        key_items[label] = key_items.get(label, 0) + amount
                    else:
                        held_items[label] = held_items.get(label, 0) + amount

        mint_map = itembox.get("mints")
        if isinstance(mint_map, dict):
            for mint_name, raw_count in mint_map.items():
                label = pretty_label(mint_name)
                amount = parse_int(raw_count, default=0)
                if label and amount > 0:
                    label = label if label.endswith(" Mint") else f"{label} Mint"
                    key_items[label] = key_items.get(label, 0) + amount

        z_crystals = itembox.get("zCrystals")
        if isinstance(z_crystals, dict):
            for crystal_name, raw_count in z_crystals.items():
                label = pretty_label(crystal_name)
                amount = parse_int(raw_count, default=0)
                if label and amount > 0:
                    key_items[label] = key_items.get(label, 0) + amount

        for label, field_name in (
            ("Bottle Cap", "bottleCaps"),
            ("Gold Bottle Cap", "goldBottleCaps"),
            ("Ability Capsule", "abilityCapsules"),
            ("Ability Patch", "abilityPatches"),
            ("Max Soup", "maxSoup"),
        ):
            amount = parse_int(itembox.get(field_name), default=0)
            if amount > 0:
                key_items[label] = key_items.get(label, 0) + amount

        if itembox:
            shop_backup["itembox"] = itembox

    for backup_key in ("spinEvent", "tmshop"):
        value = raw_extra.get(backup_key)
        if value not in (None, {}, [], ""):
            shop_backup[backup_key] = value

    shop_state_json = json.dumps({"legacy_backup": shop_backup}, sort_keys=True) if shop_backup else None
    return count_map_to_json(held_items), count_map_to_json(key_items), shop_state_json


def build_stats_snapshot(
    *,
    species: str,
    nature: str,
    level: int,
    ivs: dict[str, int],
    evs: dict[str, int],
    data_service: PokemonDataService,
) -> dict[str, int]:
    temp_pokemon = SimpleNamespace(
        species=species,
        nature=nature,
        level=level,
        iv_hp=ivs["hp"],
        iv_atk=ivs["atk"],
        iv_def=ivs["def"],
        iv_spa=ivs["spa"],
        iv_spd=ivs["spd"],
        iv_spe=ivs["spe"],
        ev_hp=evs["hp"],
        ev_atk=evs["atk"],
        ev_def=evs["def"],
        ev_spa=evs["spa"],
        ev_spd=evs["spd"],
        ev_spe=evs["spe"],
    )
    return data_service.calculate_stats(temp_pokemon)


def build_export_payload(
    *,
    species: str,
    nickname: str | None,
    item: str,
    ability: str,
    moves: list[str],
    nature: str,
    evs: dict[str, int],
    gender: str,
    ivs: dict[str, int],
    shiny: bool,
    level: int,
    friendship: int,
    tera_type: str,
) -> tuple[str, str, str]:
    moves_json = json.dumps(moves)
    packed_set = "|".join([
        nickname or "",
        species,
        item,
        ability,
        ",".join(moves),
        nature,
        ",".join(str(evs[key]) for key in ("hp", "atk", "def", "spa", "spd", "spe")),
        gender,
        ",".join(str(ivs[key]) for key in ("hp", "atk", "def", "spa", "spd", "spe")),
        "S" if shiny else "",
        f"{level},{friendship},{tera_type}",
    ])
    temp_pokemon = SimpleNamespace(
        species=species,
        nickname=nickname,
        item=item,
        ability=ability,
        nature=nature,
        level=level,
        shiny=shiny,
        moves_json=moves_json,
        packed_set=packed_set,
        form_state_json=None,
    )
    return moves_json, build_effective_export_text(temp_pokemon), build_effective_packed_set(temp_pokemon)


def normalized_ivs_or_evs(
    raw_values: Any,
    *,
    field_name: str,
    max_value: int,
    report: MigrationReport,
) -> dict[str, int]:
    source = raw_values if isinstance(raw_values, dict) else {}
    key_map = {
        "hp": "hp",
        "attack": "atk",
        "defense": "def",
        "special_attack": "spa",
        "special_defense": "spd",
        "speed": "spe",
    }
    result: dict[str, int] = {}
    for old_key, new_key in key_map.items():
        raw_amount = source.get(old_key)
        amount = parse_int(raw_amount, default=0)
        if raw_amount is None:
            report.defaulted(f"{field_name}.{new_key}")
        if amount < 0:
            amount = 0
            report.warn(f"Negative {field_name} value encountered for {old_key}; clamped to 0.")
        if amount > max_value:
            report.defaulted(f"{field_name}.{new_key}_clamped")
            amount = max_value
        result[new_key] = amount
    return result


def converted_moves(
    raw_moves: Any,
    *,
    user_id: str,
    species: str,
    source_file: str,
    data_service: PokemonDataService,
    report: MigrationReport,
) -> list[str]:
    resolved: list[str] = []
    if isinstance(raw_moves, list):
        for move in raw_moves:
            if isinstance(move, str) and clean_text(move):
                move_name = pretty_label(move)
            else:
                move_name = data_service.move_name_from_id(move)
            if not move_name:
                report.unknown_move(str(move))
                report.warn(
                    f"[{user_id}] {species}: could not resolve legacy move {move!r} from {source_file}."
                )
                continue
            if move_name not in resolved:
                resolved.append(move_name)
            if len(resolved) >= 4:
                break
    if resolved:
        return resolved
    report.defaulted("owned_pokemon.moves_json")
    report.skipped_pokemon(
        user_id,
        species,
        "No recognizable moves were found; defaulted to Tackle.",
        source_file=source_file,
    )
    return ["Tackle"]


def prepared_pokemon_from_legacy(
    raw_pokemon: dict[str, Any],
    *,
    user_id: str,
    source_file: str,
    current_region: str,
    origin_region: str,
    created_at: datetime | None,
    updated_at: datetime | None,
    index: int,
    data_service: PokemonDataService,
    report: MigrationReport,
) -> PreparedPokemon | None:
    legacy_keys_report(report, "pokemon", raw_pokemon, KNOWN_POKEMON_KEYS)
    species = resolve_species_name(raw_pokemon.get("name"), raw_pokemon.get("id"), data_service)
    if not species:
        original = clean_text(raw_pokemon.get("name")) or f"id:{raw_pokemon.get('id')}"
        report.unknown_species(original)
        report.skipped_pokemon(
            user_id,
            original,
            "Unknown species in current species reference.",
            source_file=source_file,
        )
        return None
    experience = max(0, parse_int(raw_pokemon.get("exp"), default=0))
    level = pokemon_level_from_exp(species, experience, data_service)
    nature = clean_text(raw_pokemon.get("nature")) or "Hardy"
    if not clean_text(raw_pokemon.get("nature")):
        report.defaulted("owned_pokemon.nature")
    ability = pretty_label(raw_pokemon.get("ability")) or "Unknown Ability"
    if not clean_text(raw_pokemon.get("ability")):
        report.defaulted("owned_pokemon.ability")
    ivs = normalized_ivs_or_evs(raw_pokemon.get("ivs"), field_name="ivs", max_value=31, report=report)
    evs = normalized_ivs_or_evs(raw_pokemon.get("evs"), field_name="evs", max_value=252, report=report)
    moves = converted_moves(
        raw_pokemon.get("moves"),
        user_id=user_id,
        species=species,
        source_file=source_file,
        data_service=data_service,
        report=report,
    )
    nickname = clean_text(raw_pokemon.get("nickname")) or None
    if nickname and nickname.lower() == species.lower():
        nickname = None
    item = pretty_label(raw_pokemon.get("held_item"))
    if item.lower() == "none":
        item = ""
    gender = normalize_gender(raw_pokemon.get("gender"))
    if raw_pokemon.get("gender") is None:
        report.defaulted("owned_pokemon.gender")
    friendship = 255
    report.defaulted("owned_pokemon.friendship")
    tera_type = ""
    types = data_service.types_for_species(species)
    if types:
        tera_type = pretty_label(types[0])
    else:
        report.defaulted("owned_pokemon.tera_type")
    shiny = bool(raw_pokemon.get("shiny"))
    if not shiny and clean_text(raw_pokemon.get("symbol")):
        shiny = True
    stats = build_stats_snapshot(
        species=species,
        nature=nature,
        level=level,
        ivs=ivs,
        evs=evs,
        data_service=data_service,
    )
    source_kind = "starter" if index == 0 else "wild"
    if index == 0:
        report.defaulted("owned_pokemon.source_kind")
    moves_json, export_text, packed_set = build_export_payload(
        species=species,
        nickname=nickname,
        item=item,
        ability=ability,
        moves=moves,
        nature=nature,
        evs=evs,
        gender=gender,
        ivs=ivs,
        shiny=shiny,
        level=level,
        friendship=friendship,
        tera_type=tera_type,
    )
    legacy_pass = clean_text(raw_pokemon.get("pass"))
    if not legacy_pass:
        report.skipped_pokemon(
            user_id,
            species,
            "Missing legacy pass value required for team reconstruction.",
            source_file=source_file,
        )
        return None
    payload = {
        "species": species,
        "nickname": nickname,
        "origin_region": origin_region or current_region,
        "source_kind": source_kind,
        "level": level,
        "experience": experience,
        "friendship": friendship,
        "ability": ability,
        "nature": nature,
        "gender": gender,
        "item": item,
        "status": "",
        "tera_type": tera_type,
        "current_hp": max(1, int(stats["hp"])),
        "max_hp": max(1, int(stats["hp"])),
        "shiny": shiny,
        "untradeable": False,
        "unreleasable": False,
        "ivs": ivs,
        "evs": evs,
        "moves_json": moves_json,
        "export_text": export_text,
        "packed_set": packed_set,
        "form_state_json": None,
    }
    return PreparedPokemon(
        legacy_pass=legacy_pass,
        payload=payload,
        created_at=created_at,
        updated_at=updated_at,
    )


def prepare_trainer(
    *,
    path: Path,
    data_service: PokemonDataService,
    report: MigrationReport,
    transfer_request_key_items: dict[int, dict[str, int]] | None = None,
) -> PreparedTrainer | None:
    raw = parse_json_file(path)
    report.bump("user_files_scanned")
    legacy_keys_report(report, "top_level", raw, KNOWN_TOP_LEVEL_KEYS)

    raw_user_id = clean_text(raw.get("userId") or raw.get("user_id") or raw.get("_id") or path.stem)
    try:
        telegram_user_id = int(raw_user_id)
    except ValueError:
        report.bump("users_skipped")
        report.skipped_user(raw_user_id or path.stem, "Invalid numeric Telegram user id.", source_file=path.name)
        return None

    data = raw.get("data")
    if not isinstance(data, dict):
        report.bump("users_skipped")
        report.skipped_user(raw_user_id, "Missing data object.", source_file=path.name)
        return None
    legacy_keys_report(report, "data", data, KNOWN_DATA_KEYS)

    raw_inv = data.get("inv") if isinstance(data.get("inv"), dict) else {}
    raw_extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    raw_balls = data.get("balls") if isinstance(data.get("balls"), dict) else {}
    raw_teams = data.get("teams") if isinstance(data.get("teams"), dict) else {}
    raw_pokemon_list = data.get("pokes") if isinstance(data.get("pokes"), list) else []

    legacy_keys_report(report, "inv", raw_inv, KNOWN_INV_KEYS)
    legacy_keys_report(report, "extra", raw_extra, KNOWN_EXTRA_KEYS)
    if not isinstance(data.get("settings"), dict) and data.get("settings") not in (None, {}):
        report.legacy_only("data.settings")
    elif isinstance(data.get("settings"), dict):
        report.legacy_only("data.settings")
    if isinstance(data.get("pokeseen"), list):
        report.legacy_only("data.pokeseen")

    created_at = parse_legacy_datetime(raw.get("createdAt")) or parse_legacy_datetime(raw_extra.get("date"))
    updated_at = parse_legacy_datetime(raw.get("updatedAt")) or created_at
    current_region = normalize_region(raw_inv.get("region")) or normalize_region(raw_inv.get("hometown")) or "kanto"
    if normalize_region(raw_inv.get("region")) is None and raw_inv.get("region") not in (None, ""):
        report.defaulted("trainer.current_region")
    origin_region = normalize_region(raw_inv.get("hometown")) or current_region
    display_name = clean_text(raw_inv.get("name")) or f"Trainer {telegram_user_id}"
    if not clean_text(raw_inv.get("name")):
        report.defaulted("trainer.display_name")

    poke_balls, great_balls, ultra_balls, special_balls_json = extract_special_balls(raw_balls, raw_inv, report)
    if not raw_balls:
        poke_balls = 10
        report.defaulted("inventory.poke_balls")
        report.defaulted("inventory.great_balls")
        report.defaulted("inventory.ultra_balls")

    prepared_pokemon: list[PreparedPokemon] = []
    for index, raw_pokemon in enumerate(raw_pokemon_list):
        if not isinstance(raw_pokemon, dict):
            report.bump("pokemon_skipped")
            report.skipped_pokemon(raw_user_id, "unknown", "Pokemon entry was not an object.", source_file=path.name)
            continue
        prepared = prepared_pokemon_from_legacy(
            raw_pokemon,
            user_id=raw_user_id,
            source_file=path.name,
            current_region=current_region,
            origin_region=origin_region,
            created_at=created_at,
            updated_at=updated_at,
            index=index,
            data_service=data_service,
            report=report,
        )
        if prepared is None:
            report.bump("pokemon_skipped")
            continue
        prepared_pokemon.append(prepared)

    if not prepared_pokemon:
        report.bump("users_skipped")
        report.skipped_user(raw_user_id, "No valid Pokemon could be prepared from backup.", source_file=path.name)
        return None

    starter_species = prepared_pokemon[0].payload["species"]
    raw_sort_mode = normalize_sort_mode(clean_text(raw_extra.get("sort")))
    raw_display_mode = normalize_display_mode(clean_text(raw_extra.get("display")))
    if raw_extra.get("sort") and raw_sort_mode is None:
        report.defaulted("trainer.sort_mode")
    if raw_extra.get("display") and raw_display_mode is None:
        report.defaulted("trainer.display_mode")

    legacy_daycare = raw_extra.get("daycare")
    daycare_state_json = None
    if legacy_daycare not in (None, {}, [], ""):
        daycare_state_json = json.dumps({"legacy_backup": legacy_daycare}, sort_keys=True)
        report.legacy_only("extra.daycare_preserved")

    held_items_json, itembox_key_items_json, shop_state_json = legacy_itembox_payload(raw_extra)
    safari_entered_at = parse_legacy_datetime(raw_extra.get("lastsafari"))
    pending_move_learning = raw_extra.get("pendingMoveLearn")
    if isinstance(pending_move_learning, (dict, list)):
        pending_move_learning_json = json.dumps(pending_move_learning, sort_keys=True)
    else:
        pending_move_learning_json = None
    current_location = clean_text(raw_extra.get("location")) or None
    sort_order = clean_text(raw_extra.get("sort_order")).lower()
    sort_descending = sort_order != "asc" if sort_order else True

    trainer_fields = {
        "current_region": current_region,
        "current_location": current_location,
        "last_safari_entered_at": safari_entered_at,
        "starter_species": starter_species,
        "gender": None,
        "sort_mode": raw_sort_mode or "none",
        "sort_descending": sort_descending,
        "display_mode": raw_display_mode or "none",
        "challenge_mode": "owned",
        "challenge_generation": 9,
        "battle_visuals": False,
        "started_at": created_at or datetime.utcnow(),
        "trainer_level": trainer_level_from_exp(parse_int(raw_inv.get("exp"), default=0)),
        "trainer_exp": max(0, parse_int(raw_inv.get("exp"), default=0)),
        "total_wins": max(0, parse_int(raw_inv.get("win"), default=0)),
        "total_losses": max(0, parse_int(raw_inv.get("lose"), default=0)),
        "total_caught": max(len(data.get("pokecaught") or []), len(prepared_pokemon)),
        "pending_move_learning": pending_move_learning_json,
        "daycare_state_json": daycare_state_json,
        "eggs_json": None,
        "shop_state_json": shop_state_json,
        "created_at": created_at or datetime.utcnow(),
        "updated_at": updated_at or created_at or datetime.utcnow(),
    }

    inventory_fields = {
        "victory_points": max(0, parse_int(raw_inv.get("pc"), default=0)),
        "season_points": 0,
        "league_points": max(0, parse_int(raw_inv.get("league_points"), default=0)),
        "poke_balls": max(0, poke_balls),
        "great_balls": max(0, great_balls),
        "ultra_balls": max(0, ultra_balls),
        "special_balls_json": special_balls_json,
        "held_items_json": held_items_json,
        "tm_inventory_json": convert_tm_inventory(data.get("tms"), data_service, report),
        "medicine_inventory_json": convert_medicine_inventory(raw_inv),
        "key_items_json": merge_key_item_json_strings(
            convert_key_items(raw_inv, raw_extra, raw_pokemon_list),
            itembox_key_items_json,
            count_map_to_json((transfer_request_key_items or {}).get(telegram_user_id, {})),
        ),
        "egg_energy": 100,
    }
    report.defaulted("inventory.egg_energy")

    teams: dict[int, list[str]] = {}
    for slot_number in range(1, 7):
        raw_team_members = raw_teams.get(str(slot_number), [])
        if not isinstance(raw_team_members, list):
            report.defaulted(f"teams.{slot_number}")
            teams[slot_number] = []
            continue
        teams[slot_number] = [clean_text(value) for value in raw_team_members if clean_text(value)]
    if not any(teams.values()):
        teams[1] = [pokemon.legacy_pass for pokemon in prepared_pokemon[:6]]
        report.defaulted("teams.1")

    active_team_slot = parse_int(raw_inv.get("team"), default=1)
    if active_team_slot not in {1, 2, 3, 4, 5, 6}:
        active_team_slot = 1
        report.defaulted("trainer.active_team_slot")

    report.bump("users_prepared")
    report.bump("pokemon_prepared", amount=len(prepared_pokemon))
    return PreparedTrainer(
        telegram_user_id=telegram_user_id,
        display_name=display_name,
        username=None,
        trainer_fields=trainer_fields,
        inventory_fields=inventory_fields,
        pokemon=prepared_pokemon,
        teams=teams,
        active_team_slot=active_team_slot,
        source_file=path.name,
    )


def assign_party_and_teams(
    *,
    session: Session,
    trainer: Trainer,
    legacy_pass_to_pokemon_id: dict[str, int],
    teams: dict[int, list[str]],
    active_team_slot: int,
    report: MigrationReport,
) -> None:
    trainers = TrainerRepository(session)
    team_repository = TeamRepository(session)
    trainers._ensure_party_slots(trainer)
    team_presets = team_repository.ensure_team_presets(trainer)

    party_members = teams.get(active_team_slot) or teams.get(1) or list(legacy_pass_to_pokemon_id.keys())[:6]
    party_slots = sorted(trainer.party_slots, key=lambda slot: slot.slot_index)
    for slot in party_slots:
        slot.pokemon_id = None
    for slot, legacy_pass in zip(party_slots, party_members[:6], strict=False):
        pokemon_id = legacy_pass_to_pokemon_id.get(legacy_pass)
        if pokemon_id is None:
            report.warn(
                f"[{trainer.telegram_user_id}] Team reference {legacy_pass!r} could not be matched for party slots."
            )
            continue
        slot.pokemon_id = pokemon_id
        report.bump("party_slots_assigned")

    for team in team_presets:
        team.is_active = team.slot_number == active_team_slot
        slots = team_repository.team_slots(team)
        for slot in slots:
            slot.pokemon_id = None
        for slot, legacy_pass in zip(slots, teams.get(team.slot_number, [])[:6], strict=False):
            pokemon_id = legacy_pass_to_pokemon_id.get(legacy_pass)
            if pokemon_id is None:
                report.warn(
                    f"[{trainer.telegram_user_id}] Team {team.slot_number} reference {legacy_pass!r} was missing."
                )
                continue
            slot.pokemon_id = pokemon_id
            report.bump("team_slots_assigned")


def import_prepared_trainer(
    prepared: PreparedTrainer,
    *,
    session: Session,
    replace_existing: bool,
    report: MigrationReport,
) -> None:
    trainers = TrainerRepository(session)
    existing = session.scalar(
        select(Trainer).where(Trainer.telegram_user_id == prepared.telegram_user_id)
    )
    if existing is not None and not replace_existing:
        report.bump("users_skipped_existing")
        report.skipped_user(
            str(prepared.telegram_user_id),
            "Trainer already exists in destination database. Re-run with --replace-existing to overwrite.",
            source_file=prepared.source_file,
        )
        return
    if existing is not None and replace_existing:
        session.delete(existing)
        session.flush()
        report.bump("users_replaced")

    trainer = trainers.ensure_trainer(
        telegram_user_id=prepared.telegram_user_id,
        username=prepared.username,
        display_name=prepared.display_name,
    )
    for field_name, value in prepared.trainer_fields.items():
        setattr(trainer, field_name, value)
    for field_name, value in prepared.inventory_fields.items():
        setattr(trainer.inventory, field_name, value)

    from bot.db.repositories import PokemonRepository

    pokemon_repository = PokemonRepository(session)
    legacy_pass_to_pokemon_id: dict[str, int] = {}
    for pokemon in prepared.pokemon:
        created = pokemon_repository.create_owned_pokemon(trainer=trainer, data=pokemon.payload)
        if pokemon.created_at is not None:
            created.created_at = pokemon.created_at
        if pokemon.updated_at is not None:
            created.updated_at = pokemon.updated_at
        legacy_pass_to_pokemon_id[pokemon.legacy_pass] = created.id
        report.bump("pokemon_imported")

    assign_party_and_teams(
        session=session,
        trainer=trainer,
        legacy_pass_to_pokemon_id=legacy_pass_to_pokemon_id,
        teams=prepared.teams,
        active_team_slot=prepared.active_team_slot,
        report=report,
    )
    report.bump("users_imported")


def dry_run_summary(prepared: PreparedTrainer, report: MigrationReport) -> None:
    report.bump("users_ready_for_import")
    report.bump("pokemon_ready_for_import", amount=len(prepared.pokemon))
    for team_passes in prepared.teams.values():
        for legacy_pass in team_passes[:6]:
            if any(pokemon.legacy_pass == legacy_pass for pokemon in prepared.pokemon):
                report.bump("team_slots_ready")


def create_session_factory(database_url: str):
    normalized_url = _normalize_database_url(database_url)
    engine = create_engine(normalized_url, future=True, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "Destination database must be PostgreSQL. "
            f"Resolved dialect: {engine.dialect.name}"
        )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def process_backup(
    *,
    source: Path,
    session_factory,
    dry_run: bool,
    replace_existing: bool,
    limit_users: int | None,
    report: MigrationReport,
) -> None:
    temp_dir, backup_root = extracted_backup_root(source)
    try:
        data_service = PokemonDataService()
        transfer_request_key_items = load_transfer_request_key_items(backup_root, report)
        user_files = iter_user_files(backup_root, limit_users)
        report.bump("user_files_selected", amount=len(user_files))
        for user_file in user_files:
            try:
                prepared = prepare_trainer(
                    path=user_file,
                    data_service=data_service,
                    report=report,
                    transfer_request_key_items=transfer_request_key_items,
                )
                if prepared is None:
                    continue
                if dry_run:
                    dry_run_summary(prepared, report)
                    continue
                assert session_factory is not None
                with session_factory.begin() as session:
                    import_prepared_trainer(
                        prepared,
                        session=session,
                        replace_existing=replace_existing,
                        report=report,
                    )
            except Exception as exc:
                report.bump("users_failed")
                report.skipped_user(user_file.stem, f"{type(exc).__name__}: {exc}", source_file=user_file.name)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def main() -> None:
    args = parse_args()
    source = resolve_source(args.source)
    report_path = Path(args.report_path).expanduser().resolve() if args.report_path else default_report_path(source)
    report = MigrationReport(source=source, dry_run=bool(args.dry_run))

    try:
        session_factory = None if args.dry_run else create_session_factory(args.database_url)
        process_backup(
            source=source,
            session_factory=session_factory,
            dry_run=bool(args.dry_run),
            replace_existing=bool(args.replace_existing),
            limit_users=args.limit_users,
            report=report,
        )
    except Exception as exc:
        report.set_fatal_error(f"{type(exc).__name__}: {exc}")
        report_path.write_text(report.as_json(), encoding="utf-8")
        raise

    report_path.write_text(report.as_json(), encoding="utf-8")

    counts = report.payload["counts"]
    mode = "Dry run" if args.dry_run else "Import"
    print(f"{mode} complete for backup source: {source}")
    print(f"Report written to: {report_path}")
    print(f"Users prepared: {counts.get('users_prepared', 0)}")
    print(f"Users imported: {counts.get('users_imported', 0)}")
    print(f"Users skipped: {counts.get('users_skipped', 0)}")
    print(f"Users skipped existing: {counts.get('users_skipped_existing', 0)}")
    print(f"Users failed: {counts.get('users_failed', 0)}")
    print(f"Pokemon prepared: {counts.get('pokemon_prepared', 0)}")
    print(f"Pokemon imported: {counts.get('pokemon_imported', 0)}")
    print(f"Pokemon skipped: {counts.get('pokemon_skipped', 0)}")


if __name__ == "__main__":
    main()
