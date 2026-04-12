from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import random
import re
import secrets
from dataclasses import dataclass
from math import ceil, sqrt
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from telethon import Button
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.types import User
from telethon.utils import get_display_name

from bot.battle.protocol import parse_condition
from bot.config import (
    CATCH_SETTINGS_PATH,
    ENCOUNTER_POOLS_PATH,
    LOCATION_ENCOUNTERS_PATH,
    POKEDEX_REGIONS_PATH,
    RARITY_WEIGHTS_PATH,
    REGIONS_PATH,
    SAFARI_POOLS_PATH,
    SPECIES_REFERENCE_PATH,
    SPECIES_CATCH_RATES_PATH,
    STONES_PATH,
)
from bot.db.models import OwnedPokemon, PartySlot, Trainer
from bot.db.repositories import InventoryRepository, PokemonRepository, TeamRepository, TrainerRepository
from bot.db.session import db_session, run_db_work_async
from bot.game.balls import BALL_MASTER, BALL_ORDER, BALL_POKE, ball_label as format_ball_label, ball_short_label
from bot.game.fusion import (
    active_item_key,
    add_signature_bonus_move,
    effective_moves,
    effective_species,
    ensure_signature_prompt_state,
    load_form_state,
    set_signature_move_slot,
    lookup_species_name,
    signature_moves_for_species,
)
from bot.game.services.encounter_loot import (
    BASE_SHINY_ODDS,
    MEGA_STONE_DROP_ODDS,
    SHINY_CHARM_ITEM,
    SHINY_CHARM_ODDS,
    TERA_SHARD_DROP_ODDS,
    TERA_SHARDS,
    TM_DROP_ODDS,
    TM_DROPS,
    Z_CRYSTAL_DROP_ODDS,
    Z_CRYSTALS,
    mega_stones_for_region,
)
from bot.game.services.medicine import EXP_CANDY_DROP_KEYS, EXP_CANDY_DROP_ODDS, medicine_name
from bot.game.services.pokemon_data import PokemonDataService
from bot.game.services.weekend_boost import weekend_boost_active
from bot.telegram_helpers import resolve_event_user, safe_client_edit, safe_event_edit
from bot.bridge.showdown_bridge import ShowdownBridgeError

if TYPE_CHECKING:
    from bot.battle.models import BattleSession, PlayerState
    from bot.battle.service import BattleService
    from bot.game.services.generator import PokemonGeneratorService


def display_name(user: User | None, fallback: str = "Trainer") -> str:
    if not user:
        return fallback
    value = get_display_name(user).strip()
    return value or fallback


def species_key(name: str) -> str:
    text = name.strip().lower().replace("♀", "-f").replace("♂", "-m")
    text = text.replace(" ", "-").replace(".", "").replace("'", "")
    return re.sub(r"[^a-z0-9-]+", "", text)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def pokemon_display_name(species: str, *, shiny: bool = False) -> str:
    return f"{species}{' ✨' if shiny else ''}"


