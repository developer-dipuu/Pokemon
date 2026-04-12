"""
fix_inventory_migration.py
==========================
Patches inventory data for trainers that were already imported by the original
migration script.  It does NOT touch Trainers, OwnedPokemon, teams or party
slots – only the Inventory row.

What it fixes
-------------
1. key_items_json  – mega stones, rings, z-crystals, mints, bottle caps, etc.
   The original script's convert_key_items() was correct, but this script
   re-derives the value from scratch using a stricter pipeline and merges it
   with what is already in the DB (union, not overwrite) so nothing that may
   have been granted in-game is lost.

2. medicine_inventory_json – adds Vitamins and Berries (raw counts from
   inv.vitamin and inv.berry) which the original script dropped entirely.
   Rare Candy from inv.candy is preserved as before.

3. held_items_json – re-derives from itembox.heldItems, filtering out items
   that are actually key items (mega stones, z-crystals, rusted items) so
   they land in key_items_json instead of held_items_json.

4. Drops the dead  ('Item Box', 'itembox')  loop entry that was silently
   emitting 0 and could produce a spurious "Item Box: 0" entry.

Run modes
---------
--dry-run   Print what would change without touching the DB.
--source    Path to the backup .zip or extracted directory.
            Defaults to the same stem as the original script.
--report-path  Where to write the JSON report.

Usage
-----
    python fix_inventory_migration.py --source /path/to/backup.zip
    python fix_inventory_migration.py --dry-run
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.config import DATABASE_URL, _normalize_database_url
from bot.db.models import Base, Inventory, Trainer

UTC = timezone.utc
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_SOURCE_STEM = "pokeplay_backup_2026-04-08T16-14-13-602Z"
DEFAULT_SOURCE_DIR = SCRIPT_DIR / DEFAULT_SOURCE_STEM
DEFAULT_SOURCE_ZIP = SCRIPT_DIR / f"{DEFAULT_SOURCE_STEM}.zip"

# ──────────────────────────────────────────────────────────────────────────────
# Key items that should exist at most once (boolean flags)
# ──────────────────────────────────────────────────────────────────────────────
UNIQUE_KEY_ITEM_NAMES: set[str] = {
    "Gmax Band",
    "Mega Ring",
    "Omni Ring",
    "Shiny Charm",
}

# ──────────────────────────────────────────────────────────────────────────────
# Items that live in held_items_json in the new bot schema.
# When they appear inside itembox.heldItems we must NOT move them to
# key_items_json (they are NOT key items despite having "ite" endings).
# ──────────────────────────────────────────────────────────────────────────────
KNOWN_HELD_ITEMS: set[str] = {
    "life-orb", "eviolite", "choice-band", "choice-specs", "choice-scarf",
    "leftovers", "rocky-helmet", "assault-vest", "focus-sash", "shell-bell",
    "weakness-policy", "expert-belt", "iron-ball", "flame-orb", "toxic-orb",
    "black-sludge", "lum-berry", "sitrus-berry", "stone-plate",
    "black-glasses", "charcoal", "mystic-water", "miracle-seed",
    "never-melt-ice", "magnet", "twisted-spoon", "poison-barb",
    "soft-sand", "sharp-beak", "silk-scarf", "silver-powder",
    "spell-tag", "metal-coat", "dragon-fang", "hard-stone",
    "pixie-plate", "power-herb", "white-herb", "kings-rock",
    "scope-lens", "razor-claw", "razor-fang", "bright-powder",
    "quick-claw", "muscle-band", "wise-glasses", "choice-specs",
    "thick-club", "deep-sea-tooth", "deep-sea-scale",
    "light-ball", "lucky-punch", "stick",
    "power-weight", "power-bracer", "power-belt", "power-lens",
    "power-band", "power-anklet",
}


# ──────────────────────────────────────────────────────────────────────────────
# Pure helper functions (no dependencies on bot.* modules)
# ──────────────────────────────────────────────────────────────────────────────

def clean_text(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    return " ".join(text.split())


def pretty_label(value: Any) -> str:
    """Convert a raw stone/item slug to a display label.

    Examples:
        'charizardite-y'  ->  'Charizardite Y'
        'rusted-sword'    ->  'Rusted Sword'
        'timid'           ->  'Timid'
    """
    text = clean_text(value).replace("-", " ").replace("_", " ")
    if not text:
        return ""
    parts = [p for p in text.split() if p]
    return " ".join(p.upper() if len(p) == 1 else p.capitalize() for p in parts)


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def parse_item_counts(raw: str | None) -> dict[str, int]:
    """Mirror of repositories.parse_item_counts – no import needed."""
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in payload.items():
        name = str(key).strip()
        if not name:
            continue
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            counts[name] = amount
    return counts


def serialize_item_counts(counts: dict[str, int]) -> str:
    """Mirror of repositories.serialize_item_counts."""
    cleaned = {
        str(k).strip(): int(v)
        for k, v in counts.items()
        if str(k).strip() and int(v) > 0
    }
    return json.dumps(dict(sorted(cleaned.items())), sort_keys=True)


def merge_key_items(base: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    """
    Merge two key-item count dicts.
    Unique items (rings, charms) are capped at 1.
    Regular items (stones, mints, z-crystals …) are summed.
    """
    merged = dict(base)
    for key, amount in extra.items():
        if key in UNIQUE_KEY_ITEM_NAMES:
            merged[key] = 1 if (amount > 0 or merged.get(key, 0) > 0) else 0
        else:
            merged[key] = merged.get(key, 0) + amount
    return {k: v for k, v in merged.items() if v > 0}


def overlay_key_items(existing: dict[str, int], derived: dict[str, int]) -> dict[str, int]:
    merged = dict(existing)
    for key, amount in derived.items():
        if key in UNIQUE_KEY_ITEM_NAMES:
            merged[key] = 1 if (amount > 0 or merged.get(key, 0) > 0) else 0
        else:
            merged[key] = max(merged.get(key, 0), amount)
    return {k: v for k, v in merged.items() if v > 0}


def is_mega_stone_slug(slug: str) -> bool:
    """Return True if the slug looks like a mega stone / key stone."""
    s = slug.lower().strip()
    if s in KNOWN_HELD_ITEMS:
        return False
    return (
        s.endswith("ite")
        or s.endswith("ite-x")
        or s.endswith("ite-y")
        or s.endswith("-orb")
        or s.startswith("rusted-")
        or s == "baxcaliburite"
    )


def recover_key_item_slugs_from_pokemon(raw_pokemon_list: list[Any]) -> list[str]:
    recovered: list[str] = []
    for raw_pokemon in raw_pokemon_list:
        if not isinstance(raw_pokemon, dict):
            continue
        held_item = clean_text(raw_pokemon.get("held_item")).lower()
        if held_item and held_item != "none" and is_mega_stone_slug(held_item):
            recovered.append(held_item)
    return recovered


# ──────────────────────────────────────────────────────────────────────────────
# Conversion functions
# ──────────────────────────────────────────────────────────────────────────────

def derive_key_items(
    raw_inv: dict[str, Any],
    raw_extra: dict[str, Any],
    raw_pokemon_list: list[Any] | None = None,
) -> dict[str, int]:
    """
    Derive the full key_items dict from legacy inv + extra data.

    Sources (in priority order):
      1. inv.ring / omniring / gmax_band / shiny_charm  → unique flags
      2. inv.stones                                      → mega stone list
      3. inv.holowear_tickets / battle_boxes / daycare_candy
      4. extra.itembox.mints                             → mint counts
      5. extra.itembox.zCrystals                         → z-crystal counts
      6. extra.itembox.bottleCaps / goldBottleCaps / abilityCapsules /
         abilityPatches / maxSoup
      7. extra.itembox.heldItems  – only items that ARE actually key items
         (mega stones, rusted items) not regular held items

    Note: ('Item Box', 'itembox') is intentionally NOT included –
          itembox is a dict, not a count, and its contents are already
          processed in items 4-7 above.
    """
    counts: dict[str, int] = {}

    # ── unique key items from boolean/flag fields ──
    if raw_inv.get("ring"):
        counts["Mega Ring"] = 1
    if raw_inv.get("omniring"):
        counts["Omni Ring"] = 1
    if raw_inv.get("gmax_band"):
        counts["Gmax Band"] = 1
    if raw_inv.get("shiny_charm"):
        counts["Shiny Charm"] = 1

    # ── countable key items stored directly in inv ──
    for item_name, field in (
        ("Holowear Ticket", "holowear_tickets"),
        ("Battle Box",      "battle_boxes"),
        ("Daycare Candy",   "daycare_candy"),
        # NOTE: 'itembox' deliberately omitted – it's a dict, not a count
    ):
        # field may live in inv OR in extra (older schema versions stored
        # daycare_candy in extra)
        raw_val = raw_inv.get(field) if field in raw_inv else raw_extra.get(field)
        amount = parse_int(raw_val, default=0)
        if amount > 0:
            counts[item_name] = counts.get(item_name, 0) + amount

    # ── mega stones from inv.stones list ──
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

    # ── itembox contents ──
    itembox = raw_extra.get("itembox") if isinstance(raw_extra.get("itembox"), dict) else {}

    mint_map = itembox.get("mints")
    if isinstance(mint_map, dict):
        for mint_name, raw_count in mint_map.items():
            label = pretty_label(mint_name)
            amount = parse_int(raw_count, default=0)
            if label and amount > 0:
                # e.g. "Timid" -> store as "Timid Mint" for clarity
                label_full = label if label.endswith(" Mint") else f"{label} Mint"
                counts[label_full] = counts.get(label_full, 0) + amount

    z_crystals = itembox.get("zCrystals")
    if isinstance(z_crystals, dict):
        for crystal_name, raw_count in z_crystals.items():
            label = pretty_label(crystal_name)
            amount = parse_int(raw_count, default=0)
            if label and amount > 0:
                counts[label] = counts.get(label, 0) + amount

    for item_label, field_name in (
        ("Bottle Cap",      "bottleCaps"),
        ("Gold Bottle Cap", "goldBottleCaps"),
        ("Ability Capsule", "abilityCapsules"),
        ("Ability Patch",   "abilityPatches"),
        ("Max Soup",        "maxSoup"),
    ):
        amount = parse_int(itembox.get(field_name), default=0)
        if amount > 0:
            counts[item_label] = counts.get(item_label, 0) + amount

    # ── itembox.heldItems: only move items that are actually key items ──
    held_item_map = itembox.get("heldItems")
    if isinstance(held_item_map, dict):
        for item_name, raw_count in held_item_map.items():
            slug = item_name.lower().strip()
            if not is_mega_stone_slug(slug):
                continue  # regular held item; stays in held_items_json
            label = pretty_label(item_name)
            amount = parse_int(raw_count, default=0)
            if label and amount > 0:
                counts[label] = counts.get(label, 0) + amount

    return {k: v for k, v in counts.items() if v > 0}


def derive_transfer_key_items(kv_path: Path) -> dict[int, dict[str, int]]:
    """
    Load approved stone transfer requests from kv.json and return a mapping of
    telegram_user_id -> {stone_label: count}.
    """
    if not kv_path.exists():
        return {}

    try:
        with kv_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}

    if not isinstance(payload, list):
        return {}

    transfer_requests: dict | None = None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if clean_text(entry.get("_id", "")).lower() == "transfer_requests":
            val = entry.get("value")
            if isinstance(val, dict):
                transfer_requests = val
            break

    if not transfer_requests:
        return {}

    grants: dict[int, dict[str, int]] = {}
    for _req_id, raw_req in transfer_requests.items():
        if not isinstance(raw_req, dict):
            continue
        if clean_text(raw_req.get("status", "")).lower() != "approved":
            continue
        if clean_text(raw_req.get("type", "")).lower() != "stones":
            continue

        raw_uid = raw_req.get("userId")
        if isinstance(raw_uid, bool):
            continue
        try:
            user_id = int(float(raw_uid))
        except (TypeError, ValueError):
            continue

        user_counts = grants.setdefault(user_id, {})

        # Prefer 'added' over 'stones' (added = actually granted list)
        raw_stones = raw_req.get("added")
        if not isinstance(raw_stones, list):
            raw_stones = raw_req.get("stones")
        if isinstance(raw_stones, list):
            for stone_name in raw_stones:
                label = pretty_label(stone_name)
                if label:
                    user_counts[label] = user_counts.get(label, 0) + 1

        # Keystone (Mega Ring) from transfer
        keystone_status = clean_text(raw_req.get("keyStoneStatus", "")).lower()
        if keystone_status in {"equipped", "owned", "yes", "true"}:
            user_counts["Mega Ring"] = 1

    return grants


def derive_medicine(raw_inv: dict[str, Any]) -> dict[str, int]:
    """
    Derive the full medicine inventory from legacy inv data.

    Fields migrated:
      inv.candy  -> Rare Candy
      inv.vitamin -> Vitamin  (was dropped by original script)
      inv.berry   -> Berry    (was dropped by original script)
    """
    counts: dict[str, int] = {}

    candy = parse_int(raw_inv.get("candy"), default=0)
    if candy > 0:
        counts["Rare Candy"] = candy

    vitamin = parse_int(raw_inv.get("vitamin"), default=0)
    if vitamin > 0:
        counts["Vitamin"] = vitamin

    berry = parse_int(raw_inv.get("berry"), default=0)
    if berry > 0:
        counts["Berry"] = berry

    return counts


def derive_held_items(raw_extra: dict[str, Any]) -> dict[str, int]:
    """
    Derive held_items_json from itembox.heldItems, EXCLUDING items that are
    actually key items (mega stones, rusted items) so they don't appear twice.
    """
    counts: dict[str, int] = {}
    itembox = raw_extra.get("itembox") if isinstance(raw_extra.get("itembox"), dict) else {}
    held_item_map = itembox.get("heldItems")
    if not isinstance(held_item_map, dict):
        return counts

    for item_name, raw_count in held_item_map.items():
        slug = item_name.lower().strip()
        if is_mega_stone_slug(slug):
            continue  # key item – handled in derive_key_items
        label = pretty_label(item_name)
        amount = parse_int(raw_count, default=0)
        if label and amount > 0:
            counts[label] = counts.get(label, 0) + amount

    return counts


# ──────────────────────────────────────────────────────────────────────────────
# Backup I/O helpers
# ──────────────────────────────────────────────────────────────────────────────

def find_backup_root(root: Path) -> Path:
    if (root / "users").is_dir():
        return root
    for child in root.iterdir():
        if child.is_dir() and (child / "users").is_dir():
            return child
    raise FileNotFoundError(f"Could not find a backup root with a users/ directory under {root}")


def extracted_backup_root(source: Path) -> tuple[tempfile.TemporaryDirectory | None, Path]:
    if source.is_dir():
        return None, find_backup_root(source)
    if source.suffix.lower() != ".zip":
        raise ValueError(f"Expected a directory or .zip file, got: {source}")
    tmp = tempfile.TemporaryDirectory(prefix="pokeplay-fix-")
    with zipfile.ZipFile(source) as arc:
        arc.extractall(tmp.name)
    return tmp, find_backup_root(Path(tmp.name))


def iter_user_files(backup_root: Path) -> list[Path]:
    users_dir = backup_root / "users"
    return sorted(p for p in users_dir.iterdir() if p.is_file() and p.suffix.lower() == ".json")


def parse_json_file(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def resolve_source(source_arg: str | None) -> Path:
    if source_arg:
        p = Path(source_arg).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Backup source not found: {p}")
        return p
    if DEFAULT_SOURCE_DIR.exists():
        return DEFAULT_SOURCE_DIR
    if DEFAULT_SOURCE_ZIP.exists():
        return DEFAULT_SOURCE_ZIP
    raise FileNotFoundError(
        "No --source given and default backup not found. "
        f"Expected {DEFAULT_SOURCE_DIR} or {DEFAULT_SOURCE_ZIP}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────

class FixReport:
    def __init__(self, *, source: Path, dry_run: bool) -> None:
        self.payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": str(source),
            "dry_run": dry_run,
            "counts": Counter(),
            "warnings": [],
            "patches": [],   # per-user diff summary
            "fatal_error": None,
        }

    def bump(self, key: str, amount: int = 1) -> None:
        self.payload["counts"][key] += amount

    def warn(self, msg: str) -> None:
        if len(self.payload["warnings"]) < 500:
            self.payload["warnings"].append(msg)

    def record_patch(self, user_id: int, field: str, before: str, after: str) -> None:
        if len(self.payload["patches"]) < 2000:
            self.payload["patches"].append({
                "user_id": user_id,
                "field": field,
                "before": before,
                "after": after,
            })

    def as_json(self) -> str:
        serializable = dict(self.payload)
        serializable["counts"] = dict(serializable["counts"])
        return json.dumps(serializable, indent=2, sort_keys=True)


# ──────────────────────────────────────────────────────────────────────────────
# Core patch logic
# ──────────────────────────────────────────────────────────────────────────────

def patch_inventory(
    *,
    session: Session,
    telegram_user_id: int,
    derived_key_items: dict[str, int],
    derived_medicine: dict[str, int],
    derived_held_items: dict[str, int],
    dry_run: bool,
    report: FixReport,
) -> None:
    trainer: Trainer | None = session.scalar(
        select(Trainer).where(Trainer.telegram_user_id == telegram_user_id)
    )
    if trainer is None:
        report.warn(f"[{telegram_user_id}] Not found in DB – skipping (run full migration first).")
        report.bump("users_not_in_db")
        return

    inv: Inventory = trainer.inventory
    if inv is None:
        report.warn(f"[{telegram_user_id}] Trainer has no inventory row – skipping.")
        report.bump("users_no_inventory")
        return

    report.bump("users_found")
    changed = False

    # ── key_items_json ──────────────────────────────────────────────────────
    existing_ki = parse_item_counts(inv.key_items_json)
    # Merge: new derivation takes priority for items it knows about,
    # but preserve anything already in DB that we didn't derive
    # (e.g. items granted in-game since first migration).
    merged_ki = overlay_key_items(existing_ki, derived_key_items)
    new_ki_json = serialize_item_counts(merged_ki)
    if new_ki_json != serialize_item_counts(existing_ki):
        report.record_patch(telegram_user_id, "key_items_json", inv.key_items_json or "{}", new_ki_json)
        if not dry_run:
            inv.key_items_json = new_ki_json
        report.bump("key_items_patched")
        changed = True

    # ── medicine_inventory_json ────────────────────────────────────────────
    existing_med = parse_item_counts(inv.medicine_inventory_json)
    # Only add what's missing – don't reduce in-game earned medicine
    merged_med = dict(existing_med)
    for name, amount in derived_medicine.items():
        if name not in merged_med or merged_med[name] < amount:
            merged_med[name] = amount
    new_med_json = serialize_item_counts(merged_med)
    if new_med_json != serialize_item_counts(existing_med):
        report.record_patch(telegram_user_id, "medicine_inventory_json", inv.medicine_inventory_json or "{}", new_med_json)
        if not dry_run:
            inv.medicine_inventory_json = new_med_json
        report.bump("medicine_patched")
        changed = True

    # ── held_items_json ────────────────────────────────────────────────────
    existing_hi = parse_item_counts(inv.held_items_json)
    # Merge – add missing, don't reduce
    merged_hi = dict(existing_hi)
    for name, amount in derived_held_items.items():
        merged_hi[name] = max(merged_hi.get(name, 0), amount)
    new_hi_json = serialize_item_counts(merged_hi)
    if new_hi_json != serialize_item_counts(existing_hi):
        report.record_patch(telegram_user_id, "held_items_json", inv.held_items_json or "{}", new_hi_json)
        if not dry_run:
            inv.held_items_json = new_hi_json
        report.bump("held_items_patched")
        changed = True

    if changed:
        report.bump("users_patched")
    else:
        report.bump("users_already_correct")


# ──────────────────────────────────────────────────────────────────────────────
# Main processing loop
# ──────────────────────────────────────────────────────────────────────────────

def process_backup(
    *,
    source: Path,
    session_factory,
    dry_run: bool,
    report: FixReport,
) -> None:
    tmp, backup_root = extracted_backup_root(source)
    try:
        transfer_grants = derive_transfer_key_items(backup_root / "kv.json")
        report.bump("transfer_grants_users", amount=len(transfer_grants))

        user_files = iter_user_files(backup_root)
        report.bump("user_files_found", amount=len(user_files))

        for user_file in user_files:
            try:
                raw = parse_json_file(user_file)
            except Exception as exc:
                report.warn(f"[{user_file.stem}] Could not parse JSON: {exc}")
                report.bump("files_parse_error")
                continue

            # Resolve telegram_user_id
            raw_uid = (
                raw.get("userId")
                or raw.get("user_id")
                or raw.get("_id")
                or user_file.stem
            )
            try:
                telegram_user_id = int(float(str(raw_uid).strip()))
            except (TypeError, ValueError):
                report.warn(f"[{user_file.stem}] Cannot parse user id {raw_uid!r} – skipping.")
                report.bump("files_bad_uid")
                continue

            data = raw.get("data")
            if not isinstance(data, dict):
                report.warn(f"[{telegram_user_id}] Missing data object – skipping.")
                report.bump("files_no_data")
                continue

            raw_inv   = data.get("inv")   if isinstance(data.get("inv"),   dict) else {}
            raw_extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
            raw_pokemon_list = data.get("pokes") if isinstance(data.get("pokes"), list) else {}

            # ── derive what should be in DB ──
            derived_ki = derive_key_items(raw_inv, raw_extra, raw_pokemon_list)

            # Merge in any approved stone transfer grants
            transfer_ki = transfer_grants.get(telegram_user_id, {})
            if transfer_ki:
                derived_ki = merge_key_items(derived_ki, transfer_ki)

            derived_med = derive_medicine(raw_inv)
            derived_hi  = derive_held_items(raw_extra)

            # ── apply patch ──
            if dry_run:
                # For dry run we still need a session to read the current DB state
                with session_factory() as session:
                    patch_inventory(
                        session=session,
                        telegram_user_id=telegram_user_id,
                        derived_key_items=derived_ki,
                        derived_medicine=derived_med,
                        derived_held_items=derived_hi,
                        dry_run=True,
                        report=report,
                    )
            else:
                try:
                    with session_factory.begin() as session:
                        patch_inventory(
                            session=session,
                            telegram_user_id=telegram_user_id,
                            derived_key_items=derived_ki,
                            derived_medicine=derived_med,
                            derived_held_items=derived_hi,
                            dry_run=False,
                            report=report,
                        )
                except Exception as exc:
                    report.warn(f"[{telegram_user_id}] DB error: {type(exc).__name__}: {exc}")
                    report.bump("users_db_error")

    finally:
        if tmp is not None:
            tmp.cleanup()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Patch inventory fields (key items, medicine, held items) for already-migrated trainers."
    )
    p.add_argument("--source", default=None, help="Path to backup .zip or extracted directory.")
    p.add_argument("--database-url", default=DATABASE_URL)
    p.add_argument("--report-path", default=None)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read DB and backup; print what would change without writing.",
    )
    return p.parse_args()


def default_report_path(source: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return SCRIPT_DIR / f"fix_inventory_report_{source.stem}_{ts}.json"


def create_session_factory(database_url: str):
    normalized = _normalize_database_url(database_url)
    engine = create_engine(normalized, future=True, pool_pre_ping=True)
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported dialect: {engine.dialect.name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False,
                        expire_on_commit=False, class_=Session)


def main() -> None:
    args = parse_args()
    source = resolve_source(args.source)
    report_path = (
        Path(args.report_path).expanduser().resolve()
        if args.report_path
        else default_report_path(source)
    )

    report = FixReport(source=source, dry_run=bool(args.dry_run))

    try:
        session_factory = create_session_factory(args.database_url)
        process_backup(
            source=source,
            session_factory=session_factory,
            dry_run=bool(args.dry_run),
            report=report,
        )
    except Exception as exc:
        report.payload["fatal_error"] = f"{type(exc).__name__}: {exc}"
        report_path.write_text(report.as_json(), encoding="utf-8")
        raise

    report_path.write_text(report.as_json(), encoding="utf-8")

    counts = report.payload["counts"]
    mode = "DRY RUN" if args.dry_run else "PATCH"
    print(f"\n{'='*60}")
    print(f"  fix_inventory_migration.py  [{mode}]")
    print(f"{'='*60}")
    print(f"  Backup source  : {source}")
    print(f"  Report written : {report_path}")
    print()
    print(f"  User files found       : {counts.get('user_files_found', 0)}")
    print(f"  Users found in DB      : {counts.get('users_found', 0)}")
    print(f"  Users NOT in DB        : {counts.get('users_not_in_db', 0)}")
    print()
    print(f"  Users patched          : {counts.get('users_patched', 0)}")
    print(f"  Users already correct  : {counts.get('users_already_correct', 0)}")
    print(f"  Users with DB errors   : {counts.get('users_db_error', 0)}")
    print()
    print(f"  key_items_json patched     : {counts.get('key_items_patched', 0)}")
    print(f"  medicine patched           : {counts.get('medicine_patched', 0)}")
    print(f"  held_items patched         : {counts.get('held_items_patched', 0)}")
    print(f"  Transfer grant users       : {counts.get('transfer_grants_users', 0)}")
    print(f"{'='*60}\n")

    if args.dry_run:
        print("  This was a DRY RUN – no changes were written to the database.")
        print("  Review the report JSON, then re-run without --dry-run to apply.\n")


if __name__ == "__main__":
    main()