def is_remote_media(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def parse_spawn_weight(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(float(value), 0.001)
    text = str(value or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return max(float(match.group(1)), 0.001) if match else 1.0


def rarity_bonus_multiplier(spawn_weight: float) -> float:
    weight = max(float(spawn_weight or 1.0), 0.001)
    if weight <= 2:
        return 2.1
    if weight <= 5:
        return 1.85
    if weight <= 10:
        return 1.6
    if weight <= 20:
        return 1.38
    if weight <= 40:
        return 1.22
    if weight <= 70:
        return 1.1
    return 1.0


def adjusted_odds_for_rarity(base_odds: int, *, spawn_weight: float, strength: float = 1.0) -> int:
    """Lower odds divisor for rarer encounters (lower spawn weight)."""
    base = max(int(base_odds), 1)
    clamped_strength = max(0.0, min(float(strength), 1.0))
    bonus = rarity_bonus_multiplier(spawn_weight)
    tuned_bonus = 1.0 + (bonus - 1.0) * clamped_strength
    return max(1, int(round(base / tuned_bonus)))


def wrap_name_list(names: list[str], *, width: int = 88) -> list[str]:
    lines: list[str] = []
    current = ""
    for name in names:
        piece = name if not current else f", {name}"
        if current and len(current) + len(piece) > width:
            lines.append(current)
            current = name
            continue
        current = f"{current}{piece}" if current else name
    if current:
        lines.append(current)
    return lines


def chunk_button_specs(specs: list[tuple[str, str]], *, per_row: int) -> list[list[tuple[str, str]]]:
    return [specs[index:index + per_row] for index in range(0, len(specs), per_row)]


REGIONAL_FORM_OVERRIDES: dict[str, dict[str, str]] = {
    "alola": {
        "geodude": "Geodude-Alola",
        "graveler": "Graveler-Alola",
        "golem": "Golem-Alola",
        "rattata": "Rattata-Alola",
        "raticate": "Raticate-Alola",
        "sandshrew": "Sandshrew-Alola",
        "sandslash": "Sandslash-Alola",
        "vulpix": "Vulpix-Alola",
        "ninetales": "Ninetales-Alola",
        "diglett": "Diglett-Alola",
        "dugtrio": "Dugtrio-Alola",
        "meowth": "Meowth-Alola",
        "persian": "Persian-Alola",
        "grimer": "Grimer-Alola",
        "muk": "Muk-Alola",
        "exeggutor": "Exeggutor-Alola",
        "marowak": "Marowak-Alola",
        "raichu": "Raichu-Alola",
    },
    "galar": {
        "meowth": "Meowth-Galar",
        "ponyta": "Ponyta-Galar",
        "rapidash": "Rapidash-Galar",
        "slowpoke": "Slowpoke-Galar",
        "slowbro": "Slowbro-Galar",
        "farfetchd": "Farfetch'd-Galar",
        "weezing": "Weezing-Galar",
        "mr-mime": "Mr. Mime-Galar",
        "corsola": "Corsola-Galar",
        "zigzagoon": "Zigzagoon-Galar",
        "linoone": "Linoone-Galar",
        "darumaka": "Darumaka-Galar",
        "darmanitan": "Darmanitan-Galar",
        "yamask": "Yamask-Galar",
        "stunfisk": "Stunfisk-Galar",
        "articuno": "Articuno-Galar",
        "zapdos": "Zapdos-Galar",
        "moltres": "Moltres-Galar",
        "slowking": "Slowking-Galar",
    },
    "hisui": {
        "growlithe": "Growlithe-Hisui",
        "arcanine": "Arcanine-Hisui",
        "voltorb": "Voltorb-Hisui",
        "electrode": "Electrode-Hisui",
        "qwilfish": "Qwilfish-Hisui",
        "sneasel": "Sneasel-Hisui",
        "lilligant": "Lilligant-Hisui",
        "zorua": "Zorua-Hisui",
        "zoroark": "Zoroark-Hisui",
        "sliggoo": "Sliggoo-Hisui",
        "goodra": "Goodra-Hisui",
        "avalugg": "Avalugg-Hisui",
        "braviary": "Braviary-Hisui",
    },
    "paldea": {
        "wooper": "Wooper-Paldea",
        "tauros": "Tauros-Paldea-Combat",
    },
}

SAFARI_REGION_FALLBACK_ID = "national"
SAFARI_REGION_FALLBACK_LABEL = "National Safari Reserve"
SAFARI_DEFAULT_BALLS = 30
SAFARI_CATCH_MULTIPLIER = 4.0
BALL_THROW_ANIMATION_DELAY = 1.2
ULTRA_BEAST_KEYS = {
    "nihilego",
    "buzzwole",
    "pheromosa",
    "xurkitree",
    "celesteela",
    "kartana",
    "guzzlord",
    "poipole",
    "naganadel",
    "stakataka",
    "blacephalon",
}
DEXNAV_EXCLUDED_CATEGORIES = {
    "legendaries",
    "mythicals",
    "ultra_beasts",
    "paradox",
    "pseudo_legendaries",
}
CAVE_LOCATION_KEYWORDS = (
    "cave",
    "cavern",
    "tunnel",
    "den",
    "grotto",
    "mine",
    "well",
    "hideout",
    "chamber",
    "catacomb",
    "sewer",
    "ruins",
)


@dataclass
class EncounterSession:
    encounter_id: str
    trainer_user_id: int
    trainer_name: str
    region: str
    location_id: str | None
    location_name: str | None
    source: str
    species: str
    level: int
    generated: dict[str, Any]
    catch_rate: int
    spawn_weight: float = 1.0
    iv_profile: str | None = None
    weekend_boost: bool = False
    message_id: int | None = None
    ball_menu_open: bool = False
    battle_id: str | None = None
    note: str = ""


@dataclass
class SafariState:
    balls_left: int = 30
    region_id: str = SAFARI_REGION_FALLBACK_ID
    region_label: str = SAFARI_REGION_FALLBACK_LABEL


class EncounterService:
    def __init__(
        self,
        generator: "PokemonGeneratorService",
        battle_service: "BattleService",
        data_service: PokemonDataService,
    ) -> None:
        self.generator = generator
        self.battle_service = battle_service
        self.data = data_service
        self.active_by_user: dict[int, EncounterSession] = {}
        self.dexnav_queries: dict[int, str] = {}
        self.safari_sessions: dict[int, SafariState] = {}
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.materialize_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._json_cache: dict[Path, tuple[int, Any]] = {}
        self._pending_move_task: asyncio.Task | None = None
        self.regions = json.loads(Path(REGIONS_PATH).read_text(encoding="utf-8"))["regions"]
        self.locations_per_page = 8
        self.location_data = self._load_location_data()
        self.species_pokedex_numbers = self._load_species_pokedex_numbers()
        self.pokedex_region_numbers = self._load_pokedex_region_numbers()
        self.safari_data = self._load_safari_data()
        self.mega_stones_by_species = self._load_mega_stones_by_species()
        self.dexnav_excluded_species_keys = self._load_dexnav_excluded_species_keys()

    def user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self.user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self.user_locks[user_id] = lock
        return lock

    def _preview_max_hp(self, species: str, level: int) -> int:
        base = self.data.base_stats.get(species_key(lookup_species_name(species)), {})
        base_hp = int(base.get("hp", 1) or 1)
        level_value = max(1, int(level))
        preview_iv = 15
        if base_hp == 1:
            return 1
        return max(1, int(((2 * base_hp + preview_iv) * level_value) / 100) + level_value + 10)

    def _preview_generated_payload(
        self,
        *,
        species: str,
        level: int,
        shiny: bool,
        item: str,
    ) -> dict[str, Any]:
        preview_hp = self._preview_max_hp(species, level)
        return {
            "species": species,
            "level": int(level),
            "shiny": bool(shiny),
            "item": str(item or ""),
            "status": "",
            "types": self.data.types_for_species(species),
            "current_hp": preview_hp,
            "max_hp": preview_hp,
            "current_hp_exact": preview_hp,
            "max_hp_exact": preview_hp,
        }

    def _encounter_is_materialized(self, encounter: EncounterSession) -> bool:
        return bool(str(encounter.generated.get("packed_set") or "").strip())

    async def _materialize_encounter_impl(self, encounter: EncounterSession) -> dict[str, Any]:
        if self._encounter_is_materialized(encounter):
            return encounter.generated

        generated = await self.generator.generate_wild(
            species=encounter.species,
            level=encounter.level,
            region=encounter.region,
            source_kind=encounter.source,
            shiny=bool(encounter.generated.get("shiny")),
            item=str(encounter.generated.get("item") or ""),
            iv_profile=encounter.iv_profile,
            weekend_boost=bool(encounter.weekend_boost),
        )
        merged = dict(encounter.generated)
        merged.update(generated)
        encounter.generated = merged
        encounter.species = str(generated.get("species") or encounter.species)
        encounter.level = int(generated.get("level") or encounter.level)
        encounter.catch_rate = self.catch_rate_for_species(encounter.species)
        return encounter.generated

    def _start_materialize_encounter_task(self, encounter: EncounterSession) -> asyncio.Task[dict[str, Any]]:
        task = self.materialize_tasks.get(encounter.encounter_id)
        if task is not None and not task.done():
            return task

        async def _runner() -> dict[str, Any]:
            return await self._materialize_encounter_impl(encounter)

        task = asyncio.create_task(_runner())

        def _cleanup(_task: asyncio.Task[dict[str, Any]]) -> None:
            self.materialize_tasks.pop(encounter.encounter_id, None)

        task.add_done_callback(_cleanup)
        self.materialize_tasks[encounter.encounter_id] = task
        return task

    async def _ensure_materialized_encounter(self, encounter: EncounterSession) -> dict[str, Any]:
        return await self._start_materialize_encounter_task(encounter)

    def _should_include_artwork(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        shiny: bool = False,
    ) -> bool:
        return bool(getattr(event, "is_private", False))

    def start_background_tasks(self) -> None:
        if self._pending_move_task is None or self._pending_move_task.done():
            self._pending_move_task = asyncio.create_task(self._pending_move_expiry_loop())

    def _load_pending_move_entries(self, trainer: Trainer) -> list[dict[str, Any]]:
        raw = trainer.pending_move_learning
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []

        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return []

        entries: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            pokemon_id = item.get("pokemon_id")
            move_name = str(item.get("move") or "").strip()
            if pokemon_id is None or not move_name:
                continue
            moves = item.get("moves")
            entries.append(
                {
                    "id": str(item.get("id") or secrets.token_hex(6)),
                    "kind": str(item.get("kind") or ""),
                    "pokemon_id": int(pokemon_id),
                    "pokemon_name": str(item.get("pokemon_name") or "Pokemon"),
                    "move": move_name,
                    "moves": list(moves) if isinstance(moves, list) else [],
                    "item_key": str(item.get("item_key") or ""),
                    "remaining_moves": [
                        str(move).strip()
                        for move in list(item.get("remaining_moves") or [])
                        if str(move).strip()
                    ],
                    "expires_at": int(item.get("expires_at") or 0),
                    "chat_id": int(item["chat_id"]) if item.get("chat_id") is not None else None,
                    "message_id": int(item["message_id"]) if item.get("message_id") is not None else None,
                }
            )
        return entries

    def _store_pending_move_entries(self, trainer: Trainer, entries: list[dict[str, Any]]) -> None:
        trainer.pending_move_learning = json.dumps(entries) if entries else None

    def _pending_move_prompt_text(self, entry: dict[str, Any]) -> str:
        lines = [
            f"{entry['pokemon_name']} wants to learn {entry['move']} but it already knows 4 moves!",
            "",
            "Select which move to replace. Time to replace: 10 minutes.",
            "",
        ]
        moves = list(entry.get("moves") or [])
        for index, move_name in enumerate(moves, start=1):
            lines.append(f"{index}. {move_name}")
        return "\n".join(lines)

    def _pending_move_buttons(self, prompt_id: str, move_count: int) -> list[list[Button]]:
        buttons = [
            Button.inline(str(index), data=f"movelearn:{prompt_id}:{index}".encode("utf-8"))
            for index in range(1, max(1, move_count) + 1)
        ]
        return [buttons[index:index + 4] for index in range(0, len(buttons), 4)]

    def _pending_move_expired_text(self, entry: dict[str, Any]) -> str:
        return f"{entry['pokemon_name']} didn't learn {entry['move']}!"

    def _level_up_text(
        self,
        pokemon_name: str,
        old_level: int,
        new_level: int,
        old_stats: dict[str, int],
        new_stats: dict[str, int],
    ) -> str:
        stat_rows = [
            ("hp", "HP"),
            ("atk", "Attack"),
            ("def", "Defense"),
            ("spa", "Sp. Atk"),
            ("spd", "Sp. Def"),
            ("spe", "Speed"),
        ]
        lines = [
            f"🎉 **{pokemon_name} leveled up!**",
            f"📈 **Level:** `{old_level} ➔ {new_level}`",
            "",
            "📊 **Stat Changes:**"
        ]
        for key, label in stat_rows:
            # Aligns the old and new stats neatly
            old_val = int(old_stats.get(key, 0))
            new_val = int(new_stats.get(key, 0))
            lines.append(f"• **{label}:** `{old_val} ➔ {new_val}`")
            
        return "\n".join(lines)

    def _build_progression_message(
        self,
        pokemon_name: str,
        events: list[dict[str, Any]],
        learned_moves: list[str],
        pending_moves: list[str],
    ) -> str | None:
        if not events and not learned_moves:
            return None

        lines: list[str] = []
        if events:
            first_event = events[0]
            last_event = events[-1]
            lines.append(
                self._level_up_text(
                    pokemon_name,
                    int(first_event.get("old_level", 1)),
                    int(last_event.get("level", first_event.get("level", 1))),
                    dict(first_event.get("old_stats") or {}),
                    dict(last_event.get("new_stats") or {}),
                )
            )

        for move_name in learned_moves:
            if lines:
                lines.append("")
            lines.append(f"✨ {pokemon_name} learnt **{move_name}**!")

        return "\n".join(lines) if lines else None
    
    def _move_learn_initial_text(self, entry: dict[str, Any]) -> str:
        return (
            f"🌟 **{entry['pokemon_name']}** wants to learn **{entry['move']}**,\n"
            f"but it already knows 4 moves!\n\n"
            f"Replace an existing move to learn **{entry['move']}**?\n"
            f"__(Expires in 10 minutes)__"
        )

    def _move_learn_initial_buttons(self, prompt_id: str) -> list[list[Button]]:
        return [[
            Button.inline("✅ Learn", data=f"movelearn:start:{prompt_id}".encode("utf-8")),
            Button.inline("❌ No", data=f"movelearn:cancel:{prompt_id}".encode("utf-8")),
        ]]

    def _move_learn_select_text(self, entry: dict[str, Any]) -> str:
        lines = [
            f"Which move should **{entry['pokemon_name']}** forget to learn **{entry['move']}**?",
            "",
        ]
        moves = list(entry.get("moves") or [])
        for index, move_name in enumerate(moves, start=1):
            lines.append(f"`[{index}]` **{move_name}**")
        return "\n".join(lines)

    def _move_learn_select_buttons(self, prompt_id: str, move_count: int) -> list[list[Button]]:
        buttons = [
            Button.inline(str(index), data=f"movelearn:select:{prompt_id}:{index}".encode("utf-8"))
            for index in range(1, max(1, move_count) + 1)
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([Button.inline("⬅️ Back", data=f"movelearn:back:{prompt_id}".encode("utf-8"))])
        return rows

    def _move_learn_confirm_text(self, entry: dict[str, Any], old_move: str) -> str:
        return (
            f"Are you sure you want to replace **{old_move}** with **{entry['move']}**?"
        )

    def _move_learn_confirm_buttons(self, prompt_id: str, slot: int) -> list[list[Button]]:
        return [[
            Button.inline("✅ Yes", data=f"movelearn:apply:{prompt_id}:{slot}".encode("utf-8")),
            Button.inline("⬅️ Back", data=f"movelearn:start:{prompt_id}".encode("utf-8")),
        ]]

    async def _apply_experience_gain(
        self,
        session,
        trainer: Trainer,
        pokemon,
        amount: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if int(amount) <= 0:
            return None, []

        pokemons = PokemonRepository(session)
        events = pokemons.gain_exp(pokemon, int(amount), self.data)
        changed = bool(events)
        learned_moves: list[str] = []
        pending_moves: list[str] = []
        pending_prompts: list[dict[str, Any]] = []
        pokemon_name = pokemon_display_name(pokemon.species, shiny=bool(pokemon.shiny))

        for event in events:
            new_moves = await self.generator.get_levelup_moves(pokemon.species, int(event["level"])) if self.generator else []
            for move_name in new_moves:
                current_moves = list(json.loads(pokemon.moves_json))
                if move_name in current_moves:
                    continue
                if len(current_moves) < 4:
                    current_moves.append(move_name)
                    pokemon.moves_json = json.dumps(current_moves)
                    learned_moves.append(move_name)
                    changed = True
                else:
                    prompt = self._queue_move_prompt(trainer, pokemon, move_name)
                    pending_moves.append(move_name)
                    if prompt is not None:
                        pending_prompts.append(dict(prompt))

        if changed:
            pokemons.sync_packed_set(pokemon, self.data)

        return self._build_progression_message(pokemon_name, events, learned_moves, pending_moves), pending_prompts

    def _queue_move_prompt(
        self,
        trainer: Trainer,
        pokemon,
        move_name: str,
        *,
        moves: list[str] | None = None,
        pokemon_name: str | None = None,
        kind: str = "",
        item_key: str = "",
        remaining_moves: list[str] | None = None,
    ) -> dict[str, Any] | None:
        moves = list(moves) if moves is not None else list(json.loads(pokemon.moves_json))
        if len(moves) < 4 or move_name in moves:
            return None

        entries = self._load_pending_move_entries(trainer)
        for entry in entries:
            if (
                int(entry.get("pokemon_id", 0)) == int(pokemon.id)
                and str(entry.get("move")) == move_name
                and str(entry.get("kind") or "") == str(kind or "")
            ):
                return entry

        entry = {
            "id": secrets.token_hex(6),
            "kind": str(kind or ""),
            "pokemon_id": int(pokemon.id),
            "pokemon_name": str(pokemon_name or pokemon_display_name(pokemon.species, shiny=bool(pokemon.shiny))),
            "move": move_name,
            "moves": moves,
            "item_key": str(item_key or ""),
            "remaining_moves": [str(move).strip() for move in list(remaining_moves or []) if str(move).strip()],
            "expires_at": int((datetime.utcnow() + timedelta(minutes=10)).timestamp()),
            "chat_id": int(trainer.telegram_user_id),
            "message_id": None,
        }
        entries.append(entry)
        self._store_pending_move_entries(trainer, entries)
        return entry

    def queue_fusion_signature_prompts(self, trainer: Trainer, pokemon, *, moves_to_process: list[str] | None = None) -> list[dict[str, Any]]:
        ensure_signature_prompt_state(pokemon)
        remaining = [
            str(move).strip()
            for move in (
                list(moves_to_process)
                if moves_to_process is not None
                else signature_moves_for_species(effective_species(pokemon))
            )
            if str(move).strip()
        ]
        prompts: list[dict[str, Any]] = []

        while remaining:
            current_moves = effective_moves(pokemon)
            current_keys = {str(move).strip().lower() for move in current_moves}
            move_name = remaining.pop(0)
            if move_name.strip().lower() in current_keys:
                continue
            if len(current_moves) < 4:
                add_signature_bonus_move(pokemon, move_name)
                continue

            prompt = self._queue_move_prompt(
                trainer,
                pokemon,
                move_name,
                moves=current_moves,
                pokemon_name=pokemon_display_name(effective_species(pokemon), shiny=bool(pokemon.shiny)),
                kind="fusion_signature",
                item_key=str(active_item_key(pokemon) or ""),
                remaining_moves=remaining,
            )
            if prompt is not None:
                prompts.append(prompt)
            break

        return prompts

    def _active_party_members(self, session, trainer: Trainer) -> list[Any]:
        slots = list(
            session.scalars(
                select(PartySlot).where(PartySlot.trainer_id == trainer.id).order_by(PartySlot.slot_index)
            )
        )
        return [slot.pokemon for slot in slots if slot.pokemon is not None and slot.pokemon.current_hp > 0]

    def _roll_loot_lines(
        self,
        inventories: InventoryRepository,
        trainer: Trainer,
        region_id: str,
        *,
        source_kind: str,
        encounter_weight: float = 1.0,
    ) -> list[str]:
        lines: list[str] = []
        tm_drop_odds, mega_drop_odds, z_drop_odds, shard_drop_odds = self._loot_odds_for_weight(encounter_weight)

        if random.randint(1, tm_drop_odds) == 1:
            tm_name = random.choice(TM_DROPS)
            inventories.add_tm(trainer, tm_name)
            lines.append(f"Found {tm_name}!")

        mega_choices = mega_stones_for_region(region_id)
        if mega_choices and random.randint(1, mega_drop_odds) == 1:
            mega_stone = random.choice(mega_choices)
            inventories.add_item(trainer, mega_stone)
            lines.append(f"Found {mega_stone}!")

        if region_id.lower() == "alola" and random.randint(1, z_drop_odds) == 1:
            z_crystal = random.choice(Z_CRYSTALS)
            inventories.add_item(trainer, z_crystal)
            lines.append(f"Found {z_crystal}!")

        if random.randint(1, shard_drop_odds) == 1:
            tera_shard = random.choice(TERA_SHARDS)
            inventories.add_item(trainer, tera_shard)
            lines.append(f"Found {tera_shard}!")

        if source_kind == "hunt" and random.randint(1, EXP_CANDY_DROP_ODDS) == 1:
            candy_key = random.choice(EXP_CANDY_DROP_KEYS)
            candy_label = medicine_name(candy_key)
            inventories.add_medicine(trainer, candy_label)
            lines.append(f"Found {candy_label}!")

        return lines

    def _loot_odds_for_weight(self, encounter_weight: float) -> tuple[int, int, int, int]:
        tm_drop_odds = adjusted_odds_for_rarity(TM_DROP_ODDS, spawn_weight=encounter_weight, strength=1.0)
        mega_drop_odds = adjusted_odds_for_rarity(MEGA_STONE_DROP_ODDS, spawn_weight=encounter_weight, strength=1.0)
        z_drop_odds = adjusted_odds_for_rarity(Z_CRYSTAL_DROP_ODDS, spawn_weight=encounter_weight, strength=1.0)
        shard_drop_odds = adjusted_odds_for_rarity(TERA_SHARD_DROP_ODDS, spawn_weight=encounter_weight, strength=1.0)
        if weekend_boost_active():
            tm_drop_odds = max(1, int(round(tm_drop_odds / 2)))
            mega_drop_odds = max(1, int(round(mega_drop_odds / 1.5)))
        return tm_drop_odds, mega_drop_odds, z_drop_odds, shard_drop_odds

    def simulate_hunt_report(
        self,
        *,
        region_id: str,
        location_id: str | None,
        hunts: int,
        has_shiny_charm: bool,
    ) -> dict[str, int]:
        total_hunts = max(0, int(hunts))
        results = {
            "hunts": total_hunts,
            "shiny": 0,
            "tms": 0,
            "mega_stone": 0,
            "z_crystal": 0,
            "tera_shards": 0,
            "exp_candy": 0,
            "failed_spawns": 0,
        }
        if total_hunts <= 0:
            return results

        base_shiny_odds = SHINY_CHARM_ODDS if has_shiny_charm else BASE_SHINY_ODDS
        for _ in range(total_hunts):
            entry = self.pick_encounter_entry(region_id, location_id=location_id, source="hunt")
            if entry is None:
                results["failed_spawns"] += 1
                continue
            encounter_weight = max(float(entry.get("weight", 1.0)), 0.001)
            shiny_odds = adjusted_odds_for_rarity(base_shiny_odds, spawn_weight=encounter_weight, strength=0.35)
            if random.randint(1, shiny_odds) == 1:
                results["shiny"] += 1

            tm_odds, mega_odds, z_odds, shard_odds = self._loot_odds_for_weight(encounter_weight)
            if random.randint(1, tm_odds) == 1:
                results["tms"] += 1
            if random.randint(1, mega_odds) == 1:
                results["mega_stone"] += 1
            if region_id.lower() == "alola" and random.randint(1, z_odds) == 1:
                results["z_crystal"] += 1
            if random.randint(1, shard_odds) == 1:
                results["tera_shards"] += 1
            if random.randint(1, EXP_CANDY_DROP_ODDS) == 1:
                results["exp_candy"] += 1

        return results

    async def _award_party_experience(
        self,
        session,
        trainer: Trainer,
        party: list[Any],
        *,
        wild_species: str,
        wild_level: int,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        if not party:
            return [], []

        level_up_messages: list[str] = []
        pending_prompts: list[dict[str, Any]] = []
        base_exp = int((self.data.base_experience(wild_species) * wild_level) / 7)
        gain = int(base_exp / len(party))
        if gain <= 0:
            return [], []

        for pokemon in party:
            message, prompt_entries = await self._apply_experience_gain(session, trainer, pokemon, gain)
            if message:
                level_up_messages.append(message)
            if prompt_entries:
                pending_prompts.extend(prompt_entries)

        return level_up_messages, pending_prompts

    async def _send_progression_followups(
        self,
        user_id: int,
        *,
        level_up_messages: list[str],
        pending_prompts: list[dict[str, Any]],
    ) -> None:
        if level_up_messages:
            chunk = ""
            for message in level_up_messages:
                block = message.strip()
                if not block:
                    continue
                candidate = block if not chunk else f"{chunk}\n\n{block}"
                if len(candidate) > 3500:
                    await self.battle_service.client.send_message(user_id, chunk, parse_mode="md")
                    chunk = block
                else:
                    chunk = candidate
            if chunk:
                await self.battle_service.client.send_message(user_id, chunk, parse_mode="md")

        sent_prompts: dict[str, dict[str, int]] = {}
        for prompt in pending_prompts:
            try:
                message = await self.battle_service.client.send_message(
                    user_id,
                    self._move_learn_initial_text(prompt),
                    buttons=self._move_learn_initial_buttons(str(prompt["id"])),
                    parse_mode="md"
                )
            except Exception:
                continue
            sent_prompts[str(prompt["id"])] = {"chat_id": int(user_id), "message_id": int(message.id)}

        if not sent_prompts:
            return

        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None:
                return
            entries = self._load_pending_move_entries(trainer)
            updated = False
            for entry in entries:
                patch = sent_prompts.get(str(entry.get("id")))
                if patch is None:
                    continue
                entry.update(patch)
                updated = True
            if updated:
                self._store_pending_move_entries(trainer, entries)

    async def _expire_prompt_message(self, prompt: dict[str, Any]) -> None:
        chat_id = prompt.get("chat_id")
        message_id = prompt.get("message_id")
        if chat_id is None or message_id is None:
            return
        await safe_client_edit(
            self.battle_service.client,
            int(chat_id),
            int(message_id),
            self._pending_move_expired_text(prompt),
            buttons=None,
            parse_mode=None,
            link_preview=False,
        )

    async def _expire_due_move_prompts_once(self) -> None:
        expired: list[dict[str, Any]] = []
        now_ts = int(datetime.utcnow().timestamp())
        with db_session() as session:
            trainers = list(session.scalars(select(Trainer).where(Trainer.pending_move_learning.is_not(None))))
            for trainer in trainers:
                entries = self._load_pending_move_entries(trainer)
                if not entries:
                    trainer.pending_move_learning = None
                    continue
                remaining: list[dict[str, Any]] = []
                for entry in entries:
                    if int(entry.get("expires_at") or 0) and int(entry["expires_at"]) <= now_ts:
                        expired.append(dict(entry))
                    else:
                        remaining.append(entry)
                if len(remaining) != len(entries):
                    self._store_pending_move_entries(trainer, remaining)

        for prompt in expired:
            await self._expire_prompt_message(prompt)

    async def _pending_move_expiry_loop(self) -> None:
        while True:
            try:
                await self._expire_due_move_prompts_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)

    def _load_json(self, path: Path) -> Any:
        resolved = Path(path)
        cache_key = resolved.resolve()
        mtime_ns = cache_key.stat().st_mtime_ns
        cached = self._json_cache.get(cache_key)
        if cached and cached[0] == mtime_ns:
            return cached[1]
        payload = json.loads(cache_key.read_text(encoding="utf-8"))
        self._json_cache[cache_key] = (mtime_ns, payload)
        return payload

    def _load_mega_stones_by_species(self) -> dict[str, list[str]]:
        if not Path(STONES_PATH).exists():
            return {}
        raw = self._load_json(Path(STONES_PATH))
        if not isinstance(raw, dict):
            return {}
        mapping: dict[str, list[str]] = {}
        for stone_name, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            species_name = str(payload.get("pokemon") or "").strip()
            stone_text = str(stone_name or "").strip()
            if not species_name or not stone_text:
                continue
            mapping.setdefault(species_key(species_name), []).append(stone_text)
        return {
            key: sorted(set(values), key=str.lower)
            for key, values in mapping.items()
            if values
        }

    def _mega_stones_for_species(self, species: str) -> list[str]:
        direct = list(self.mega_stones_by_species.get(species_key(species), []))
        if direct:
            return direct
        pokedex_number = self.data.pokedex_number(species)
        if pokedex_number is None:
            return []
        canonical_species = self.data.species_by_pokedex_number.get(pokedex_number)
        if not canonical_species:
            return []
        return list(self.mega_stones_by_species.get(species_key(canonical_species), []))

    def _roll_wild_held_item(self, species: str) -> str:
        mega_stones = self._mega_stones_for_species(species)
        if not mega_stones:
            return ""
        if random.randint(1, 100) != 1:
            return ""
        return random.choice(mega_stones)

    def _load_species_pokedex_numbers(self) -> dict[str, int]:
        if not Path(SPECIES_REFERENCE_PATH).exists():
            return {}
        raw = self._load_json(Path(SPECIES_REFERENCE_PATH))
        if not isinstance(raw, dict):
            return {}
        numbers: dict[str, int] = {}
        for key, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            value = payload.get("pokedex_number")
            try:
                numbers[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return numbers

    def _load_pokedex_region_numbers(self) -> dict[str, set[int]]:
        if not Path(POKEDEX_REGIONS_PATH).exists():
            return {}
        raw = self._load_json(Path(POKEDEX_REGIONS_PATH))
        if not isinstance(raw, dict):
            return {}
        mapping: dict[str, set[int]] = {}
        for region_id, species_names in raw.items():
            if not isinstance(species_names, list):
                continue
            numbers: set[int] = set()
            for name in species_names:
                number = self.species_pokedex_numbers.get(species_key(str(name).strip()))
                if number is not None:
                    numbers.add(number)
            mapping[str(region_id)] = numbers
        return mapping

    def _load_safari_data(self) -> dict[str, Any]:
        if not Path(SAFARI_POOLS_PATH).exists():
            return {"meta": {}, "regions": {}}
        raw = self._load_json(Path(SAFARI_POOLS_PATH))
        if not isinstance(raw, dict):
            return {"meta": {}, "regions": {}}
        meta = raw.get("meta")
        regions = raw.get("regions")
        return {
            "meta": meta if isinstance(meta, dict) else {},
            "regions": regions if isinstance(regions, dict) else {},
        }

    def _load_location_data(self) -> dict[str, list[dict[str, Any]]]:
        if not Path(LOCATION_ENCOUNTERS_PATH).exists():
            return {}
        raw = self._load_json(Path(LOCATION_ENCOUNTERS_PATH))
        regions = raw.get("regions", {})
        mapping: dict[str, list[dict[str, Any]]] = {}
        if not isinstance(regions, dict):
            return mapping
        for region_id, payload in regions.items():
            locations = payload.get("locations", []) if isinstance(payload, dict) else payload
            if not isinstance(locations, list):
                continue
            normalized: list[dict[str, Any]] = []
            for location in locations:
                if not isinstance(location, dict):
                    continue
                location_name = str(location.get("name") or "").strip()
                if not location_name:
                    continue
                location_copy = dict(location)
                location_copy["id"] = str(location.get("id") or slugify(location_name))
                location_copy["name"] = location_name
                encounters = location.get("encounters", [])
                normalized_encounters: list[dict[str, Any]] = []
                if isinstance(encounters, list):
                    for encounter in encounters:
                        if not isinstance(encounter, dict):
                            continue
                        encounter_copy = dict(encounter)
                        encounter_copy["species"] = self.apply_regional_form(
                            str(encounter.get("species") or encounter.get("pokemon") or "").strip(),
                            str(region_id),
                        )
                        normalized_encounters.append(encounter_copy)
                if not normalized_encounters:
                    continue
                location_copy["encounters"] = normalized_encounters
                normalized.append(location_copy)
            mapping[str(region_id)] = normalized
        return mapping

    def safari_meta(self) -> dict[str, Any]:
        meta = self.safari_data.get("meta", {})
        return meta if isinstance(meta, dict) else {}

    def safari_region_id(self) -> str:
        return str(self.safari_meta().get("region_id") or SAFARI_REGION_FALLBACK_ID)

    def safari_region_label(self) -> str:
        return str(self.safari_meta().get("region_label") or SAFARI_REGION_FALLBACK_LABEL)

    def default_region_id(self) -> str:
        if self.regions:
            return str(self.regions[0].get("id") or SAFARI_REGION_FALLBACK_ID)
        return SAFARI_REGION_FALLBACK_ID

    def trainer_region_id(self, user_id: int) -> str:
        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is not None and str(trainer.current_region).strip():
                return str(trainer.current_region)
        return self.default_region_id()

    def safari_entry_available_now(self, user_id: int) -> tuple[bool, datetime | None]:
        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None or trainers.safari_available_now(trainer):
                return True, None
            return False, trainers.safari_reset_at(trainer)

    def safari_cooldown_text(self, *, region_label: str, reset_at: datetime | None) -> str:
        if reset_at is None:
            return f"The {region_label} is not available yet."
        return (
            f"You already entered the {region_label} today.\n"
            f"It resets at {reset_at:%Y-%m-%d %H:%M} UTC."
        )

    def safari_zone_label(self, region_id: str) -> str:
        for region in self.regions:
            if region["id"] == region_id:
                return f"{region['label']} Safari Zone"
        if region_id == self.safari_region_id():
            return self.safari_region_label()
        return f"{region_id.title()} Safari Zone"

    def safari_form_region(self, species: str) -> str | None:
        key = species_key(species)
        if "-alola" in key:
            return "alola"
        if "-galar" in key:
            return "galar"
        if "-hisui" in key:
            return "sinnoh"
        if "-paldea" in key or key.endswith(("-combat", "-blaze", "-aqua")):
            return "paldea"
        return None

    def safari_region_pokedex_numbers(self, region_id: str) -> set[int]:
        numbers: set[int] = set()
        for key, values in self.pokedex_region_numbers.items():
            if key == region_id or key.startswith(f"{region_id}-") or key.endswith(f"-{region_id}"):
                numbers.update(values)
        return numbers

    def safari_entry_allowed_in_region(self, species: str, region_id: str) -> bool:
        explicit_region = self.safari_form_region(species)
        if explicit_region is not None:
            return explicit_region == region_id
        allowed_numbers = self.safari_region_pokedex_numbers(region_id)
        if not allowed_numbers:
            return False
        key = species_key(species)
        number = self.species_pokedex_numbers.get(key)
        if number is None:
            number = self.species_pokedex_numbers.get(self.base_species_key(species))
        return number in allowed_numbers if number is not None else False

    def safari_pool_region_id(self, region_id: str | None) -> str:
        regions = self.safari_data.get("regions", {})
        if not isinstance(regions, dict):
            return region_id or self.safari_region_id()
        requested = str(region_id or self.safari_region_id())
        if isinstance(regions.get(requested), list):
            return requested
        fallback = self.safari_region_id()
        if isinstance(regions.get(fallback), list):
            return fallback
        for key, value in regions.items():
            if isinstance(value, list):
                return str(key)
        return requested

    def safari_entries(self, region_id: str | None = None) -> list[dict[str, Any]]:
        regions = self.safari_data.get("regions", {})
        if not isinstance(regions, dict):
            return []
        requested_region_id = str(region_id or self.safari_region_id())
        entries = regions.get(self.safari_pool_region_id(requested_region_id), [])
        if not isinstance(entries, list):
            return []
        return [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("species") or "").strip()
            and self.safari_entry_allowed_in_region(str(entry.get("species") or "").strip(), requested_region_id)
        ]

    def safari_species_names(self, region_id: str | None = None) -> list[str]:
        pool_region_id = self.safari_pool_region_id(region_id)
        requested_region_id = str(region_id or pool_region_id)
        meta_species = self.safari_meta().get("species", [])
        if requested_region_id == pool_region_id == self.safari_region_id() and isinstance(meta_species, list):
            names = [str(name).strip() for name in meta_species if str(name).strip()]
            if names:
                return names
        return sorted({
            str(entry.get("species") or "").strip()
            for entry in self.safari_entries(region_id)
            if str(entry.get("species") or "").strip()
        })

    def safari_category_counts(self, region_id: str | None = None) -> dict[str, int]:
        pool_region_id = self.safari_pool_region_id(region_id)
        requested_region_id = str(region_id or pool_region_id)
        counts = self.safari_meta().get("category_counts", {})
        if requested_region_id == pool_region_id == self.safari_region_id() and isinstance(counts, dict):
            return {str(key): int(value) for key, value in counts.items()}
        category_counts: dict[str, set[str]] = {}
        for entry in self.safari_entries(region_id):
            species = str(entry.get("species") or "").strip()
            if not species:
                continue
            categories = entry.get("categories", [])
            if not isinstance(categories, list):
                continue
            for category in categories:
                category_key = str(category).strip()
                if not category_key:
                    continue
                category_counts.setdefault(category_key, set()).add(species)
        return {key: len(values) for key, values in category_counts.items()}

    def _load_dexnav_excluded_species_keys(self) -> set[str]:
        regions = self.safari_data.get("regions", {})
        if not isinstance(regions, dict):
            return set()

        blocked: set[str] = set()
        for entries in regions.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                categories = entry.get("categories", [])
                if not isinstance(categories, list):
                    continue
                category_keys = {
                    str(category).strip().lower().replace("-", "_").replace(" ", "_")
                    for category in categories
                }
                if not (category_keys & DEXNAV_EXCLUDED_CATEGORIES):
                    continue
                species_name = str(entry.get("species") or "").strip()
                if species_name:
                    blocked.add(species_key(species_name))
        return blocked

    def dexnav_species_allowed(self, species: str) -> bool:
        key = species_key(species)
        return key not in self.dexnav_excluded_species_keys and self.base_species_key(species) not in self.dexnav_excluded_species_keys

    def safari_state(self, user_id: int) -> SafariState | None:
        state = self.safari_sessions.get(user_id)
        if state is None:
            return None
        if state.balls_left <= 0:
            self.safari_sessions.pop(user_id, None)
            return None
        return state

    def safari_context(self, user_id: int) -> tuple[str, str, list[dict[str, Any]], SafariState | None]:
        state = self.safari_state(user_id)
        if state is not None:
            region_id = state.region_id
            region_label = state.region_label
        else:
            region_id = self.trainer_region_id(user_id)
            region_label = self.safari_zone_label(region_id)
        return region_id, region_label, self.safari_entries(region_id), state

    def render_safari_info_text(self, user_id: int) -> str:
        region_id, region_label, entries, state = self.safari_context(user_id)
        if not entries:
            return f"**{region_label} Safari**\n━━━━━━━━━━━━━━━━━━━━━\nNo Safari pool is configured yet."

        meta = self.safari_meta()
        safari_balls = int(meta.get("safari_balls", SAFARI_DEFAULT_BALLS))
        entry_fee = int(meta.get("entry_fee", 0) or 0)
        fee_text = f"`{entry_fee} VP`" if entry_fee > 0 else "`Free`"

        if state is not None:
            return (
                f"**{region_label} Safari**\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "You are currently exploring the Safari Zone.\n\n"
                f"**Safari Balls Remaining:** `{state.balls_left}`\n\n"
                "Use `/hunt` to search for Pokémon, or `/exit` to leave."
            )
        return (
            f"**{region_label} Safari**\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Welcome to the Safari Zone. Here, you can catch rare Pokémon "
            "without battling them. You will be provided with Safari Balls upon entry.\n\n"
            f"**Entry Fee:** {fee_text}\n"
            f"**Provided Ammo:** `{safari_balls} Safari Balls`\n\n"
            "Do you wish to enter?"
        )

    def render_safari_result_text(
        self,
        *,
        user_id: int,
        note: str,
        balls_left: int | None = None,
        region_label: str | None = None,
    ) -> str:
        state = self.safari_state(user_id)
        current_balls = balls_left if balls_left is not None else (state.balls_left if state is not None else 0)
        return f"{note}\n\nsafari balls remaining : {max(0, int(current_balls))}"

    def apply_regional_form(self, species: str, region_id: str) -> str:
        if not species:
            return species
        return REGIONAL_FORM_OVERRIDES.get(region_id, {}).get(species_key(species), species)

    def base_species_key(self, species: str) -> str:
        key = species_key(species)
        for suffix in ("-alola", "-galar", "-hisui", "-paldea", "-combat", "-blaze", "-aqua"):
            if key.endswith(suffix):
                return key[: -len(suffix)]
        return key

    def species_matches_query(self, species: str, query: str) -> bool:
        query_key = species_key(query)
        species_name_key = species_key(species)
        return species_name_key == query_key or self.base_species_key(species) == query_key

    def dexnav_matching_locations(self, region_id: str, query: str) -> list[dict[str, Any]]:
        return [
            location
            for location in self.location_data.get(region_id, [])
            if any(
                self.dexnav_species_allowed(str(encounter.get("species", "")))
                and self.species_matches_query(str(encounter.get("species", "")), query)
                for encounter in location.get("encounters", [])
            )
        ]

    def _region_label(self, region_id: str) -> str:
        for region in self.regions:
            if region["id"] == region_id:
                return region["label"]
        return region_id.title()

    def location_by_id(self, region_id: str, location_id: str | None) -> dict[str, Any] | None:
        if not location_id:
            return None
        for location in self.location_data.get(region_id, []):
            if str(location.get("id")) == location_id:
                return location
        return None

    def fly_buttons(self) -> list[list[Button]]:
        buttons = [Button.inline(region["label"], data=f"fly:set:{region['id']}".encode("utf-8")) for region in self.regions]
        return [buttons[index:index + 3] for index in range(0, len(buttons), 3)]

    def walk_buttons(self, region_id: str, page: int) -> list[list[Button]]:
        locations = self.location_data.get(region_id, [])
        if not locations:
            return []
        total_pages = max(1, ceil(len(locations) / self.locations_per_page))
        page = max(0, min(page, total_pages - 1))
        start = page * self.locations_per_page
        chunk = locations[start:start + self.locations_per_page]
        rows: list[list[Button]] = []
        for index in range(0, len(chunk), 2):
            rows.append([
                Button.inline(
                    str(location["name"]),
                    data=f"walk:set:{region_id}:{location['id']}".encode("utf-8"),
                )
                for location in chunk[index:index + 2]
            ])
        nav: list[Button] = []
        if page > 0:
            nav.append(Button.inline("Previous", data=f"walk:page:{region_id}:{page - 1}".encode("utf-8")))
        if page < total_pages - 1:
            nav.append(Button.inline("Next", data=f"walk:page:{region_id}:{page + 1}".encode("utf-8")))
        if nav:
            rows.append(nav)
        return rows

    def walk_text(self, *, region_id: str, current_location_id: str | None, page: int) -> str:
        locations = self.location_data.get(region_id, [])
        total_areas = len(locations)
        total_pages = max(1, ceil(max(len(locations), 1) / self.locations_per_page))
        page = max(0, min(page, total_pages - 1))
        current = self.location_by_id(region_id, current_location_id)
        current_name = str(current["name"]) if current else "None selected"
        return (
            "Travel Hub\n"
            f"Region: {self._region_label(region_id)}\n"
            f"Current Area: {current_name}\n"
            f"Areas Available: {total_areas}\n"
            f"Page: {page + 1}/{total_pages}\n"
            "\n"
            "Select an area below, then use /hunt."
        )

    def travel_text(self, *, region_id: str, current_location_id: str | None) -> str:
        current = self.location_by_id(region_id, current_location_id)
        current_name = str(current["name"]) if current else "None selected"
        return (
            "Travel Hub\n"
            f"Current Region: {self._region_label(region_id)}\n"
            f"Current Area: {current_name}\n"
            "\n"
            "Select a region to continue."
        )

    def dexnav_matching_regions(self, query: str) -> list[dict[str, Any]]:
        return [
            region
            for region in self.regions
            if self.dexnav_matching_locations(str(region["id"]), query)
        ]

    def dexnav_region_buttons(self, query: str) -> list[list[Button]]:
        buttons = [
            Button.inline(region["label"], data=f"dexnav:region:{region['id']}:0".encode("utf-8"))
            for region in self.dexnav_matching_regions(query)
        ]
        return [buttons[index:index + 3] for index in range(0, len(buttons), 3)]

    def dexnav_area_buttons(self, region_id: str, query: str, page: int) -> list[list[Button]]:
        locations = self.dexnav_matching_locations(region_id, query)
        if not locations:
            return []
        total_pages = max(1, ceil(len(locations) / self.locations_per_page))
        page = max(0, min(page, total_pages - 1))
        start = page * self.locations_per_page
        chunk = locations[start:start + self.locations_per_page]
        rows: list[list[Button]] = []
        for index in range(0, len(chunk), 2):
            rows.append([
                Button.inline(
                    str(location["name"]),
                    data=f"dexnav:area:{region_id}:{location['id']}".encode("utf-8"),
                )
                for location in chunk[index:index + 2]
            ])
        nav: list[Button] = []
        if page > 0:
            nav.append(Button.inline("Previous", data=f"dexnav:region:{region_id}:{page - 1}".encode("utf-8")))
        if page < total_pages - 1:
            nav.append(Button.inline("Next", data=f"dexnav:region:{region_id}:{page + 1}".encode("utf-8")))
        if nav:
            rows.append(nav)
        return rows

    def dexnav_region_text(self, *, query: str) -> str:
        matching_regions = self.dexnav_matching_regions(query)
        return (
            "DexNav Search\n"
            f"Pokemon: {query}\n"
            f"Matching Regions: {len(matching_regions)}\n"
            "\n"
            "Select a region to view matching areas."
        )

    def dexnav_area_text(self, *, region_id: str, query: str, page: int) -> str:
        locations = self.dexnav_matching_locations(region_id, query)
        total_pages = max(1, ceil(max(len(locations), 1) / self.locations_per_page))
        page = max(0, min(page, total_pages - 1))
        matching_areas = len(locations)
        return (
            "DexNav Search\n"
            f"Pokemon: {query}\n"
            f"Region: {self._region_label(region_id)}\n"
            f"Matching Areas: {matching_areas}\n"
            f"Page: {page + 1}/{total_pages}\n"
            "\n"
            "Select an area to inspect spawn details."
        )

    async def show_dexnav_regions(self, event: NewMessage.Event | CallbackQuery.Event, *, query: str) -> None:
        text = self.dexnav_region_text(query=query)
        buttons = self.dexnav_region_buttons(query)
        if isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, text, buttons=buttons)
            if not edited:
                await event.respond(text, buttons=buttons)
            return
        await event.respond(text, buttons=buttons)

    async def show_dexnav_areas(self, event: CallbackQuery.Event, *, region_id: str, query: str, page: int) -> None:
        text = self.dexnav_area_text(region_id=region_id, query=query, page=page)
        buttons = self.dexnav_area_buttons(region_id, query, page)
        edited = await safe_event_edit(event, text, buttons=buttons)
        if not edited:
            await event.respond(text, buttons=buttons)

    async def show_dexnav_area_detail(self, event: CallbackQuery.Event, *, region_id: str, query: str, location: dict[str, Any]) -> None:
        encounters = list(location.get("encounters", []))
        matching = [
            enc
            for enc in encounters
            if self.dexnav_species_allowed(str(enc.get("species", "")))
            and self.species_matches_query(str(enc.get("species", "")), query)
        ]
        all_species = sorted({
            str(enc.get("species", "")).strip()
            for enc in encounters
            if str(enc.get("species", "")).strip()
            and self.dexnav_species_allowed(str(enc.get("species", "")))
        })

        lines = [
            "DexNav Results",
            f"Pokemon: {query}",
            f"Region: {self._region_label(region_id)}",
            f"Area: {location['name']}",
        ]
        if matching:
            lines.extend(["", "Matching Spawns:"])
            for encounter in matching[:6]:
                species_name = str(encounter.get("species", "")).strip()
                min_level = int(encounter.get("min_level", 1))
                max_level = int(encounter.get("max_level", 1))
                rate_raw = str(encounter.get("spawn_rate_raw", encounter.get("spawn_rate", ""))).strip()
                lines.append(f"- {species_name}: {rate_raw} | Lv {min_level}-{max_level}")
        else:
            lines.extend(["", "No matching spawns listed for this Pokemon in this area."])

        if all_species:
            lines.extend(["", "Area Roster: " + ", ".join(all_species[:18])])

        text = "\n".join(lines)
        buttons = self.dexnav_area_buttons(region_id, query, 0)
        edited = await safe_event_edit(event, text, buttons=buttons)
        if not edited:
            await event.respond(text, buttons=buttons)
        image_url = str(location.get("image_url") or "").strip()
        if image_url:
            try:
                await event.respond(text, file=image_url)
            except Exception:
                pass

    def encounter_buttons(self, encounter: EncounterSession) -> list[list[Button]]:
        encounter_id = encounter.encounter_id
        if encounter.source == "safari":
            return [
                [
                    Button.inline("Capture", data=f"enc:{encounter_id}:capture".encode("utf-8")),
                ],
            ]
        return [
            [
                Button.inline("Capture", data=f"enc:{encounter_id}:capture".encode("utf-8")),
                Button.inline("EV Yield", data=f"enc:{encounter_id}:ev".encode("utf-8")),
            ],
        ]

    def safari_throw_buttons(self, encounter_id: str) -> list[list[Button]]:
        return [[
            Button.inline("Throw", data=f"enc:{encounter_id}:safarithrow".encode("utf-8")),
            Button.inline("Leave", data=f"enc:{encounter_id}:safarileave".encode("utf-8")),
        ]]

    def safari_throw_text(self, user_id: int) -> str:
        state = self.safari_state(user_id)
        balls_left = int(state.balls_left if state is not None else 0)
        return f"safari balls remaining : {balls_left}"

    def safari_enter_buttons(self, region_id: str) -> list[list[Button]]:
        return [[
            Button.inline("Enter Safari", data=f"senter:confirm:{region_id}".encode("utf-8")),
            Button.inline("Cancel", data="senter:cancel".encode("utf-8")),
        ]]

    def _travel_text_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> str:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return self.travel_text(region_id=trainer.current_region, current_location_id=trainer.current_location)

    def _walk_context_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, str | None]:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return trainer.current_region, trainer.current_location

    def _spawn_context_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, str, str | None, int, int]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        shiny_odds = SHINY_CHARM_ODDS if inventories.has_item(trainer, SHINY_CHARM_ITEM) else BASE_SHINY_ODDS
        return (
            trainer.display_name,
            trainer.current_region,
            trainer.current_location,
            shiny_odds,
            int(trainer.total_caught or 0),
        )

    def _mark_safari_entry(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> None:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        trainers.mark_safari_entry(trainer, entered_at=datetime.utcnow())

    def _set_region_and_load_walk_context(
        self,
        session,
        *,
        owner_id: int,
        region_id: str,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, str | None]:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        trainers.set_region(trainer, region_id)
        return trainer.current_region, trainer.current_location

    def _current_location_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> str | None:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return trainer.current_location

    def _set_walk_location(
        self,
        session,
        *,
        owner_id: int,
        region_id: str,
        location_id: str,
        username: str | None,
        display_name_value: str,
    ) -> None:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        if trainer.current_region != region_id:
            trainers.set_region(trainer, region_id)
        trainers.set_location(trainer, location_id)

    async def on_travel(self, event: NewMessage.Event) -> None:
        if self.safari_state(event.sender_id) is not None:
            await event.respond("You cannot travel while inside Safari. Exit safari by /exit.")
            return
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        response_text = await run_db_work_async(lambda session: self._travel_text_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(response_text, buttons=self.fly_buttons())

    async def on_fly(self, event: NewMessage.Event) -> None:
        await self.on_travel(event)

    async def on_walk(self, event: NewMessage.Event) -> None:
        if self.safari_state(event.sender_id) is not None:
            await event.respond("You cannot travel while inside Safari. Exit safari by /exit.")
            return
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        region_id, current_location_id = await run_db_work_async(lambda session: self._walk_context_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        if not self.location_data.get(region_id):
            await event.respond(
                f"No walk areas are loaded for {self._region_label(region_id)} yet. Run the Bulbapedia import first."
            )
            return
        await event.respond(
            self.walk_text(region_id=region_id, current_location_id=current_location_id, page=0),
            buttons=self.walk_buttons(region_id, 0),
        )

    async def on_dexnav(self, event: NewMessage.Event) -> None:
        parts = event.raw_text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await event.respond("Usage: /dexnav <pokemon>\nExample: /dexnav pikachu")
            return
        query = " ".join(parts[1].strip().split())
        self.dexnav_queries[event.sender_id] = query
        region_buttons = self.dexnav_region_buttons(query)
        if not region_buttons:
            await event.respond(f"No matching locations found for '{query}'.")
            return
        await self.show_dexnav_regions(event, query=query)

    async def on_hunt(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await event.respond("Use /hunt in DM.")
            return
        lock = self.user_lock(int(event.sender_id or 0))
        if lock.locked():
            return
        async with lock:
            source = "safari" if self.safari_state(event.sender_id) is not None else "hunt"
            await self._spawn_encounter(event, source=source)

    async def on_safari(self, event: NewMessage.Event) -> None:
        async with self.user_lock(event.sender_id):
            region_id, _region_label, entries, state = self.safari_context(event.sender_id)
            if state is None and not entries:
                await event.respond(self.render_safari_info_text(event.sender_id))
                return
            if state is None:
                await event.respond(
                    self.render_safari_info_text(event.sender_id),
                    buttons=self.safari_enter_buttons(region_id),
                )
                return
            await event.respond(self.render_safari_info_text(event.sender_id))

    async def on_senter(self, event: NewMessage.Event) -> None:
        await self.on_safari(event)

    async def on_sexit(self, event: NewMessage.Event) -> None:
        async with self.user_lock(event.sender_id):
            state = self.safari_state(event.sender_id)
            active = self.active_by_user.get(event.sender_id)
            active_safari = active is not None and active.source == "safari"
            if state is None and not active_safari:
                await event.respond("You are not inside the Safari.")
                return
            if active_safari:
                self.active_by_user.pop(event.sender_id, None)
                if active.message_id is not None:
                    await safe_client_edit(
                        self.battle_service.client,
                        event.sender_id,
                        active.message_id,
                        self.render_safari_result_text(
                            user_id=event.sender_id,
                            note="Safari encounter ended with /exit.",
                            balls_left=state.balls_left if state is not None else 0,
                            region_label=state.region_label if state is not None else active.location_name,
                        ),
                        buttons=None,
                        parse_mode="md",
                        link_preview=False,
                    )
            left_label = (
                state.region_label
                if state is not None
                else (active.location_name if active_safari and active.location_name else self.safari_zone_label(self.trainer_region_id(event.sender_id)))
            )
            self.safari_sessions.pop(event.sender_id, None)
            await event.respond(f"You left the {left_label}.")

    async def replace_active_encounter(self, user_id: int, *, reason: str) -> bool:
        active = self.active_by_user.get(user_id)
        if active is None or active.battle_id:
            return False
        self.active_by_user.pop(user_id, None)
        await self.expire_encounter_card(active, reason=reason)
        return True

    async def _spawn_encounter(self, event: NewMessage.Event, *, source: str) -> None:
        if not event.is_private:
            command_name = "hunt" if source == "safari" else source
            await event.respond(f"Use /{command_name} in DM.")
            return
            
        can_hunt, reason = self.battle_service.can_start_hunt(event.sender_id)
        if not can_hunt:
            await event.respond(reason or "Finish your PvP battle first.")
            return
            
        if event.sender_id in self.active_by_user:
            active = self.active_by_user[event.sender_id]
            if active.battle_id:
                await event.respond("Finish your current encounter battle first.")
                return
                
            # CHANGED: Pop the old encounter directly to skip the edit API call
            self.active_by_user.pop(event.sender_id, None)

        sender = await resolve_event_user(event)
        trainer_name, current_region, current_location, shiny_odds, total_caught = await run_db_work_async(
            lambda session: self._spawn_context_payload(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            )
        )

        encounter_region = current_region
        encounter_location_id = current_location
        selected_location = self.location_by_id(current_region, current_location)
        encounter_location_name = str(selected_location["name"]) if selected_location else None

        if source == "hunt":
            if not current_location:
                await event.respond("Pick an area first with /travel.")
                return
            entry = self.pick_encounter_entry(current_region, location_id=current_location, source=source)
        else:
            state = self.safari_state(event.sender_id)
            if state is None:
                await event.respond("Enter Safari first from /safari.")
                return
            encounter_region = state.region_id
            encounter_location_id = None
            selected_location = None
            encounter_location_name = state.region_label
            entry = self.pick_encounter_entry(encounter_region, location_id=None, source=source)

        if entry is None:
            if source == "hunt" and selected_location is not None:
                await event.respond(
                    f"No hunt encounters are configured for {selected_location['name']} in {self._region_label(current_region)} yet."
                )
                return
            if source == "safari":
                await event.respond(f"No Safari encounters are configured for {encounter_location_name or self.safari_zone_label(encounter_region)} yet.")
            else:
                await event.respond(
                    f"No {source} encounters are configured for {self._region_label(current_region)} yet."
                )
            return

        spawn_weight = max(float(entry.get("weight", 1.0)), 0.001)
        shiny_odds = adjusted_odds_for_rarity(shiny_odds, spawn_weight=spawn_weight, strength=0.35)
        level = random.randint(int(entry["min_level"]), int(entry["max_level"]))
        held_item = self._roll_wild_held_item(str(entry["species"]))
        iv_profile = source
        is_shiny = random.randint(1, shiny_odds) == 1
        weekend_boost = weekend_boost_active()
        if source == "hunt":
            boost_tag = "hunt-boost" if random.randint(1, 50) == 1 else "hunt"
            iv_profile = f"{boost_tag}:{max(int(total_caught), 0) + 1}"
        preview_generated = self._preview_generated_payload(
            species=str(entry["species"]),
            level=level,
            shiny=is_shiny,
            item=held_item,
        )

        encounter = EncounterSession(
            encounter_id=secrets.token_hex(4),
            trainer_user_id=event.sender_id,
            trainer_name=trainer_name,
            region=encounter_region,
            location_id=encounter_location_id,
            location_name=encounter_location_name,
            source=source,
            species=str(preview_generated["species"]),
            level=int(preview_generated["level"]),
            generated=preview_generated,
            catch_rate=self.catch_rate_for_species(str(preview_generated["species"])),
            spawn_weight=spawn_weight,
            iv_profile=iv_profile,
            weekend_boost=weekend_boost,
        )
        self.active_by_user[event.sender_id] = encounter

        text = self.render_encounter_text(encounter)
        buttons = self.encounter_buttons(encounter)
        candidates = self.data.artwork_candidates(encounter.species, shiny=bool(encounter.generated.get("shiny")))
        message = None
        reply_to = getattr(getattr(event, "message", None), "id", None)

        if self._should_include_artwork(event, shiny=bool(encounter.generated.get("shiny"))):
            best_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate and (is_remote_media(candidate) or Path(candidate).exists())
                ),
                None,
            )
            if best_candidate is not None:
                try:
                    message = await event.respond(text, file=best_candidate, buttons=buttons, parse_mode="md", reply_to=reply_to)
                except Exception:
                    message = None
        if message is None:
            message = await event.respond(text, buttons=buttons, parse_mode="md", reply_to=reply_to)

        if message is None:
            raise RuntimeError("Failed to send encounter message.")
        encounter.message_id = message.id

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        data = event.data.decode("utf-8")
        if data.startswith("movelearn:"):
            async with self.user_lock(event.sender_id):
                await self._handle_movelearn_callback(event, data)
            return True
        if data.startswith("senter:"):
            async with self.user_lock(event.sender_id):
                await self._handle_senter_callback(event, data)
            return True
        if data.startswith("dexnav:"):
            async with self.user_lock(event.sender_id):
                await self._handle_dexnav_callback(event, data)
            return True
        if data.startswith("fly:"):
            async with self.user_lock(event.sender_id):
                await self._handle_fly_callback(event, data)
            return True
        if data.startswith("walk:"):
            async with self.user_lock(event.sender_id):
                await self._handle_walk_callback(event, data)
            return True
        if data.startswith("enc:"):
            async with self.user_lock(event.sender_id):
                await self._handle_encounter_callback(event, data)
            return True
        return False

    async def _handle_movelearn_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Unknown move action.", alert=True)
            return

        action = parts[1]
        prompt_id = parts[2]
        expired_prompt: dict[str, Any] | None = None
        final_text: str | None = None
        next_prompts: list[dict[str, Any]] = []

        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(event.sender_id)
            if trainer is None:
                await event.answer("Trainer record missing.", alert=True)
                return

            entries = self._load_pending_move_entries(trainer)
            prompt = next((entry for entry in entries if str(entry.get("id")) == prompt_id), None)
            
            if prompt is None:
                await event.answer("That move prompt is no longer active.", alert=True)
                return

            if int(prompt.get("expires_at") or 0) <= int(datetime.utcnow().timestamp()):
                entries = [entry for entry in entries if str(entry.get("id")) != prompt_id]
                self._store_pending_move_entries(trainer, entries)
                expired_prompt = dict(prompt)
                
            else:
                pokemon = pokemons.get_owned_pokemon(trainer, int(prompt["pokemon_id"]))
                if pokemon is None:
                    entries = [entry for entry in entries if str(entry.get("id")) != prompt_id]
                    self._store_pending_move_entries(trainer, entries)
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return

                if action == "cancel":
                    entries = [entry for entry in entries if str(entry.get("id")) != prompt_id]
                    self._store_pending_move_entries(trainer, entries)
                    text = f"🚫 **{prompt['pokemon_name']}** gave up on learning **{prompt['move']}**."
                    await safe_event_edit(event, text, buttons=None, parse_mode="md")
                    await event.answer("Move learning cancelled.")
                    return
                    
                if action == "back":
                    await safe_event_edit(
                        event,
                        self._move_learn_initial_text(prompt),
                        buttons=self._move_learn_initial_buttons(prompt_id),
                        parse_mode="md"
                    )
                    await event.answer()
                    return
                    
                if action == "start":
                    current_moves = list(prompt.get("moves") or effective_moves(pokemon))
                    await safe_event_edit(
                        event,
                        self._move_learn_select_text(prompt),
                        buttons=self._move_learn_select_buttons(prompt_id, len(current_moves)),
                        parse_mode="md"
                    )
                    await event.answer()
                    return
                    
                if action == "select" and len(parts) == 4:
                    slot = int(parts[3])
                    current_moves = list(prompt.get("moves") or effective_moves(pokemon))
                    if slot < 1 or slot > len(current_moves):
                        await event.answer("That move slot is no longer valid.", alert=True)
                        return
                    old_move = current_moves[slot - 1]
                    await safe_event_edit(
                        event,
                        self._move_learn_confirm_text(prompt, old_move),
                        buttons=self._move_learn_confirm_buttons(prompt_id, slot),
                        parse_mode="md"
                    )
                    await event.answer()
                    return

                if action == "apply" and len(parts) == 4:
                    slot = int(parts[3])
                    prompt_kind = str(prompt.get("kind") or "")
                    new_move = str(prompt["move"])

                    if prompt_kind == "fusion_signature":
                        state = load_form_state(pokemon)
                        if str(state.get("kind") or "") != "fusion" or str(active_item_key(pokemon) or "") != str(prompt.get("item_key") or ""):
                            entries = [entry for entry in entries if str(entry.get("id")) != prompt_id]
                            self._store_pending_move_entries(trainer, entries)
                            await event.answer("That fusion is no longer active.", alert=True)
                            return

                        current_moves = list(prompt.get("moves") or effective_moves(pokemon))
                        if slot < 1 or slot > len(current_moves):
                            await event.answer("That move slot is no longer valid.", alert=True)
                            return

                        old_move = str(current_moves[slot - 1])
                        set_signature_move_slot(pokemon, new_move, slot)
                        pokemons.sync_packed_set(pokemon, self.data)
                        final_text = f"✨ **Ta-da!**\n**{prompt['pokemon_name']}** forgot **{old_move}** and learned **{new_move}**!"
                        entries = [entry for entry in entries if str(entry.get("id")) != prompt_id]
                        self._store_pending_move_entries(trainer, entries)
                        next_prompts = self.queue_fusion_signature_prompts(
                            trainer,
                            pokemon,
                            moves_to_process=list(prompt.get("remaining_moves") or []),
                        )
                    else:
                        current_moves = list(json.loads(pokemon.moves_json))
                        if slot < 1 or slot > len(current_moves):
                            await event.answer("That move slot is no longer valid.", alert=True)
                            return

                        old_move = str(current_moves[slot - 1])
                        if new_move not in current_moves:
                            current_moves[slot - 1] = new_move
                            pokemon.moves_json = json.dumps(current_moves)
                            pokemons.sync_packed_set(pokemon, self.data)
                        entries = [entry for entry in entries if str(entry.get("id")) != prompt_id]
                        self._store_pending_move_entries(trainer, entries)
                        final_text = f"✨ **Ta-da!**\n**{prompt['pokemon_name']}** forgot **{old_move}** and learned **{new_move}**!"

        if expired_prompt is not None:
            text = self._pending_move_expired_text(expired_prompt)
            edited = await safe_event_edit(event, text, buttons=None, parse_mode="md")
            if not edited:
                await self._expire_prompt_message(expired_prompt)
            await event.answer("This move prompt expired.", alert=True)
            return

        if final_text is not None:
            edited = await safe_event_edit(event, final_text, buttons=None, parse_mode="md")
            if not edited:
                await event.respond(final_text, buttons=Button.clear(), parse_mode="md")
            if next_prompts:
                await self._send_progression_followups(
                    event.sender_id,
                    level_up_messages=[],
                    pending_prompts=next_prompts,
                )
            await event.answer("Move replaced.")

    async def _handle_senter_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 2:
            await event.answer("Unknown Safari action.", alert=True)
            return

        action = parts[1]
        if action == "cancel":
            await safe_event_edit(event, "Safari entry cancelled.", buttons=None)
            await event.answer("Cancelled.")
            return

        if action != "confirm" or len(parts) != 3:
            await event.answer("Unknown Safari action.", alert=True)
            return

        region_id = parts[2]
        region_label = self.safari_zone_label(region_id)
        if not self.safari_entries(region_id):
            await safe_event_edit(event, f"Safari\nRegion: {region_label}\nNo Safari encounters are configured yet.", buttons=None)
            await event.answer("No Safari encounters are configured yet.", alert=True)
            return

        state = self.safari_state(event.sender_id)
        if state is not None:
            await safe_event_edit(
                event,
                f"You are already inside the {state.region_label}. Use /hunt to explore or /exit to leave.",
                buttons=None,
            )
            await event.answer("Already inside Safari.", alert=True)
            return

        available_now, reset_at = self.safari_entry_available_now(event.sender_id)
        if not available_now:
            await safe_event_edit(
                event,
                self.safari_cooldown_text(region_label=region_label, reset_at=reset_at),
                buttons=None,
            )
            await event.answer("Safari is on cooldown.", alert=True)
            return

        active = self.active_by_user.get(event.sender_id)
        if active is not None and active.battle_id:
            await event.answer("Finish your current encounter battle first.", alert=True)
            return
        if active is not None:
            await self.replace_active_encounter(
                event.sender_id,
                reason="This encounter expired when you entered the Safari.",
            )

        safari_balls = int(self.safari_meta().get("safari_balls", SAFARI_DEFAULT_BALLS))
        sender = await event.get_sender()
        await run_db_work_async(lambda session: self._mark_safari_entry(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        self.safari_sessions[event.sender_id] = SafariState(
            balls_left=safari_balls,
            region_id=region_id,
            region_label=region_label,
        )
        await safe_event_edit(
            event,
            (
                f"You entered the {region_label} with {safari_balls} Safari Balls.\n"
                "Use /hunt to explore or /exit to leave."
            ),
            buttons=None,
        )
        await event.answer("Safari entered.")

    async def _handle_fly_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) != 3 or parts[1] != "set":
            await event.answer("Unknown fly action.", alert=True)
            return
        if self.safari_state(event.sender_id) is not None:
            await event.answer("You cannot travel while inside Safari. Exit safari by /exit.", alert=True)
            return
        sender = await event.get_sender()
        region_id, current_location_id = await run_db_work_async(lambda session: self._set_region_and_load_walk_context(
            session,
            owner_id=int(event.sender_id or 0),
            region_id=parts[2],
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        if not self.location_data.get(region_id):
            await safe_event_edit(
                event,
                f"No travel areas are loaded for {self._region_label(region_id)} yet.",
                buttons=self.fly_buttons(),
            )
            await event.answer("No areas loaded.", alert=True)
            return
        await safe_event_edit(
            event,
            self.walk_text(region_id=region_id, current_location_id=current_location_id, page=0),
            buttons=self.walk_buttons(region_id, 0),
        )
        await event.answer(f"Moved to {self._region_label(region_id)}.")

    async def _handle_walk_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) != 4:
            await event.answer("Unknown walk action.", alert=True)
            return
        if self.safari_state(event.sender_id) is not None:
            await event.answer("You cannot travel while inside Safari. Exit safari by /exit.", alert=True)
            return
        sender = await event.get_sender()
        action, region_id, value = parts[1], parts[2], parts[3]
        if action == "page":
            page = max(int(value), 0)
            current_location_id = await run_db_work_async(lambda session: self._current_location_payload(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            ))
            await safe_event_edit(
                event,
                self.walk_text(region_id=region_id, current_location_id=current_location_id, page=page),
                buttons=self.walk_buttons(region_id, page),
            )
            await event.answer()
            return
        if action != "set":
            await event.answer("Unknown walk action.", alert=True)
            return

        selected = self.location_by_id(region_id, value)
        if selected is None:
            await event.answer("That area is no longer available.", alert=True)
            return

        await run_db_work_async(lambda session: self._set_walk_location(
            session,
            owner_id=int(event.sender_id or 0),
            region_id=region_id,
            location_id=value,
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))

        final_text = f"📍 You have entered **{selected['name']}**, {self._region_label(region_id)}."
        await safe_event_edit(
            event,
            final_text,
            buttons=None,
            parse_mode="md"
        )
        await event.answer(f"Arrived at {selected['name']}.")

    async def _handle_dexnav_callback(self, event: CallbackQuery.Event, data: str) -> None:
        query = self.dexnav_queries.get(event.sender_id)
        if not query:
            await event.answer("Run /dexnav <pokemon> first.", alert=True)
            return

        parts = data.split(":")
        if len(parts) != 4:
            await event.answer("Unknown DexNav action.", alert=True)
            return

        action, region_id, value = parts[1], parts[2], parts[3]
        if action == "region":
            page = max(int(value), 0)
            buttons = self.dexnav_area_buttons(region_id, query, page)
            if not buttons:
                await event.answer(f"{query} is not listed in this region.", alert=True)
                return
            await self.show_dexnav_areas(event, region_id=region_id, query=query, page=page)
            await event.answer()
            return

        if action != "area":
            await event.answer("Unknown DexNav action.", alert=True)
            return

        location = self.location_by_id(region_id, value)
        if location is None:
            await event.answer("That area is no longer available.", alert=True)
            return

        await self.show_dexnav_area_detail(event, region_id=region_id, query=query, location=location)
        await event.answer()

    async def _handle_encounter_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Unknown encounter action.", alert=True)
            return
        encounter = self.active_by_user.get(event.sender_id)
        if encounter is None or encounter.encounter_id != parts[1]:
            await event.answer("That encounter is no longer active.", alert=True)
            return
        action = parts[2]

        if action == "ev":
            if encounter.source == "safari":
                await event.answer("Safari encounters only allow capture.", alert=True)
                return
            await event.answer(self.data.ev_yield_text(encounter.species), alert=True)
            return
        if action == "run":
            if encounter.source == "safari":
                await event.answer("Use /exit to leave the safari.", alert=True)
                return
            await event.answer("Run is only available after you start a battle.", alert=True)
            return
        if action == "capture" and encounter.source == "safari":
            await event.respond(
                self.safari_throw_text(event.sender_id),
                buttons=self.safari_throw_buttons(encounter.encounter_id),
                parse_mode="md",
            )
            await event.answer("Choose throw or leave.")
            return
        if action == "safarileave":
            self.active_by_user.pop(event.sender_id, None)
            await safe_event_edit(event, "You left this safari encounter.", buttons=None, parse_mode="md")
            await event.answer("Encounter left.")
            return
        if action in {"safari", "safarithrow"}:
            if encounter.source != "safari":
                await event.answer("Safari Ball can only be used in Safari.", alert=True)
                return
            throw_plan = self._plan_safari_throw_outcome(encounter)
            materialize_task = (
                self._start_materialize_encounter_task(encounter)
                if bool(throw_plan.get("success"))
                else None
            )
            await self.animate_encounter_ball_throw(
                event,
                encounter,
                "Safari Ball",
                shake_count=int(throw_plan.get("shake_count") or 3),
            )
            outcome = await self.attempt_safari_ball(
                event.sender_id,
                encounter,
                planned_outcome=throw_plan,
                materialize_task=materialize_task,
            )
            if outcome["keep_active"]:
                await safe_event_edit(
                    event,
                    outcome["text"],
                    buttons=self.safari_throw_buttons(encounter.encounter_id),
                    parse_mode="md",
                )
                await event.answer("Safari Ball failed.")
                return
            await safe_event_edit(
                event,
                outcome["text"],
                buttons=outcome.get("buttons"),
                parse_mode="md",
            )
            await event.answer(outcome["answer"])
            return
        if action in {"battle", "capture"}:
            if encounter.source == "safari":
                await event.answer("Safari encounters only allow safari throws.", alert=True)
                return
            
            # --- NEW: Prevent clicking 'Battle' multiple times on the same message ---
            if encounter.battle_id:
                await event.answer("You are already battling this Pokemon!", alert=True)
                return

            can_hunt, reason = self.battle_service.can_start_hunt(event.sender_id)
            if not can_hunt:
                await event.answer(reason or "Finish your PvP battle first.", alert=True)
                return
            
            sender = await event.get_sender()
            with db_session() as session:
                trainers = TrainerRepository(session)
                teams = TeamRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(sender, "username", None),
                    display_name=encounter.trainer_name,
                )
                active_team = teams.get_active_team(trainer)
                packed = teams.build_packed_team(active_team)
                member_ids = [slot.pokemon_id for slot in teams.team_slots(active_team) if slot.pokemon_id is not None]
            
            if not packed or not member_ids:
                await event.answer("Set up your active team in /myteam first.", alert=True)
                return
            
            # --- CHANGED: We removed the safe_event_edit here so the original card stays untouched ---
            
            # Send the new battle menu down below
            battle_message = await event.respond("Preparing wild battle...")
            encounter.message_id = battle_message.id
            
            try:
                await self._ensure_materialized_encounter(encounter)
                battle = await self.battle_service.start_wild_encounter(
                    encounter=encounter,
                    packed_team=packed,
                    owned_team_ids=member_ids,
                )
            except Exception as exc:
                self.active_by_user.pop(event.sender_id, None)
                err_str = str(exc)
                if (
                    "unknown species" in err_str.lower()
                    or "invalid set" in err_str.lower()
                    or "unknown move" in err_str.lower()
                    or "unknown ability" in err_str.lower()
                ):
                    await event.answer(f"Oops! The wild {encounter.species} just ran away!", alert=True)
                else:
                    await safe_event_edit(event, f"Encounter battle failed.\n{err_str}", buttons=None)
                    await event.answer("Battle startup failed.", alert=True)
                return
            
            encounter.battle_id = battle.battle_id
            await event.answer("Battle started.")
            return
        if action == "throw" and len(parts) == 4:
            result_text = await self.attempt_menu_throw_ball(event.sender_id, parts[3], encounter)
            await safe_event_edit(
                event,
                self.render_encounter_text(encounter),
                buttons=self.encounter_buttons(encounter) if event.sender_id in self.active_by_user else None,
                parse_mode="md",
            )
            await event.answer(result_text)
            return
        await event.answer("Unknown encounter action.", alert=True)

    def _legacy_ball_use_line(self, ball_name: str, *, shake_count: int = 3) -> str:
        stars = "".join("\u2B50" for _ in range(max(1, shake_count)))
        return f"You used {ball_name}\n{stars}"

    async def _legacy_animate_encounter_ball_throw(self, event: CallbackQuery.Event, encounter: EncounterSession, ball_name: str) -> None:
        for shake_count in range(1, 4):
            # Only show the minimal star line, removing the massive Safari text block
            minimal_text = self.ball_use_line(ball_name, shake_count=shake_count)
            
            await safe_event_edit(
                event,
                minimal_text,
                buttons=None,
                parse_mode="md",
            )
            # Sleep for 1.2 seconds per shake to safely avoid Telegram Flood limits
            await asyncio.sleep(1.2)

    def render_encounter_text(self, encounter: EncounterSession, *, note: str | None = None) -> str:
        display_species = pokemon_display_name(encounter.species, shiny=bool(encounter.generated.get("shiny")))
        
        # Use formatted_types to get full type names with emojis, separated by " / "
        type_text = self.data.formatted_types(lookup_species_name(encounter.species))
        type_suffix = f" ({type_text})" if type_text and type_text != "Unknown" else ""
        
        lines: list[str] = [f"A wild **{display_species}**{type_suffix} (Lv. {encounter.level}) has appeared!"]
        current_note = encounter.note if note is None else note
        if current_note:
            lines.append("")
            lines.append(current_note)
        return "\n".join(lines)

    async def expire_encounter_card(self, encounter: EncounterSession, *, reason: str) -> None:
        if encounter.message_id is None:
            return
        await safe_client_edit(
            self.battle_service.client,
            encounter.trainer_user_id,
            encounter.message_id,
            reason,
            buttons=None,
            parse_mode=None,
            link_preview=False,
        )

    async def exit_user_state(self, user_id: int, *, actor_name: str) -> str | None:
        messages: list[str] = []
        encounter = self.active_by_user.pop(user_id, None)
        if encounter is not None:
            if encounter.battle_id:
                battle = self.battle_service.battles_by_id.get(encounter.battle_id)
                if battle is None:
                    if encounter.message_id is not None:
                        await safe_client_edit(
                            self.battle_service.client,
                            user_id,
                            encounter.message_id,
                            "Encounter cleared with /exit.",
                            buttons=None,
                            parse_mode=None,
                            link_preview=False,
                        )
                    messages.append("Cleared a stale encounter battle.")
                else:
                    async with battle.lock:
                        battle.finished = True
                        battle.metadata["encounter_outcome"] = "exit"
                        battle.metadata["encounter_note"] = f"{actor_name} cleared the encounter with /exit."
                        await self.battle_service._edit_message(
                            battle.chat_id,
                            battle.public_message_id,
                            "Encounter closed with /exit.",
                            buttons=None,
                        )
                        self.battle_service.battles_by_id.pop(battle.battle_id, None)
                        if battle.bridge is not None:
                            await battle.bridge.close()
                    messages.append("Closed your encounter battle.")
            else:
                if encounter.message_id is not None:
                    await safe_client_edit(
                        self.battle_service.client,
                        user_id,
                        encounter.message_id,
                        "Encounter cleared with /exit.",
                        buttons=None,
                        parse_mode=None,
                        link_preview=False,
                    )
                messages.append("Cleared your active encounter.")

        safari_state = self.safari_state(user_id)
        if safari_state is not None:
            self.safari_sessions.pop(user_id, None)
            messages.append(f"You left the {safari_state.region_label}.")

        return "\n".join(messages) if messages else None

    def _pick_weighted_entry(self, weighted_entries: list[tuple[dict[str, Any], float]]) -> dict[str, Any] | None:
        if not weighted_entries:
            return None
        total_weight = sum(weight for _, weight in weighted_entries)
        pick = random.uniform(0, total_weight)
        running = 0.0
        for entry, weight in weighted_entries:
            running += weight
            if pick <= running:
                return entry
        return weighted_entries[-1][0]

    def pick_encounter_entry(self, region_id: str, *, location_id: str | None, source: str) -> dict[str, Any] | None:
        if source == "safari":
            region_entries = self.safari_entries(region_id)
            if not region_entries:
                return None
            weighted_entries = [
                (entry, max(float(entry.get("weight", 1)), 0.001))
                for entry in region_entries
            ]
            return self._pick_weighted_entry(weighted_entries)

        pools = self._load_json(ENCOUNTER_POOLS_PATH)["regions"]
        region_entries = pools.get(region_id) or []
        rarity_data = self._load_json(RARITY_WEIGHTS_PATH) if Path(RARITY_WEIGHTS_PATH).exists() else {"default": 1.0, "species": {}}
        default_rarity = float(rarity_data.get("default", 1.0))
        rarity_by_species = rarity_data.get("species", {})

        weighted_entries: list[tuple[dict[str, Any], float]] = []

        # Hunt encounters now use a region-wide pool, with location species heavily boosted.
        if source == "hunt" and location_id:
            by_species: dict[str, dict[str, Any]] = {}
            for entry in region_entries:
                species = str(entry.get("species") or "").strip()
                if not species:
                    continue
                key = species_key(species)
                rarity_factor = float(rarity_by_species.get(key, default_rarity))
                by_species[key] = {
                    "species": species,
                    "min_level": int(entry.get("min_level", 1)),
                    "max_level": int(entry.get("max_level", 1)),
                    "weight": max(float(entry.get("weight", 1)), 0.001) * max(rarity_factor, 0.0001),
                }

            location = self.location_by_id(region_id, location_id)
            location_entries = list(location.get("encounters", [])) if location else []
            total_location_weight = sum(
                parse_spawn_weight(item.get("spawn_rate", item.get("spawn_rate_raw", 1)))
                for item in location_entries
            ) or 1.0

            for entry in location_entries:
                species = str(entry.get("species") or entry.get("pokemon") or "").strip()
                if not species:
                    continue
                level_range = entry.get("level_range") or [entry.get("min_level", 1), entry.get("max_level", 1)]
                min_level = int(entry.get("min_level", level_range[0]))
                max_level = int(entry.get("max_level", level_range[-1]))
                location_weight = parse_spawn_weight(entry.get("spawn_rate", entry.get("spawn_rate_raw", 1)))
                boost_multiplier = 2.5 + (7.5 * (location_weight / total_location_weight))
                key = species_key(species)
                existing = by_species.get(key)
                if existing is None:
                    by_species[key] = {
                        "species": species,
                        "min_level": min_level,
                        "max_level": max_level,
                        "weight": max(location_weight * 4.0, 0.001),
                    }
                    continue
                existing["min_level"] = min_level
                existing["max_level"] = max_level
                existing["weight"] = max(float(existing["weight"]) * boost_multiplier, 0.001)

            weighted_entries = [
                (
                    {
                        "species": str(entry["species"]),
                        "min_level": int(entry["min_level"]),
                        "max_level": int(entry["max_level"]),
                        "weight": max(float(entry["weight"]), 0.001),
                    },
                    max(float(entry["weight"]), 0.001),
                )
                for entry in by_species.values()
            ]

            # If region pool is unavailable, fall back to route-only spawns.
            if not weighted_entries and location_entries:
                for entry in location_entries:
                    species = str(entry.get("species") or entry.get("pokemon") or "").strip()
                    if not species:
                        continue
                    level_range = entry.get("level_range") or [entry.get("min_level", 1), entry.get("max_level", 1)]
                    min_level = int(entry.get("min_level", level_range[0]))
                    max_level = int(entry.get("max_level", level_range[-1]))
                    weight = parse_spawn_weight(entry.get("spawn_rate", entry.get("spawn_rate_raw", 1)))
                    weighted_entries.append((
                        {
                            "species": species,
                            "min_level": min_level,
                            "max_level": max_level,
                            "weight": weight,
                        },
                        weight,
                    ))
        else:
            for entry in region_entries:
                species = str(entry.get("species") or "").strip()
                if not species:
                    continue
                key = species_key(species)
                rarity_factor = float(rarity_by_species.get(key, default_rarity))
                weight = max(float(entry.get("weight", 1)), 0.001) * max(rarity_factor, 0.0001)
                weighted_entries.append((entry, weight))

        return self._pick_weighted_entry(weighted_entries)

    def catch_rate_for_species(self, species: str) -> int:
        data = self._load_json(SPECIES_CATCH_RATES_PATH)
        return int(data.get("species", {}).get(species_key(species), data.get("default", 120)))

    def trainer_level_up_text(self, reward: dict[str, Any], *, prefix: str = "\n", inline: bool = False) -> str:
        if not reward.get("leveled_up"):
            return ""
        base = f"Trainer leveled up to Lv. {reward['new_level']}!"
        lines = [str(line).strip() for line in list(reward.get("level_reward_lines") or []) if str(line).strip()]
        if inline:
            if not lines:
                return f"{prefix}🎉 Lv. {reward['new_level']}!"
            return f"{prefix}🎉 Lv. {reward['new_level']}! Rewards: " + ", ".join(lines)
        if not lines:
            return f"{prefix}{base}"
        return (
            f"{prefix}{base}\n"
            "Level Rewards:\n"
            + "\n".join(f"- {line}" for line in lines)
        )

    def safari_flee_chance(self, catch_rate: int) -> float:
        if catch_rate <= 3:
            return 0.45
        if catch_rate <= 30:
            return 0.35
        if catch_rate <= 60:
            return 0.28
        if catch_rate <= 120:
            return 0.22
        return 0.16

    def failed_ball_flee_chance(self, catch_rate: int) -> float:
        if catch_rate <= 3:
            return 0.22
        if catch_rate <= 30:
            return 0.18
        if catch_rate <= 60:
            return 0.14
        if catch_rate <= 120:
            return 0.1
        return 0.07

    def _failed_throw_shake_count(self) -> int:
        return random.randint(1, 3)

    def _planned_throw_shake_count(self, *, success: bool) -> int:
        return 3 if success else self._failed_throw_shake_count()

    def _plan_safari_throw_outcome(self, encounter: EncounterSession) -> dict[str, Any]:
        success = self.roll_catch(
            current_hp=int(encounter.generated["current_hp"]),
            max_hp=int(encounter.generated["max_hp"]),
            status=str(encounter.generated.get("status", "")),
            catch_rate=int(encounter.catch_rate),
            ball_kind=BALL_POKE,
            species=encounter.species,
            species_types=self._encounter_types(encounter),
            wild_level=encounter.level,
            turn_number=1,
            source=encounter.source,
            location_name=encounter.location_name,
            ball_multiplier_override=SAFARI_CATCH_MULTIPLIER,
        )
        fled = (not success) and (random.random() <= self.safari_flee_chance(int(encounter.catch_rate)))
        return {
            "success": success,
            "fled": fled,
            "shake_count": self._planned_throw_shake_count(success=success),
        }

    def _plan_battle_throw_outcome(self, battle: "BattleSession", ball_kind: str) -> dict[str, Any]:
        owner_id = int(battle.metadata["owner_user_id"])
        encounter = self.active_by_user.get(owner_id)
        if encounter is None:
            return {"error": "This encounter is no longer active.", "shake_count": 1}

        wild = battle.public_view.active.get("p2") or {}
        current_hp = wild.get("current_hp") if wild.get("current_hp") is not None else encounter.generated["current_hp"]
        max_hp = wild.get("max_hp") if wild.get("max_hp") is not None else encounter.generated["max_hp"]
        status = str(wild.get("status") or encounter.generated.get("status", ""))

        with db_session(read_only=True) as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(owner_id)
            if trainer is None:
                return {"error": "Trainer record missing.", "shake_count": 1}
            success = self.roll_catch(
                current_hp=int(current_hp),
                max_hp=int(max_hp),
                status=status,
                catch_rate=int(encounter.catch_rate),
                ball_kind=ball_kind,
                species=encounter.species,
                species_types=self._encounter_types(encounter),
                wild_level=encounter.level,
                turn_number=max(int(battle.public_view.turn or 0), 1),
                source=encounter.source,
                location_name=encounter.location_name,
                player_level=self._battle_player_level(battle),
                owned_before=self._trainer_owns_species(pokemons, trainer, encounter.species),
            )

        fled = (not success) and (random.random() <= self.failed_ball_flee_chance(int(encounter.catch_rate)))
        return {
            "success": success,
            "fled": fled,
            "shake_count": self._planned_throw_shake_count(success=success),
            "current_hp": int(current_hp),
            "max_hp": int(max_hp),
            "status": status,
        }

    async def _attempt_safari_ball_impl(
        self,
        user_id: int,
        encounter: EncounterSession,
        *,
        planned_outcome: dict[str, Any] | None = None,
        materialize_task: asyncio.Task[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = self.safari_state(user_id)
        throw_line = self.ball_use_line("Safari Ball")
        if state is None:
            self.active_by_user.pop(user_id, None)
            return {
                "keep_active": False,
                "text": self.render_safari_result_text(
                    user_id=user_id,
                    note="Safari session ended.",
                    balls_left=0,
                ),
                "answer": "Safari session ended.",
            }

        state.balls_left = max(state.balls_left - 1, 0)
        success = bool(planned_outcome.get("success")) if planned_outcome is not None else self.roll_catch(
            current_hp=int(encounter.generated["current_hp"]),
            max_hp=int(encounter.generated["max_hp"]),
            status=str(encounter.generated.get("status", "")),
            catch_rate=int(encounter.catch_rate),
            ball_kind=BALL_POKE,
            species=encounter.species,
            species_types=self._encounter_types(encounter),
            wild_level=encounter.level,
            turn_number=1,
            source=encounter.source,
            location_name=encounter.location_name,
            ball_multiplier_override=SAFARI_CATCH_MULTIPLIER,
        )
        if success:
            reward: dict[str, Any] | None = None
            level_up_messages: list[str] = []
            pending_prompts: list[dict[str, Any]] = []
            loot_lines: list[str] = []
            caught_name = pokemon_display_name(encounter.species, shiny=bool(encounter.generated.get("shiny")))
            try:
                await (materialize_task or self._ensure_materialized_encounter(encounter))
            except ShowdownBridgeError:
                self.active_by_user.pop(user_id, None)
                return {
                    "keep_active": False,
                    "text": f"{throw_line}\nThe wild {encounter.species} slipped away before the catch could be confirmed.",
                    "answer": "Catch validation failed.",
                }
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.get_by_telegram_user_id(user_id)
                if trainer is None:
                    self.active_by_user.pop(user_id, None)
                    return {
                        "keep_active": False,
                        "text": self.render_safari_result_text(
                            user_id=user_id,
                            note=f"{throw_line}\nTrainer record missing.",
                            balls_left=state.balls_left,
                        ),
                        "answer": "Trainer record missing.",
                    }

                catch_data = dict(encounter.generated)
                catch_data["source_kind"] = encounter.source
                catch_data["origin_region"] = encounter.region
                owned = pokemons.create_owned_pokemon(trainer=trainer, data=catch_data)
                caught_name = pokemon_display_name(owned.species, shiny=bool(owned.shiny))
                reward = trainers.award_wild_outcome(trainer, wild_level=encounter.level, caught=True)
                level_up_messages, pending_prompts = await self._award_party_experience(
                    session,
                    trainer,
                    self._active_party_members(session, trainer),
                    wild_species=encounter.species,
                    wild_level=encounter.level,
                )
                loot_lines = self._roll_loot_lines(
                    inventories,
                    trainer,
                    encounter.region,
                    source_kind=encounter.source,
                    encounter_weight=encounter.spawn_weight,
                )

            self.active_by_user.pop(user_id, None)
            await self._send_progression_followups(
                user_id,
                level_up_messages=level_up_messages,
                pending_prompts=pending_prompts,
            )
            balls_left = state.balls_left
            reward_line = f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP"
            level_up_line = self.trainer_level_up_text(reward, prefix="\n")
            loot_line = ("\n" + "\n".join(loot_lines)) if loot_lines else ""

            if balls_left <= 0:
                self.safari_sessions.pop(user_id, None)
                note = (
                    f"🎉 **You caught {owned.species}!**\n"
                    f"{reward_line}{level_up_line}{loot_line}\n\n"
                    f"🎒 That was your last Safari Ball. Safari session ended."
                )
            else:
                note = (
                    f"🎉 **You caught {owned.species}!**\n"
                    f"{reward_line}{level_up_line}{loot_line}\n\n"
                    f"🎒 Safari Balls left: {balls_left}"
                )
            return {
                "keep_active": False,
                "text": note,
                "answer": f"{owned.species} was caught.",
                "buttons": [[
                    Button.inline("Stats", data=f"pstats:pick:{user_id}:{owned.id}".encode("utf-8")),
                    Button.inline("Release", data=f"pstats:release:{user_id}:{owned.id}".encode("utf-8")),
                ]]
            }

        if state.balls_left <= 0:
            self.active_by_user.pop(user_id, None)
            self.safari_sessions.pop(user_id, None)
            return {
                "keep_active": False,
                "text": f"💥 Oh no! {encounter.species} broke free!\n\n🎒 You are out of Safari Balls. Session ended.",
                "answer": "Out of Safari Balls.",
            }

        fled = bool(planned_outcome.get("fled")) if planned_outcome is not None else (
            random.random() <= self.safari_flee_chance(int(encounter.catch_rate))
        )
        if fled:
            self.active_by_user.pop(user_id, None)
            return {
                "keep_active": False,
                "text": f"💨 Oh no! {encounter.species} broke free and fled!\n\n🎒 Safari Balls left: {state.balls_left}",
                "answer": f"{encounter.species} fled.",
            }

        encounter.note = f"💥 {encounter.species} broke free!\n\n🎒 Safari Balls left: {state.balls_left}"
        return {
            "keep_active": True,
            "text": encounter.note,
            "answer": "Safari Ball failed.",
        }

    async def _attempt_menu_throw_ball_impl(self, user_id: int, ball_kind: str, encounter: EncounterSession) -> str:
        ball_name = self.ball_label(ball_kind)
        throw_line = self.ball_use_line(ball_name)
        level_up_messages: list[str] = []
        pending_prompts: list[dict[str, Any]] = []
        loot_lines: list[str] = []
        caught_name = pokemon_display_name(encounter.species, shiny=bool(encounter.generated.get("shiny")))
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None:
                return "Trainer record missing."
            if not inventories.consume_ball(trainer, ball_kind):
                return "You are out of that ball."

            success = self.roll_catch(
                current_hp=int(encounter.generated["current_hp"]),
                max_hp=int(encounter.generated["max_hp"]),
                status=str(encounter.generated.get("status", "")),
                catch_rate=int(encounter.catch_rate),
                ball_kind=ball_kind,
                species=encounter.species,
                species_types=self._encounter_types(encounter),
                wild_level=encounter.level,
                turn_number=1,
                source=encounter.source,
                location_name=encounter.location_name,
                player_level=self._trainer_catch_lead_level(session, trainer),
                owned_before=self._trainer_owns_species(pokemons, trainer, encounter.species),
            )
            if not success:
                encounter.ball_menu_open = False
                if random.random() <= self.failed_ball_flee_chance(int(encounter.catch_rate)):
                    encounter.note = f"{throw_line}\n{ball_name} failed.\nWild {encounter.species} fled !!"
                    self.active_by_user.pop(user_id, None)
                    return f"Wild {encounter.species} fled !!"
                encounter.note = f"{throw_line}\n{ball_name} failed."
                return f"{ball_name} failed."

            catch_data = dict(encounter.generated)
            catch_data["source_kind"] = encounter.source
            catch_data["origin_region"] = encounter.region
            owned = pokemons.create_owned_pokemon(trainer=trainer, data=catch_data)
            caught_name = pokemon_display_name(owned.species, shiny=bool(owned.shiny))
            reward = trainers.award_wild_outcome(trainer, wild_level=encounter.level, caught=True)
            level_up_messages, pending_prompts = await self._award_party_experience(
                session,
                trainer,
                self._active_party_members(session, trainer),
                wild_species=encounter.species,
                wild_level=encounter.level,
            )
            loot_lines = self._roll_loot_lines(
                inventories,
                trainer,
                encounter.region,
                source_kind=encounter.source,
                encounter_weight=encounter.spawn_weight,
            )

        self.active_by_user.pop(user_id, None)
        await self._send_progression_followups(
            user_id,
            level_up_messages=level_up_messages,
            pending_prompts=pending_prompts,
        )
        level_up_line = self.trainer_level_up_text(reward, prefix="\n")
        loot_line = ("\n" + "\n".join(loot_lines)) if loot_lines else ""
        encounter.note = (
            f"{throw_line}\nYou caught {caught_name} with a {ball_name}!\n"
            f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP{level_up_line}{loot_line}"
        )
        return f"{caught_name} was caught."

    async def attempt_safari_ball(
        self,
        user_id: int,
        encounter: EncounterSession,
        *,
        planned_outcome: dict[str, Any] | None = None,
        materialize_task: asyncio.Task[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await self._attempt_safari_ball_impl(
            user_id,
            encounter,
            planned_outcome=planned_outcome,
            materialize_task=materialize_task,
        )
        state = self.safari_state(user_id)
        throw_line = self.ball_use_line("Safari Ball")
        if state is None:
            self.active_by_user.pop(user_id, None)
            return {
                "keep_active": False,
                "text": self.render_safari_result_text(
                    user_id=user_id,
                    note=f"{throw_line}\nYour Safari session is no longer active. Use /safari to enter again.",
                    balls_left=0,
                ),
                "answer": "Safari session ended.",
            }

        state.balls_left = max(state.balls_left - 1, 0)
        success = self.roll_catch(
            current_hp=int(encounter.generated["current_hp"]),
            max_hp=int(encounter.generated["max_hp"]),
            status=str(encounter.generated.get("status", "")),
            catch_rate=int(encounter.catch_rate),
            ball_kind=BALL_POKE,
            ball_multiplier_override=SAFARI_CATCH_MULTIPLIER,
        )
        if success:
            with db_session() as session:
                trainers = TrainerRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.get_by_telegram_user_id(user_id)
                if trainer is None:
                    self.active_by_user.pop(user_id, None)
                    return {
                        "keep_active": False,
                        "text": self.render_safari_result_text(
                            user_id=user_id,
                            note=f"{throw_line}\nTrainer record missing.",
                            balls_left=state.balls_left,
                        ),
                        "answer": "Trainer record missing.",
                    }
                catch_data = dict(encounter.generated)
                catch_data["source_kind"] = encounter.source
                catch_data["origin_region"] = encounter.region
                owned = pokemons.create_owned_pokemon(trainer=trainer, data=catch_data)
                reward = trainers.award_wild_outcome(trainer, wild_level=encounter.level, caught=True)
                
                # Award EXP to active party
                pkmn_levelups = []
                species_entry = self.data.species_entry(encounter.species)
                yield_val = species_entry.get("base_experience", 100)
                base_exp = int((yield_val * encounter.level) / 7)
                
                slots = list(session.scalars(select(PartySlot).where(PartySlot.trainer_id == trainer.id).order_by(PartySlot.slot_index)))
                active_party = [slot.pokemon for slot in slots if slot.pokemon is not None and slot.pokemon.current_hp > 0]
                if active_party:
                    gain = int(base_exp / len(active_party))
                    if gain > 0:
                        for p in active_party:
                            events = pokemons.gain_exp(p, gain, self.data)
                            for e in events:
                                pkmn_levelups.append(f"🎉 {p.species} reached Lv. {e['level']}!")
                                if self.generator:
                                    new_moves = await self.generator.get_levelup_moves(p.species, e["level"])
                                    existing_moves = json.loads(p.moves_json)
                                    for move in new_moves:
                                        if move not in existing_moves:
                                            if len(existing_moves) < 4:
                                                existing_moves.append(move)
                                                p.moves_json = json.dumps(existing_moves)
                                                pkmn_levelups.append(f"✨ {p.species} learned {move}!")
                                            else:
                                                expires = int((datetime.utcnow() + timedelta(minutes=10)).timestamp())
                                                trainer.pending_move_learning = json.dumps({
                                                    "pokemon_id": p.id,
                                                    "move": move,
                                                    "expires_at": expires
                                                })
                                                pkmn_levelups.append(f"❗ {p.species} wants to learn {move}, but it already knows 4 moves. Use the move-learning prompt to choose a move slot.")
                            
                            # Sync the packed set since level or moves changed
                            pokemons.sync_packed_set(p, self.data)

            self.active_by_user.pop(user_id, None)
            balls_left = state.balls_left
            reward_line = f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP"
            level_up_line = self.trainer_level_up_text(reward, prefix="\n🎉 ")
            pkmn_level_line = ("\n" + "\n".join(pkmn_levelups)) if pkmn_levelups else ""
            
            if balls_left <= 0:
                self.safari_sessions.pop(user_id, None)
                note = (
                    f"{throw_line}\n"
                    f"You caught {owned.species} with a Safari Ball!\n"
                    f"{reward_line}{level_up_line}{pkmn_level_line}\n"
                    "That was your last Safari Ball. Use /safari for a new Safari run."
                )
            else:
                note = (
                    f"{throw_line}\n"
                    f"You caught {owned.species} with a Safari Ball!\n"
                    f"{reward_line}{level_up_line}{pkmn_level_line}\n"
                    "Use /hunt to keep exploring or /exit to leave."
                )
            return {
                "keep_active": False,
                "text": self.render_safari_result_text(user_id=user_id, note=note, balls_left=balls_left),
                "answer": f"{owned.species} was caught.",
            }

        if state.balls_left <= 0:
            self.active_by_user.pop(user_id, None)
            self.safari_sessions.pop(user_id, None)
            return {
                "keep_active": False,
                "text": self.render_safari_result_text(
                    user_id=user_id,
                    note=(
                        f"{throw_line}\n"
                        f"Your last Safari Ball missed {encounter.species}.\n"
                        "Safari is over. Use /safari for a new run."
                    ),
                    balls_left=0,
                ),
                "answer": "Out of Safari Balls.",
            }

        if random.random() <= self.safari_flee_chance(int(encounter.catch_rate)):
            self.active_by_user.pop(user_id, None)
            return {
                "keep_active": False,
                "text": self.render_safari_result_text(
                    user_id=user_id,
                    note=(
                        f"{throw_line}\n"
                        f"The wild {encounter.species} fled after the throw.\n"
                        "Use /hunt to keep exploring or /exit to leave."
                    ),
                    balls_left=state.balls_left,
                ),
                "answer": f"{encounter.species} fled.",
            }

        encounter.note = f"{throw_line}\nSafari Ball failed. {encounter.species} is still here."
        return {
            "keep_active": True,
            "text": "",
            "answer": "Safari Ball failed.",
        }

    async def attempt_menu_throw_ball(self, user_id: int, ball_kind: str, encounter: EncounterSession) -> str:
        return await self._attempt_menu_throw_ball_impl(user_id, ball_kind, encounter)
        ball_name = self.ball_label(ball_kind)
        throw_line = self.ball_use_line(ball_name)
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None:
                return "Trainer record missing."
            if not inventories.consume_ball(trainer, ball_kind):
                return "You are out of that ball."

            success = self.roll_catch(
                current_hp=int(encounter.generated["current_hp"]),
                max_hp=int(encounter.generated["max_hp"]),
                status=str(encounter.generated.get("status", "")),
                catch_rate=int(encounter.catch_rate),
                ball_kind=ball_kind,
            )
            if not success:
                encounter.ball_menu_open = False
                encounter.note = f"{throw_line}\nThe wild {encounter.species} broke free."
                return f"{ball_name} failed."

            catch_data = dict(encounter.generated)
            catch_data["source_kind"] = encounter.source
            catch_data["origin_region"] = encounter.region
            owned = pokemons.create_owned_pokemon(trainer=trainer, data=catch_data)
            reward = trainers.award_wild_outcome(trainer, wild_level=encounter.level, caught=True)
            
            # Award EXP to active party since the turn ended with a catch
            pkmn_levelups = []
            species_entry = self.data.species_entry(encounter.species)
            yield_val = species_entry.get("base_experience", 100)
            base_exp = int((yield_val * encounter.level) / 7)
            
            # Simple version: the first party member that isn't fainted gets it or all active members get it
            # Menu encounters don't track HP in real-time like battles, but let's assume the first is active
            slots = list(session.scalars(select(PartySlot).where(PartySlot.trainer_id == trainer.id).order_by(PartySlot.slot_index)))
            active_party = [slot.pokemon for slot in slots if slot.pokemon is not None and slot.pokemon.current_hp > 0]
            if active_party:
                gain = int(base_exp / len(active_party))
                if gain > 0:
                    for p in active_party:
                        events = pokemons.gain_exp(p, gain, self.data)
                        for e in events:
                            pkmn_levelups.append(f"🎉 {p.species} reached Lv. {e['level']}!")
                            if self.generator:
                                new_moves = await self.generator.get_levelup_moves(p.species, e["level"])
                                existing_moves = json.loads(p.moves_json)
                                for move in new_moves:
                                    if move not in existing_moves:
                                        if len(existing_moves) < 4:
                                            existing_moves.append(move)
                                            p.moves_json = json.dumps(existing_moves)
                                            pkmn_levelups.append(f"✨ {p.species} learned {move}!")
                                        else:
                                            expires = int((datetime.utcnow() + timedelta(minutes=10)).timestamp())
                                            trainer.pending_move_learning = json.dumps({
                                                "pokemon_id": p.id,
                                                "move": move,
                                                "expires_at": expires
                                            })
                                            pkmn_levelups.append(f"❗ {p.species} wants to learn {move}, but it already knows 4 moves. Use the move-learning prompt to choose a move slot.")
                        
                        # Sync the packed set since level or moves changed
                        pokemons.sync_packed_set(p, self.data)

            self.active_by_user.pop(user_id, None)
            level_up_line = self.trainer_level_up_text(reward, prefix="\n🎉 ")
            pkmn_level_line = ("\n" + "\n".join(pkmn_levelups)) if pkmn_levelups else ""
            encounter.note = (
                f"{throw_line}\nYou caught {owned.species} with a {ball_name}!\n"
                f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP{level_up_line}{pkmn_level_line}"
            )
            return f"{owned.species} was caught."

    def ball_use_line(self, ball_name: str, *, shake_count: int = 3) -> str:
        return self._legacy_ball_use_line(ball_name, shake_count=shake_count)
        stars = "".join("⭐" for _ in range(max(1, shake_count)))
        return f"You used {ball_name}\n{stars}"

    async def animate_encounter_ball_throw(
        self,
        event: CallbackQuery.Event,
        encounter: EncounterSession,
        ball_name: str,
        *,
        shake_count: int = 3,
    ) -> None:
        for current_shake in range(1, max(1, int(shake_count)) + 1):
            await safe_event_edit(
                event,
                self.ball_use_line(ball_name, shake_count=current_shake),
                buttons=None,
                parse_mode="md",
            )
            await asyncio.sleep(BALL_THROW_ANIMATION_DELAY)

    def ball_label(self, ball_kind: str) -> str:
        return format_ball_label(ball_kind)

    def trainer_ball_count(self, user_id: int, ball_kind: str) -> int:
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None:
                return 0
            return inventories.ball_count(trainer, ball_kind)

    def trainer_ball_inventory(self, user_id: int, *, include_zero: bool = False) -> list[tuple[str, int]]:
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None:
                return []
            return inventories.ball_counts(trainer, include_zero=include_zero)

    def _encounter_types(self, encounter: EncounterSession) -> list[str]:
        raw_types = encounter.generated.get("types") or self.data.types_for_species(encounter.species)
        return [str(item).lower() for item in raw_types if str(item).strip()]

    def _battle_player_level(self, battle: "BattleSession") -> int | None:
        active = battle.public_view.active.get("p1") or {}
        try:
            return int(active.get("level"))
        except (TypeError, ValueError):
            return None

    def _trainer_catch_lead_level(self, session, trainer: Trainer) -> int | None:
        teams = TeamRepository(session)
        active_team = teams.get_active_team(trainer)
        for slot in teams.team_slots(active_team):
            pokemon = slot.pokemon
            if pokemon is not None and pokemon.current_hp > 0:
                return int(pokemon.level)

        party_slots = list(
            session.scalars(
                select(PartySlot).where(PartySlot.trainer_id == trainer.id).order_by(PartySlot.slot_index)
            )
        )
        for slot in party_slots:
            pokemon = slot.pokemon
            if pokemon is not None and pokemon.current_hp > 0:
                return int(pokemon.level)
        return None

    def _trainer_owns_species(self, pokemons: PokemonRepository, trainer: Trainer, species: str) -> bool:
        target = self.base_species_key(species)
        for owned_species in pokemons.session.scalars(
            select(OwnedPokemon.species).where(OwnedPokemon.trainer_id == trainer.id)
        ):
            if self.base_species_key(str(owned_species or "")) == target:
                return True
        return False

    def _dusk_ball_bonus_available(self, location_name: str | None) -> bool:
        text = slugify(location_name or "")
        return any(keyword in text for keyword in CAVE_LOCATION_KEYWORDS)

    def _ball_multiplier_for_context(
        self,
        *,
        ball_kind: str,
        species: str | None,
        species_types: list[str] | None,
        wild_level: int | None,
        turn_number: int | None,
        source: str | None,
        location_name: str | None,
        status: str,
        player_level: int | None,
        owned_before: bool,
        settings: dict[str, Any],
        ball_multiplier_override: float | None,
    ) -> float:
        if ball_multiplier_override is not None:
            return float(ball_multiplier_override)

        default_multiplier = float(settings.get("ball_multipliers", {}).get(ball_kind, 1.0))
        types = {str(item).lower() for item in (species_types or []) if str(item).strip()}
        battle_turn = max(int(turn_number or 1), 1)
        level = max(int(wild_level or 1), 1)
        status_key = status.strip().lower()
        species_token = species_key(species or "")

        if ball_kind == "quick":
            return 5.0 if battle_turn == 1 else 1.0
        if ball_kind == "timer":
            return min(4.0, 1.0 + (0.3 * max(battle_turn - 1, 0)))
        if ball_kind == "repeat":
            return 3.5 if owned_before else 1.0
        if ball_kind == "nest":
            return max(1.0, min(4.0, (41 - level) / 10)) if level < 31 else 1.0
        if ball_kind == "net":
            return 3.5 if {"bug", "water"} & types else 1.0
        if ball_kind == "dive":
            return 3.5 if "water" in types or str(source or "").lower() == "surf" else 1.0
        if ball_kind == "level":
            if player_level is None:
                return 1.0
            if player_level >= level * 4:
                return 8.0
            if player_level >= level * 2:
                return 4.0
            if player_level > level:
                return 2.0
            return 1.0
        if ball_kind == "lure":
            return 3.0 if "water" in types else 1.0
        if ball_kind == "dusk":
            return default_multiplier if self._dusk_ball_bonus_available(location_name) else 1.0
        if ball_kind == "dream":
            return 4.0 if status_key == "slp" else 1.0
        if ball_kind == "beast":
            return 5.0 if species_token in ULTRA_BEAST_KEYS else 0.1
        return default_multiplier

    def _status_multiplier(self, status: str) -> float:
        status_key = status.strip().lower()
        if status_key in {"slp", "frz"}:
            return 2.5
        if status_key in {"par", "brn", "psn", "tox"}:
            return 1.5
        return 1.0

    def roll_catch(
        self,
        *,
        current_hp: int,
        max_hp: int,
        status: str,
        catch_rate: int,
        ball_kind: str,
        species: str | None = None,
        species_types: list[str] | None = None,
        wild_level: int | None = None,
        turn_number: int | None = None,
        source: str | None = None,
        location_name: str | None = None,
        player_level: int | None = None,
        owned_before: bool = False,
        ball_multiplier_override: float | None = None,
    ) -> bool:
        if ball_kind == BALL_MASTER:
            return True
        settings = self._load_json(CATCH_SETTINGS_PATH)
        ball_multiplier = self._ball_multiplier_for_context(
            ball_kind=ball_kind,
            species=species,
            species_types=species_types,
            wild_level=wild_level,
            turn_number=turn_number,
            source=source,
            location_name=location_name,
            status=status,
            player_level=player_level,
            owned_before=owned_before,
            settings=settings,
            ball_multiplier_override=ball_multiplier_override,
        )
        status_multiplier = self._status_multiplier(status)

        maximum_hp = max(int(max_hp), 1)
        current_hp_value = min(max(int(current_hp), 1), maximum_hp)
        rate = max(int(catch_rate), 1)

        catch_value = int(
            ((((3 * maximum_hp) - (2 * current_hp_value)) * rate * ball_multiplier) / (3 * maximum_hp))
            * status_multiplier
        )
        if catch_value >= 255:
            return True
        if catch_value <= 0:
            return False

        shake_threshold = int(65536 / sqrt(sqrt(255 / catch_value)))
        return all(random.randrange(65536) < shake_threshold for _ in range(4))

    def is_wild_battle(self, battle: "BattleSession") -> bool:
        return battle.metadata.get("encounter_kind") == "wild"

    def extra_recent_lines(self, battle: "BattleSession") -> list[str]:
        if not self.is_wild_battle(battle):
            return []
        raw_lines = battle.metadata.get("encounter_recent") or []
        if not isinstance(raw_lines, list):
            return []
        return [str(line).strip() for line in raw_lines if str(line).strip()]

    def caught_battle_render(self, battle: "BattleSession") -> tuple[str, list[list[tuple[str, str]]] | None] | None:
        if not self.is_wild_battle(battle) or battle.metadata.get("encounter_outcome") != "caught":
            return None
        species = str(
            battle.metadata.get("caught_species")
            or battle.metadata.get("wild_species")
            or "the Pokemon"
        ).strip()
        pokemon_id = battle.metadata.get("caught_pokemon_id")
        buttons: list[list[tuple[str, str]]] | None = None
        if pokemon_id is not None:
            owner_id = int(battle.metadata.get("owner_user_id") or 0)
            buttons = [[
                ("Stats", f"pstats:pick:{owner_id}:{int(pokemon_id)}"),
                ("Release", f"pstats:release:{owner_id}:{int(pokemon_id)}"),
            ]]
        return f"You caught {species} !", buttons

    def ball_menu_text(self, battle: "BattleSession") -> str:
        owner_id = int(battle.metadata["owner_user_id"])
        inventory = self.trainer_ball_inventory(owner_id, include_zero=False)
        
        lines = [
            "🎒 **Bag » Poké Balls**",
            "━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        if not inventory:
            lines.append("__You do not have any Poké Balls right now.__")
            return "\n".join(lines)
            
        for index, (ball_kind, count) in enumerate(inventory, start=1):
            ball_name = self.ball_label(ball_kind)
            lines.append(f"`[{index}]` **{ball_name}**  —  `x{count}`")
            
        lines.extend([
            "",
            "__Select a number below to throw!__"
        ])
        
        return "\n".join(lines)

    def ball_menu_button_specs(self, battle: "BattleSession", player: "PlayerState") -> list[list[tuple[str, str]]]:
        owner_id = int(battle.metadata["owner_user_id"])
        inventory = self.trainer_ball_inventory(owner_id, include_zero=False)
        
        specs = [
            (
                str(index),
                self.battle_service.action_data(battle, player, f"ball-{ball_kind}"),
            )
            for index, (ball_kind, count) in enumerate(inventory, start=1)
        ]
        
        # Displaying 4 numbered buttons per row makes it look like a clean keypad
        rows = chunk_button_specs(specs, per_row=4) if specs else []
        rows.append([("⬅️ Back", self.battle_service.action_data(battle, player, "bb"))])
        return rows

    def override_battle_render(
        self,
        battle: "BattleSession",
        player: "PlayerState",
        request: dict[str, Any],
    ) -> tuple[str, list[list[tuple[str, str]]] | None] | None:
        if not self.is_wild_battle(battle) or player.slot != "p1":
            return None
        if request.get("teamPreview") or request.get("forceSwitch"):
            return None
        if not battle.metadata.get("ball_menu_open"):
            return None
        return self.ball_menu_text(battle), self.ball_menu_button_specs(battle, player)

    def extra_action_rows(self, battle: "BattleSession", player: "PlayerState", request: dict[str, Any]) -> list[list[tuple[str, str]]]:
        if not self.is_wild_battle(battle) or player.slot != "p1":
            return []
        if request.get("teamPreview"):
            return []
        rows: list[list[tuple[str, str]]] = []
        rows.append([
            ("Balls", self.battle_service.action_data(battle, player, "bl")),
            ("Run", self.battle_service.action_data(battle, player, "r")),
        ])
        return rows

    def extra_status_lines(self, battle: "BattleSession") -> list[str]:
        if not self.is_wild_battle(battle):
            return []
        note = str(battle.metadata.get("encounter_note", "")).strip()
        return [note] if note else []

    async def handle_battle_special_action(
        self,
        battle: "BattleSession",
        player: "PlayerState",
        request: dict[str, Any],
        action_code: str,
    ) -> tuple[str, bool, bool] | None:
        if not self.is_wild_battle(battle) or player.slot != "p1":
            return None
        if action_code == "bl":
            battle.metadata["ball_menu_open"] = True
            return "Ball menu opened.", True, False
        if action_code == "bb":
            battle.metadata["ball_menu_open"] = False
            return "Ball menu closed.", True, False
        if action_code.startswith("ball-"):
            ball_kind = action_code[5:]
            if ball_kind not in BALL_ORDER:
                return "That ball does not exist.", False, True
            owner_id = int(battle.metadata["owner_user_id"])
            if self.trainer_ball_count(owner_id, ball_kind) <= 0:
                battle.metadata["ball_menu_open"] = True
                return "You are out of that ball.", False, True

            throw_plan = self._plan_battle_throw_outcome(battle, ball_kind)
            if throw_plan.get("error"):
                battle.metadata["ball_menu_open"] = False
                return str(throw_plan["error"]), False, True
            ball_name = self.ball_label(ball_kind)
            battle.metadata["ball_menu_open"] = False
            for shake_count in range(1, max(1, int(throw_plan.get("shake_count") or 3)) + 1):
                await self.battle_service._edit_message(
                    battle.chat_id,
                    battle.public_message_id,
                    self.ball_use_line(ball_name, shake_count=shake_count),
                    buttons=None,
                )
                await asyncio.sleep(BALL_THROW_ANIMATION_DELAY)

            result_text = await self.attempt_battle_throw_ball(battle, ball_kind, planned_outcome=throw_plan)
            return result_text, True, False
        if action_code == "r":
            battle.finished = True
            battle.metadata["ball_menu_open"] = False
            battle.metadata["encounter_outcome"] = "ran"
            battle.metadata["encounter_note"] = "You ran away safely."
            owner_id = int(battle.metadata["owner_user_id"])
            self.active_by_user.pop(owner_id, None)
            if battle.bridge is not None:
                await battle.bridge.close()
            return "You ran away.", True, False
        return None

    async def _attempt_battle_throw_ball_impl(
        self,
        battle: "BattleSession",
        ball_kind: str,
        *,
        planned_outcome: dict[str, Any] | None = None,
    ) -> str:
        owner_id = int(battle.metadata["owner_user_id"])
        encounter = self.active_by_user.get(owner_id)
        if encounter is None:
            return "This encounter is no longer active."
        ball_name = self.ball_label(ball_kind)
        wild = battle.public_view.active.get("p2") or {}
        current_hp = (
            planned_outcome.get("current_hp")
            if planned_outcome is not None and planned_outcome.get("current_hp") is not None
            else (wild.get("current_hp") if wild.get("current_hp") is not None else encounter.generated["current_hp"])
        )
        max_hp = (
            planned_outcome.get("max_hp")
            if planned_outcome is not None and planned_outcome.get("max_hp") is not None
            else (wild.get("max_hp") if wild.get("max_hp") is not None else encounter.generated["max_hp"])
        )
        status = str(
            planned_outcome.get("status")
            if planned_outcome is not None and planned_outcome.get("status") is not None
            else (wild.get("status") or encounter.generated.get("status", ""))
        )
        level_up_messages: list[str] = []
        pending_prompts: list[dict[str, Any]] = []
        loot_lines: list[str] = []
        caught_name = pokemon_display_name(encounter.species, shiny=bool(encounter.generated.get("shiny")))
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(owner_id)
            if trainer is None:
                battle.metadata["encounter_recent"] = ["Trainer record missing."]
                return "Trainer record missing."
            if not inventories.consume_ball(trainer, ball_kind):
                return "You are out of that ball."
            success = bool(planned_outcome.get("success")) if planned_outcome is not None else self.roll_catch(
                current_hp=int(current_hp),
                max_hp=int(max_hp),
                status=status,
                catch_rate=int(encounter.catch_rate),
                ball_kind=ball_kind,
                species=encounter.species,
                species_types=self._encounter_types(encounter),
                wild_level=encounter.level,
                turn_number=max(int(battle.public_view.turn or 0), 1),
                source=encounter.source,
                location_name=encounter.location_name,
                player_level=self._battle_player_level(battle),
                owned_before=self._trainer_owns_species(pokemons, trainer, encounter.species),
            )
            if not success:
                battle.metadata["ball_menu_open"] = False
                fled = bool(planned_outcome.get("fled")) if planned_outcome is not None else (
                    random.random() <= self.failed_ball_flee_chance(int(encounter.catch_rate))
                )
                if fled:
                    battle.finished = True
                    battle.metadata["encounter_outcome"] = "fled"
                    battle.metadata["encounter_note"] = f"{ball_name} failed.\nWild {encounter.species} fled !!"
                    battle.metadata.pop("encounter_recent", None)
                    self.active_by_user.pop(owner_id, None)
                    if battle.bridge is not None:
                        await battle.bridge.close()
                    return f"Wild {encounter.species} fled !!"
                battle.metadata["encounter_note"] = ""
                battle.metadata["encounter_recent"] = [f"{ball_name} failed."]
                return f"{ball_name} failed."
            try:
                await self._ensure_materialized_encounter(encounter)
            except ShowdownBridgeError:
                battle.finished = True
                battle.metadata["ball_menu_open"] = False
                battle.metadata["encounter_outcome"] = "validation_failed"
                battle.metadata["encounter_note"] = "The catch could not be validated and the encounter ended."
                battle.metadata.pop("encounter_recent", None)
                self.active_by_user.pop(owner_id, None)
                if battle.bridge is not None:
                    await battle.bridge.close()
                return "Catch validation failed."
            catch_data = dict(encounter.generated)
            catch_data["source_kind"] = encounter.source
            catch_data["origin_region"] = encounter.region
            catch_data["current_hp"] = int(current_hp)
            catch_data["max_hp"] = int(max_hp)
            catch_data["status"] = status
            owned = pokemons.create_owned_pokemon(trainer=trainer, data=catch_data)
            caught_name = pokemon_display_name(owned.species, shiny=bool(owned.shiny))
            reward = trainers.award_wild_outcome(trainer, wild_level=encounter.level, caught=True)
            level_up_messages, pending_prompts = await self._award_party_experience(
                session,
                trainer,
                self._active_party_members(session, trainer),
                wild_species=encounter.species,
                wild_level=encounter.level,
            )
            loot_lines = self._roll_loot_lines(
                inventories,
                trainer,
                encounter.region,
                source_kind=encounter.source,
                encounter_weight=encounter.spawn_weight,
            )
        await self._send_progression_followups(
            owner_id,
            level_up_messages=level_up_messages,
            pending_prompts=pending_prompts,
        )
        battle.finished = True
        battle.metadata["ball_menu_open"] = False
        battle.metadata["encounter_outcome"] = "caught"
        battle.metadata["caught_pokemon_id"] = owned.id
        battle.metadata["caught_species"] = caught_name
        battle.metadata["encounter_note"] = (
            f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP"
            + self.trainer_level_up_text(reward, prefix="\n")
            + (("\n" + "\n".join(loot_lines)) if loot_lines else "")
        )
        battle.metadata.pop("encounter_recent", None)
        self.active_by_user.pop(owner_id, None)
        if battle.bridge is not None:
            await battle.bridge.close()
        return f"{caught_name} was caught."

    async def attempt_battle_throw_ball(
        self,
        battle: "BattleSession",
        ball_kind: str,
        *,
        planned_outcome: dict[str, Any] | None = None,
    ) -> str:
        return await self._attempt_battle_throw_ball_impl(
            battle,
            ball_kind,
            planned_outcome=planned_outcome,
        )
        owner_id = int(battle.metadata["owner_user_id"])
        encounter = self.active_by_user.get(owner_id)
        if encounter is None:
            return "This encounter is no longer active."
        ball_name = self.ball_label(ball_kind)
        wild = battle.public_view.active.get("p2") or {}
        current_hp = wild.get("current_hp") if wild.get("current_hp") is not None else encounter.generated["current_hp"]
        max_hp = wild.get("max_hp") if wild.get("max_hp") is not None else encounter.generated["max_hp"]
        status = str(wild.get("status") or encounter.generated.get("status", ""))
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(owner_id)
            if trainer is None:
                battle.metadata["encounter_recent"] = ["Trainer record missing."]
                return "Trainer record missing."
            if not inventories.consume_ball(trainer, ball_kind):
                return "You are out of that ball."
            success = self.roll_catch(
                current_hp=int(current_hp),
                max_hp=int(max_hp),
                status=status,
                catch_rate=int(encounter.catch_rate),
                ball_kind=ball_kind,
            )
            if not success:
                battle.metadata["ball_menu_open"] = False
                battle.metadata["encounter_note"] = ""
                battle.metadata["encounter_recent"] = [f"{ball_name} failed"]
                return f"{ball_name} failed."
            catch_data = dict(encounter.generated)
            catch_data["source_kind"] = encounter.source
            catch_data["origin_region"] = encounter.region
            catch_data["current_hp"] = int(current_hp)
            catch_data["max_hp"] = int(max_hp)
            catch_data["status"] = status
            owned = pokemons.create_owned_pokemon(trainer=trainer, data=catch_data)
            reward = trainers.award_wild_outcome(trainer, wild_level=encounter.level, caught=True)
        battle.finished = True
        battle.metadata["ball_menu_open"] = False
        battle.metadata["encounter_outcome"] = "caught"
        battle.metadata["caught_pokemon_id"] = owned.id
        battle.metadata["caught_species"] = owned.species
        battle.metadata["encounter_note"] = (
            f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP"
            + self.trainer_level_up_text(reward, prefix="\n🎉 ")
        )
        battle.metadata.pop("encounter_recent", None)
        self.active_by_user.pop(owner_id, None)
        if battle.bridge is not None:
            await battle.bridge.close()
        return f"{owned.species} was caught."

    async def on_battle_request(self, battle: "BattleSession", slot: str, request: dict[str, Any]) -> None:
        if not self.is_wild_battle(battle):
            return
        if request.get("teamPreview") and battle.bridge is not None:
            player = battle.players[slot]
            if player.locked_choice:
                return
            choice = self.battle_service.team_preview_choice(request, lead_index=1)
            player.locked_choice = self.battle_service.describe_choice(request, choice)
            await battle.bridge.choose(slot, choice)

    async def after_player_choice(self, battle: "BattleSession", player: "PlayerState", request: dict[str, Any], choice: str) -> None:
        if not self.is_wild_battle(battle) or player.slot != "p1" or battle.bridge is None:
            return
        battle.metadata["ball_menu_open"] = False
        wild_player = battle.players["p2"]
        wild_request = wild_player.current_request
        if not wild_request or wild_request.get("wait") or wild_player.locked_choice:
            return
        ai_choice = self.choose_wild_action(wild_request)
        if not ai_choice:
            return
        wild_player.locked_choice = "Wild action"
        await battle.bridge.choose("p2", ai_choice)

    def choose_wild_action(self, request: dict[str, Any]) -> str | None:
        if request.get("teamPreview"):
            return self.battle_service.team_preview_choice(request, lead_index=1)
        if request.get("forceSwitch"):
            for index, pokemon in enumerate(request["side"]["pokemon"], start=1):
                condition = str(pokemon.get("condition", ""))
                if not pokemon.get("active") and not parse_condition(condition)["fainted"]:
                    return f"switch {index}"
            return None
        active = (request.get("active") or [{}])[0]
        moves = active.get("moves") or []
        legal = [index for index, move in enumerate(moves, start=1) if not move.get("disabled")]
        if not legal:
            return None
        return f"move {random.choice(legal)}"

    async def _on_battle_end_impl(self, battle: "BattleSession") -> None:
        if not self.is_wild_battle(battle):
            return
        owner_id = int(battle.metadata["owner_user_id"])
        await self.persist_player_team_state(battle, owner_id)
        encounter = self.active_by_user.get(owner_id)
        outcome = str(battle.metadata.get("encounter_outcome", "")).strip()
        if encounter and not outcome:
            self.active_by_user.pop(owner_id, None)
        if outcome:
            return

        winner = (battle.public_view.winner or "").strip()
        trainer_name = battle.players["p1"].name
        if winner and winner == trainer_name:
            encounter = self.active_by_user.get(owner_id)
            wild_level = int(battle.metadata.get("wild_level", encounter.level if encounter else 5))
            with db_session() as session:
                trainers = TrainerRepository(session)
                trainer = trainers.get_by_telegram_user_id(owner_id)
                if trainer is not None:
                    reward = trainers.award_wild_outcome(trainer, wild_level=wild_level, caught=False)
                    level_line = self.trainer_level_up_text(reward, prefix="\n")
                    note = (
                        f"The wild {battle.metadata.get('wild_species', 'Pokemon')} fainted.\n"
                        f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP{level_line}"
                    )
                else:
                    note = f"The wild {battle.metadata.get('wild_species', 'Pokemon')} fainted."
            loot_lines = list(battle.metadata.pop("post_battle_loot_lines", []) or [])
            if loot_lines:
                note = note.rstrip() + "\n" + "\n".join(loot_lines)
            battle.metadata["encounter_note"] = note
        elif winner:
            with db_session() as session:
                trainers = TrainerRepository(session)
                trainer = trainers.get_by_telegram_user_id(owner_id)
                if trainer is not None:
                    trainers.record_battle_loss(trainer)
            battle.metadata["encounter_note"] = "Your team lost the battle."
        else:
            battle.metadata["encounter_note"] = "The encounter ended."
        battle.metadata.pop("post_battle_loot_lines", None)

    async def on_battle_end(self, battle: "BattleSession") -> None:
        await self._on_battle_end_impl(battle)
        return
        if not self.is_wild_battle(battle):
            return
        owner_id = int(battle.metadata["owner_user_id"])
        await self.persist_player_team_state(battle, owner_id)
        encounter = self.active_by_user.get(owner_id)
        outcome = str(battle.metadata.get("encounter_outcome", "")).strip()
        if encounter and not outcome:
            self.active_by_user.pop(owner_id, None)
        if outcome:
            return
        winner = (battle.public_view.winner or "").strip()
        trainer_name = battle.players["p1"].name
        if winner and winner == trainer_name:
            # Trainer won — award VP + EXP
            encounter = self.active_by_user.get(owner_id)
            wild_level = int(battle.metadata.get("wild_level", encounter.level if encounter else 5))
            with db_session() as session:
                trainers = TrainerRepository(session)
                trainer = trainers.get_by_telegram_user_id(owner_id)
                if trainer is not None:
                    reward = trainers.award_wild_outcome(trainer, wild_level=wild_level, caught=False)
                    level_line = self.trainer_level_up_text(reward, prefix=" | ", inline=True)
                    battle.metadata["encounter_note"] = (
                        f"The wild {battle.metadata.get('wild_species', 'Pokemon')} fainted.\n"
                        f"+{reward['exp_gain']} EXP  +{reward['vp_gain']} VP  +{reward.get('sp_gain', 0)} SP{level_line}"
                    )
                else:
                    battle.metadata["encounter_note"] = f"The wild {battle.metadata.get('wild_species', 'Pokemon')} fainted."
        elif winner:
            # Wild won — record loss
            with db_session() as session:
                trainers = TrainerRepository(session)
                trainer = trainers.get_by_telegram_user_id(owner_id)
                if trainer is not None:
                    trainers.record_battle_loss(trainer)
            battle.metadata["encounter_note"] = f"{battle.metadata.get('wild_species', 'The wild Pokemon')} won the battle."
        else:
            battle.metadata["encounter_note"] = "The encounter ended."

    async def _persist_player_team_state_impl(self, battle: "BattleSession", owner_id: int) -> None:
        request = battle.players["p1"].current_request or {}
        side_pokemon = (request.get("side") or {}).get("pokemon") or []
        owned_ids = list(battle.metadata.get("owned_team_ids") or [])
        if not side_pokemon or not owned_ids:
            battle.metadata.pop("post_battle_loot_lines", None)
            return

        winner = (battle.public_view.winner or "").strip()
        trainer_name = battle.players["p1"].name
        outcome = str(battle.metadata.get("encounter_outcome", "")).strip()
        is_win = (winner == trainer_name and outcome != "caught")
        wild_species = str(battle.metadata.get("wild_species") or "Pokemon")
        wild_level = int(battle.metadata.get("wild_level", 5))
        encounter = self.active_by_user.get(owner_id)

        level_up_messages: list[str] = []
        pending_prompts: list[dict[str, Any]] = []
        loot_lines: list[str] = []

        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(owner_id)
            if trainer is None:
                battle.metadata.pop("post_battle_loot_lines", None)
                return

            exp_recipients: list[Any] = []
            for pokemon_id, side_entry in zip(owned_ids, side_pokemon, strict=False):
                if pokemon_id is None:
                    continue
                owned = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
                if owned is None:
                    continue

                parsed = parse_condition(str(side_entry.get("condition", "")))
                current_hp = 0 if parsed["fainted"] else parsed["current_hp"]
                status = "" if parsed["fainted"] else parsed["status"]
                pokemons.apply_condition_snapshot(
                    owned,
                    current_hp=current_hp,
                    max_hp=parsed["max_hp"],
                    status=status,
                )
                if is_win and current_hp > 0:
                    exp_recipients.append(owned)

            if is_win:
                level_up_messages, pending_prompts = await self._award_party_experience(
                    session,
                    trainer,
                    exp_recipients,
                    wild_species=wild_species,
                    wild_level=wild_level,
                )
                loot_lines = self._roll_loot_lines(
                    inventories,
                    trainer,
                    encounter.region if encounter is not None else trainer.current_region,
                    source_kind=encounter.source if encounter is not None else "hunt",
                    encounter_weight=encounter.spawn_weight if encounter is not None else 1.0,
                )

        if level_up_messages or pending_prompts:
            await self._send_progression_followups(
                owner_id,
                level_up_messages=level_up_messages,
                pending_prompts=pending_prompts,
            )
        if loot_lines:
            battle.metadata["post_battle_loot_lines"] = loot_lines
        else:
            battle.metadata.pop("post_battle_loot_lines", None)

    async def persist_player_team_state(self, battle: "BattleSession", owner_id: int) -> None:
        await self._persist_player_team_state_impl(battle, owner_id)
        return
        request = battle.players["p1"].current_request or {}
        side_pokemon = (request.get("side") or {}).get("pokemon") or []
        owned_ids = list(battle.metadata.get("owned_team_ids") or [])
        if not side_pokemon or not owned_ids:
            return

        winner = (battle.public_view.winner or "").strip()
        trainer_name = battle.players["p1"].name
        is_win = (winner == trainer_name)
        
        # Calculate base EXP if it was a win
        base_exp = 0
        if is_win:
            # We need a dummy wild object or just species/level
            wild_species = battle.metadata.get("wild_species", "Pikachu")
            wild_level = int(battle.metadata.get("wild_level", 5))
            # Just a rough estimate for now since we don't have the full wild object here
            # We'll use a simplified version of the logic
            species_entry = self.data.species_entry(wild_species)
            yield_val = species_entry.get("base_experience", 100)
            base_exp = int((yield_val * wild_level) / 7)

        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(owner_id)
            if trainer is None:
                return

            pkmn_levelups = []
            
            for pokemon_id, side_entry in zip(owned_ids, side_pokemon, strict=False):
                if pokemon_id is None:
                    continue
                owned = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
                if owned is None:
                    continue
                
                parsed = parse_condition(str(side_entry.get("condition", "")))
                current_hp = 0 if parsed["fainted"] else parsed["current_hp"]
                status = "" if parsed["fainted"] else parsed["status"]
                
                # Apply HP/Status
                pokemons.apply_condition_snapshot(
                    owned,
                    current_hp=current_hp,
                    max_hp=parsed["max_hp"],
                    status=status,
                )

                # Award EXP if not fainted and it was a win
                if is_win and current_hp > 0:
                    # Distribute EXP (simple split among survivors)
                    active_count = sum(1 for p in side_pokemon if not parse_condition(str(p.get("condition", "")))["fainted"])
                    if active_count > 0:
                        gain = int(base_exp / active_count)
                        if gain > 0:
                            events = pokemons.gain_exp(owned, gain, self.data)
                            for e in events:
                                pkmn_levelups.append(f"🎉 {owned.species} reached Lv. {e['level']}!")
                                # Check for new moves
                                if self.generator:
                                    new_moves = await self.generator.get_levelup_moves(owned.species, e["level"])
                                    existing_moves = json.loads(owned.moves_json)
                                    for move in new_moves:
                                        if move not in existing_moves:
                                            if len(existing_moves) < 4:
                                                existing_moves.append(move)
                                                owned.moves_json = json.dumps(existing_moves)
                                                pkmn_levelups.append(f"✨ {owned.species} learned {move}!")
                                            else:
                                                # Set pending move learning
                                                # Use 10 minute expiration as requested
                                                expires = int((datetime.utcnow() + timedelta(minutes=10)).timestamp())
                                                trainer.pending_move_learning = json.dumps({
                                                    "pokemon_id": owned.id,
                                                    "move": move,
                                                    "expires_at": expires
                                                })
                                                pkmn_levelups.append(f"❗ {owned.species} wants to learn {move}, but it already knows 4 moves. Use the move-learning prompt to choose a move slot.")
                                
                                # Sync the packed set since level or moves changed
                                pokemons.sync_packed_set(owned, self.data)

            if pkmn_levelups:
                existing_note = battle.metadata.get("encounter_note", "")
                battle.metadata["encounter_note"] = existing_note + "\n" + "\n".join(pkmn_levelups)

