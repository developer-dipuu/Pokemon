import asyncio
import csv
import json
import random
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from telethon import Button
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.types import User
from telethon.utils import get_display_name

from bot.config import IMPORTED_DATA_DIR
from bot.db.models import Trainer
from bot.db.repositories import (
    InventoryRepository,
    PokemonRepository,
    TeamRepository,
    TrainerRepository,
    normalize_lookup,
)
from bot.db.session import db_session, run_db_work_async
from bot.game.fusion import has_form_state
from bot.game.move_history import dump_move_history
from bot.game.services.encounters import EncounterService
from bot.game.services.generator import PokemonGeneratorService
from bot.game.services.pokemon_data import PokemonDataService, species_key
from bot.telegram_helpers import safe_event_edit


DAYCARE_LOOP_SECONDS = 5
HUNT_EGG_ODDS = 30000
EGG_INCUBATOR_COST = 20000
BASE_EGG_CYCLE_HOURS = 4.25
BOOSTED_EGG_CYCLE_HOURS = 2.125
HATCH_ACCELERATOR_ABILITIES = {"flamebody", "magmaarmor", "steamengine"}
DAYCARE_SPECIAL_OFFSPRING = {
    ("manaphy", "ditto"): "Phione",
    ("ditto", "manaphy"): "Phione",
    ("phione", "ditto"): "Phione",
    ("ditto", "phione"): "Phione",
}
DAYCARE_COUNTERPARTS = {
    "nidoran-f": ("Nidoran-F", "Nidoran-M"),
    "nidoran-m": ("Nidoran-F", "Nidoran-M"),
    "nidorina": ("Nidoran-F", "Nidoran-M"),
    "nidorino": ("Nidoran-F", "Nidoran-M"),
    "nidoking": ("Nidoran-F", "Nidoran-M"),
    "illumise": ("Volbeat", "Illumise"),
    "volbeat": ("Volbeat", "Illumise"),
}
DAYCARE_PAGE_SIZE = 20
DAYCARE_PICKER_CACHE_SECONDS = 120
FALLBACK_EGG_CYCLES = 20
EVERSTONE_ITEM_KEY = "everstone"
DEFAULT_EGG_FRIENDSHIP = 70
NEUTRAL_BREEDING_NATURE = "Serious"
DOMINANT_EV_NATURES = {
    "atk": "Adamant",
    "def": "Impish",
    "spa": "Modest",
    "spd": "Careful",
    "spe": "Jolly",
}
STAT_ORDER = ("hp", "atk", "def", "spa", "spd", "spe")
POKEAPI_SPECIES_CSV_PATH = IMPORTED_DATA_DIR / "pokeapi_pokemon_species.csv"


def display_name(user: User | None, fallback: str = "Trainer") -> str:
    if not user:
        return fallback
    value = get_display_name(user).strip()
    return value or fallback


def utcnow_ts() -> int:
    return int(datetime.utcnow().timestamp())


def format_source_label(source: str) -> str:
    return "Breeding Egg" if str(source).strip().lower() == "breed" else "Hunt Egg"


def format_seconds_remaining(total_seconds: int) -> str:
    seconds = max(0, int(total_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_unix_eta(timestamp: int) -> str:
    if int(timestamp) <= 0:
        return "Unknown"
    return datetime.utcfromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M UTC")


class DaycareService:
    def __init__(
        self,
        generator: PokemonGeneratorService,
        battle_service,
        pokemon_data: PokemonDataService,
        encounters: EncounterService,
    ) -> None:
        self.generator = generator
        self.battle_service = battle_service
        self.pokemon_data = pokemon_data
        self.encounters = encounters
        self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._egg_cycles = self._load_egg_cycles()
        self._breeding_profile_cache: dict[str, dict[str, Any]] = {}
        self._egg_species_cache: list[str] | None = None
        self._first_picker_cache: dict[int, dict[str, Any]] = {}
        self._partner_picker_cache: dict[tuple[int, int], dict[str, Any]] = {}

    def start_background_tasks(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._background_loop())

    async def _background_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:
                print(f"[daycare] background loop error: {exc}")
            await asyncio.sleep(DAYCARE_LOOP_SECONDS)

    def _load_egg_cycles(self) -> dict[str, int]:
        lookup: dict[str, int] = {}
        path = Path(POKEAPI_SPECIES_CSV_PATH)
        if not path.exists():
            return lookup
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                key = species_key(str(row.get("identifier") or ""))
                if not key:
                    continue
                try:
                    lookup[key] = max(1, int(row.get("hatch_counter") or 0))
                except (TypeError, ValueError):
                    continue
        return lookup

    def _daycare_state(self, trainer: Trainer) -> dict[str, Any]:
        raw = getattr(trainer, "daycare_state_json", None)
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _store_daycare_state(self, trainer: Trainer, state: dict[str, Any]) -> None:
        cleaned = {key: value for key, value in state.items() if value not in (None, [], {}, "")}
        trainer.daycare_state_json = json.dumps(cleaned, sort_keys=True) if cleaned else None

    def _egg_entries(self, trainer: Trainer) -> list[dict[str, Any]]:
        raw = getattr(trainer, "eggs_json", None)
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]

    def _store_egg_entries(self, trainer: Trainer, eggs: list[dict[str, Any]]) -> None:
        trainer.eggs_json = json.dumps(eggs, sort_keys=True) if eggs else None

    async def _breeding_profile(self, species: str) -> dict[str, Any]:
        key = species_key(species)
        cached = self._breeding_profile_cache.get(key)
        if cached is None:
            cached = await self.generator.breeding_profile(species)
            self._breeding_profile_cache[key] = dict(cached)
        return dict(cached)

    async def _breeding_profiles(self, species_names: list[str]) -> dict[str, dict[str, Any]]:
        keys_to_fetch: list[str] = []
        species_by_key: dict[str, str] = {}
        profiles: dict[str, dict[str, Any]] = {}
        for species in species_names:
            key = species_key(species)
            if not key or key in species_by_key:
                continue
            species_by_key[key] = str(species)
            cached = self._breeding_profile_cache.get(key)
            if cached is not None:
                profiles[key] = dict(cached)
            else:
                keys_to_fetch.append(key)
        if keys_to_fetch:
            fetched = await self.generator.breeding_profiles([species_by_key[key] for key in keys_to_fetch])
            for key, payload in fetched.items():
                self._breeding_profile_cache[key] = dict(payload)
                profiles[key] = dict(payload)
        return profiles

    async def _egg_species(self) -> list[str]:
        if self._egg_species_cache is None:
            self._egg_species_cache = await self.generator.list_egg_species()
        return list(self._egg_species_cache)

    def _egg_cycles_for_species(self, species: str) -> int:
        key = species_key(species)
        cycles = self._egg_cycles.get(key)
        if cycles is not None:
            return cycles
        base_key = self.encounters.base_species_key(species)
        cycles = self._egg_cycles.get(base_key)
        if cycles is not None:
            return cycles
        return FALLBACK_EGG_CYCLES

    def _egg_duration_seconds(self, egg_cycles: int, *, accelerated: bool) -> int:
        hours_per_cycle = BOOSTED_EGG_CYCLE_HOURS if accelerated else BASE_EGG_CYCLE_HOURS
        return int(max(1, egg_cycles) * hours_per_cycle * 3600)

    def _egg_base_duration_seconds(self, egg_cycles: int) -> int:
        return self._egg_duration_seconds(egg_cycles, accelerated=False)

    def _hatch_progress_multiplier(self, *, accelerated: bool) -> int:
        return 2 if accelerated else 1

    def _egg_seconds_until_hatch(self, remaining_base_seconds: int, *, accelerated: bool) -> int:
        remaining = max(0, int(remaining_base_seconds))
        multiplier = self._hatch_progress_multiplier(accelerated=accelerated)
        return 0 if remaining <= 0 else (remaining + multiplier - 1) // multiplier

    def _legacy_egg_accelerated(self, egg: dict[str, Any]) -> bool:
        claimed_at = int(egg.get("claimed_at") or 0)
        hatch_ready_at = int(egg.get("hatch_ready_at") or 0)
        if claimed_at <= 0 or hatch_ready_at <= claimed_at:
            return False
        egg_cycles = int(egg.get("egg_cycles") or FALLBACK_EGG_CYCLES)
        legacy_duration = hatch_ready_at - claimed_at
        base_duration = self._egg_base_duration_seconds(egg_cycles)
        boosted_duration = self._egg_duration_seconds(egg_cycles, accelerated=True)
        return abs(legacy_duration - boosted_duration) <= abs(legacy_duration - base_duration)

    def _activate_claimed_egg(self, egg: dict[str, Any], *, claimed_at: int, accelerated: bool) -> dict[str, Any]:
        egg_cycles = int(egg.get("egg_cycles") or FALLBACK_EGG_CYCLES)
        remaining_base_seconds = self._egg_base_duration_seconds(egg_cycles)
        return {
            **egg,
            "claimed_at": claimed_at,
            "remaining_hatch_seconds": remaining_base_seconds,
            "hatch_progress_checked_at": claimed_at,
            "hatch_ready_at": claimed_at + self._egg_seconds_until_hatch(
                remaining_base_seconds,
                accelerated=accelerated,
            ),
        }

    def _sync_claimed_egg_timer(
        self,
        egg: dict[str, Any],
        *,
        accelerated: bool,
        now: int,
    ) -> bool:
        claimed_at = int(egg.get("claimed_at") or 0)
        if claimed_at <= 0:
            return False

        changed = False
        egg_cycles = int(egg.get("egg_cycles") or FALLBACK_EGG_CYCLES)
        base_duration = self._egg_base_duration_seconds(egg_cycles)

        if "remaining_hatch_seconds" not in egg:
            if int(egg.get("hatch_ready_at") or 0) > 0:
                elapsed = max(0, now - claimed_at)
                legacy_multiplier = self._hatch_progress_multiplier(accelerated=self._legacy_egg_accelerated(egg))
                remaining_base_seconds = max(0, base_duration - (elapsed * legacy_multiplier))
            else:
                remaining_base_seconds = base_duration
            egg["remaining_hatch_seconds"] = remaining_base_seconds
            egg["hatch_progress_checked_at"] = min(max(claimed_at, 0), now)
            changed = True

        remaining_base_seconds = max(0, int(egg.get("remaining_hatch_seconds") or 0))
        last_checked_at = int(egg.get("hatch_progress_checked_at") or claimed_at or now)
        if last_checked_at > now:
            last_checked_at = now
            egg["hatch_progress_checked_at"] = now
            changed = True

        if not egg.get("shaking_started_at") and remaining_base_seconds > 0:
            elapsed = max(0, now - last_checked_at)
            if elapsed > 0:
                remaining_base_seconds = max(
                    0,
                    remaining_base_seconds - (elapsed * self._hatch_progress_multiplier(accelerated=accelerated)),
                )
                egg["remaining_hatch_seconds"] = remaining_base_seconds
                egg["hatch_progress_checked_at"] = now
                changed = True

        if egg.get("shaking_started_at") and remaining_base_seconds != 0:
            remaining_base_seconds = 0
            egg["remaining_hatch_seconds"] = 0
            changed = True

        ready_at = now + self._egg_seconds_until_hatch(remaining_base_seconds, accelerated=accelerated)
        if int(egg.get("hatch_ready_at") or 0) != ready_at:
            egg["hatch_ready_at"] = ready_at
            changed = True

        return changed

    def _sorted_owned_pokemon(
        self,
        trainer: Trainer,
        pokemons: PokemonRepository,
        *,
        exclude_ids: set[int] | None = None,
    ) -> list:
        return self.pokemon_data.sort_owned_pokemon(
            pokemons.list_owned_pokemon(trainer, exclude_ids=exclude_ids),
            sort_mode=trainer.sort_mode,
            descending=trainer.sort_descending,
        )

    def _prune_picker_caches(self) -> None:
        now = utcnow_ts()
        stale_first = [
            trainer_id
            for trainer_id, payload in self._first_picker_cache.items()
            if int(payload.get("expires_at") or 0) <= now
        ]
        for trainer_id in stale_first:
            self._first_picker_cache.pop(trainer_id, None)
        stale_partner = [
            key
            for key, payload in self._partner_picker_cache.items()
            if int(payload.get("expires_at") or 0) <= now
        ]
        for key in stale_partner:
            self._partner_picker_cache.pop(key, None)

    def _cached_picker_ids(
        self,
        cache: dict[Any, dict[str, Any]],
        key: Any,
    ) -> list[int] | None:
        self._prune_picker_caches()
        payload = cache.get(key)
        if not payload:
            return None
        return [int(pokemon_id) for pokemon_id in (payload.get("ids") or [])]

    def _store_cached_picker_ids(
        self,
        cache: dict[Any, dict[str, Any]],
        key: Any,
        pokemon_ids: list[int],
    ) -> None:
        cache[key] = {
            "ids": [int(pokemon_id) for pokemon_id in pokemon_ids],
            "expires_at": utcnow_ts() + DAYCARE_PICKER_CACHE_SECONDS,
        }

    def _clear_picker_cache(self, trainer_id: int) -> None:
        self._first_picker_cache.pop(int(trainer_id), None)
        stale_partner = [key for key in self._partner_picker_cache if int(key[0]) == int(trainer_id)]
        for key in stale_partner:
            self._partner_picker_cache.pop(key, None)

    def _picker_page_from_ids(
        self,
        trainer: Trainer,
        pokemons: PokemonRepository,
        *,
        pokemon_ids: list[int],
        page: int,
    ) -> tuple[list, int, int]:
        total = len(pokemon_ids)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // DAYCARE_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * DAYCARE_PAGE_SIZE
        end = start + DAYCARE_PAGE_SIZE
        items = pokemons.list_owned_pokemon_by_ids(trainer, pokemon_ids[start:end])
        return items, total, current_page

    def _pokemon_page(
        self,
        trainer: Trainer,
        pokemons: PokemonRepository,
        *,
        page: int,
        exclude_ids: set[int] | None = None,
    ) -> tuple[list, int, int]:
        items = self._sorted_owned_pokemon(trainer, pokemons, exclude_ids=exclude_ids)
        total = len(items)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // DAYCARE_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * DAYCARE_PAGE_SIZE
        end = start + DAYCARE_PAGE_SIZE
        return items[start:end], total, current_page

    def _team_has_hatch_accelerator(self, trainer: Trainer, teams: TeamRepository) -> bool:
        active_team = teams.get_active_team(trainer)
        for member in teams.team_members(active_team):
            if member is None:
                continue
            if normalize_lookup(member.ability or "") in HATCH_ACCELERATOR_ABILITIES:
                return True
        return False

    def _daycare_status_text(self, state: dict[str, Any]) -> str:
        active_pair = state.get("active_pair") or {}
        pending_egg = state.get("pending_egg") or {}
        cooldown_ends_at = int(state.get("cooldown_ends_at") or 0)
        now = utcnow_ts()

        # The [\u200c](url) trick creates an invisible link to trigger Telegram's rich image preview!
        lines = [
            "[\u200c](https://files.catbox.moe/88suhl.jpg)",
            "**POKEMON DAYCARE**",
            "━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        if active_pair:
            parents = list(active_pair.get("parents") or [])
            lines.append("Status: `Breeding in progress`")
            if len(parents) == 2:
                lines.append(f"Parent 1: **{parents[0].get('species', 'Unknown')}**")
                lines.append(f"Parent 2: **{parents[1].get('species', 'Unknown')}**")
            lines.extend(["", "__We will let you know when the egg is ready.__"])
            return "\n".join(lines)

        if pending_egg:
            lines.append("Status: `Egg ready for pickup`")
            lines.extend(["", "__Pick up the egg when you are ready.__"])
            if cooldown_ends_at > now:
                lines.append("__The daycare is resting after the new egg.__")
            return "\n".join(lines)

        if cooldown_ends_at > now:
            lines.append("Status: `Resting`")
            lines.extend(["", "__Check back soon for the next breeding session.__"])
            return "\n".join(lines)

        lines.append("Status: `Available`")
        lines.extend(["", "__Would you like to leave two Pokemon to breed?__"])
        return "\n".join(lines)

    def _daycare_status_buttons(self, state: dict[str, Any]) -> list[list[Button]]:
        active_pair = state.get("active_pair") or {}
        pending_egg = state.get("pending_egg") or {}
        cooldown_ends_at = int(state.get("cooldown_ends_at") or 0)
        now = utcnow_ts()

        if active_pair:
            return [
                [Button.inline("Withdraw Pair", data="breed:withdraw".encode("utf-8"))],
                [Button.inline("Back", data="breed:leave".encode("utf-8"))],
            ]
        if pending_egg:
            return [
                [Button.inline("Get Egg", data="breed:claim".encode("utf-8"))],
                [Button.inline("Back", data="breed:leave".encode("utf-8"))],
            ]
        if cooldown_ends_at > now:
            return [[Button.inline("Back", data="breed:leave".encode("utf-8"))]]
        return [[
            Button.inline("Enter", data="breed:start".encode("utf-8")),
            Button.inline("Go Back", data="breed:leave".encode("utf-8")),
        ]]

    async def _edit_or_respond(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        text: str,
        *,
        buttons=None,
        parse_mode=None,
        link_preview: bool = False,
    ) -> None:
        if isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(
                event,
                text,
                buttons=buttons,
                parse_mode=parse_mode,
                link_preview=link_preview,
            )
            if edited:
                return
        await event.respond(text, buttons=buttons, parse_mode=parse_mode, link_preview=link_preview)

    def _picker_text(
        self,
        trainer: Trainer,
        *,
        title: str,
        page: int,
        total: int,
        items: list,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // DAYCARE_PAGE_SIZE) + 1)
        lines = ["Daycare", "", title, ""]
        if not items:
            lines.append("You do not have enough Pokemon to do that.")
        else:
            start = page * DAYCARE_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"{index}. {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.extend(["", f"Page: {page + 1}/{max_page}"])
        return "\n".join(lines)

    def _first_picker_buttons(self, *, page: int, total: int, items: list) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * DAYCARE_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(str(index), data=f"breed:sel1:{page}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(items, start=start)
        ]
        for index in range(0, len(number_buttons), 5):
            rows.append(number_buttons[index:index + 5])

        max_page = max(0, (max(total, 1) - 1) // DAYCARE_PAGE_SIZE)
        nav: list[Button] = []
        if page > 0:
            nav.append(Button.inline("<-", data=f"breed:pick1:{page - 1}".encode("utf-8")))
        nav.append(Button.inline(f"{page + 1}/{max_page + 1}", data="breed:noop".encode("utf-8")))
        if page < max_page:
            nav.append(Button.inline("->", data=f"breed:pick1:{page + 1}".encode("utf-8")))
        rows.append(nav)
        rows.append([Button.inline("Back", data="breed:entry".encode("utf-8"))])
        return rows

    def _second_picker_buttons(
        self,
        *,
        first_id: int,
        page: int,
        total: int,
        items: list,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * DAYCARE_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(str(index), data=f"breed:sel2:{first_id}:{page}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(items, start=start)
        ]
        for index in range(0, len(number_buttons), 5):
            rows.append(number_buttons[index:index + 5])

        max_page = max(0, (max(total, 1) - 1) // DAYCARE_PAGE_SIZE)
        nav: list[Button] = []
        if page > 0:
            nav.append(Button.inline("<-", data=f"breed:pick2:{first_id}:{page - 1}".encode("utf-8")))
        nav.append(Button.inline(f"{page + 1}/{max_page + 1}", data="breed:noop".encode("utf-8")))
        if page < max_page:
            nav.append(Button.inline("->", data=f"breed:pick2:{first_id}:{page + 1}".encode("utf-8")))
        rows.append(nav)
        rows.append([Button.inline("Back", data="breed:start".encode("utf-8"))])
        return rows

    async def _breedable_first_pool(
        self,
        trainer: Trainer,
        pokemons: PokemonRepository,
    ) -> list:
        candidates = self._sorted_owned_pokemon(trainer, pokemons)
        filtered: list[Any] = []
        profile_candidates: list[str] = []
        for pokemon in candidates:
            if has_form_state(pokemon):
                continue
            key = species_key(pokemon.species)
            if key != "ditto" and pokemon.gender not in {"M", "F"}:
                continue
            profile_candidates.append(pokemon.species)
        profile_cache = await self._breeding_profiles(profile_candidates)
        for pokemon in candidates:
            if has_form_state(pokemon):
                continue
            key = species_key(pokemon.species)
            if key != "ditto" and pokemon.gender not in {"M", "F"}:
                continue
            profile = profile_cache.get(key)
            if profile is None:
                profile = await self._breeding_profile(pokemon.species)
            if self._is_undiscovered(profile):
                continue
            filtered.append(pokemon)
        return filtered

    def _picker_text(
        self,
        trainer: Trainer,
        *,
        title: str,
        page: int,
        total: int,
        items: list,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // DAYCARE_PAGE_SIZE) + 1)
        lines = [
            "**POKEMON DAYCARE**",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"__{title}__",
            ""
        ]
        if not items:
            lines.append("You do not have enough Pokemon to do that.")
        else:
            start = page * DAYCARE_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"`[{index:<2}]` {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━", f"Page {page + 1}/{max_page}"])
        return "\n".join(lines)

    def _incubate_buttons(self, eggs: list[dict[str, Any]], *, owns_incubator: bool) -> list[list[Button]] | None:
        if not eggs or not owns_incubator:
            return None
        buttons = [
            Button.inline(str(index), data=f"incubate:use:{egg['id']}".encode("utf-8"))
            for index, egg in enumerate(eggs, start=1)
        ]
        rows: list[list[Button]] = []
        for index in range(0, len(buttons), 5):
            rows.append(buttons[index:index + 5])
        return rows

    def _picker_text_plain(
        self,
        trainer: Trainer,
        *,
        title: str,
        page: int,
        total: int,
        items: list,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // DAYCARE_PAGE_SIZE) + 1)
        lines = ["POKEMON DAYCARE", title, ""]
        if not items:
            lines.append("No available Pokemon match this step.")
        else:
            start = page * DAYCARE_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"[{index:>2}] {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.extend(["", f"Page {page + 1}/{max_page}"])
        return "\n".join(lines)

    async def on_breed(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await event.respond("Use /breed in private chat.")
            return
        await self._tick(user_id=event.sender_id)
        await self._show_daycare_status(event, edit=False)

    def _incubate_text(self, eggs: list[dict[str, Any]], *, owns_incubator: bool, egg_energy: int) -> str:
        lines = [
            "[\u200c](https://files.catbox.moe/88suhl.jpg)",
            "**EGG INCUBATOR**",
            "━━━━━━━━━━━━━━━━━━━━━",
            "Choose an egg to hatch instantly.",
            f"Available Energy: `{egg_energy}`",
            "━━━━━━━━━━━━━━━━━━━━━"
        ]
        if not owns_incubator:
            lines.append("_You need to buy an Egg Incubator from the shop first._")
        elif not eggs:
            lines.append("_You do not have any eggs that can use an incubator._")
        else:
            for index, egg in enumerate(eggs, start=1):
                egg_cycles = int(egg.get("egg_cycles") or FALLBACK_EGG_CYCLES)
                species = str((egg.get("pokemon_data") or {}).get("species") or "Unknown")
                lines.append(
                    f"`[{index}]` {format_source_label(str(egg.get('source') or 'breed'))}: "
                    f"**{species}** ({egg_cycles} Energy)"
                )
        return "\n".join(lines)

    def _breeddata_text(
        self,
        trainer: Trainer,
        state: dict[str, Any],
        eggs: list[dict[str, Any]],
        *,
        cooldown_accelerated: bool,
    ) -> str:
        now = utcnow_ts()
        active_pair = state.get("active_pair") or {}
        pending_egg = state.get("pending_egg") or {}
        cooldown_ends_at = int(state.get("cooldown_ends_at") or 0)
        
        lines = [
            "**DAYCARE DATA**",
            "━━━━━━━━━━━━━━━━━━━━━"
        ]

        if active_pair:
            complete_at = int(active_pair.get("complete_at") or 0)
            parents = list(active_pair.get("parents") or [])
            if len(parents) == 2:
                lines.append(f"Parents: **{parents[0].get('species', 'Unknown')}** + **{parents[1].get('species', 'Unknown')}**")
            if complete_at > now:
                lines.append(f"Egg Ready: `{format_seconds_remaining(complete_at - now)}`")
            else:
                lines.append("Egg Ready: `Any moment now`")
            lines.append("━━━━━━━━━━━━━━━━━━━━━")
        elif pending_egg:
            lines.append("Status: `Egg waiting for collection`")
            lines.append("━━━━━━━━━━━━━━━━━━━━━")

        breeding_eggs = [egg for egg in eggs if str(egg.get("source") or "") == "breed"]
        lines.append("**Your Eggs**")
        if breeding_eggs:
            for index, egg in enumerate(breeding_eggs, start=1):
                if egg.get("shaking_started_at"):
                    status = "Shaking right now"
                else:
                    hatch_ready_at = int(egg.get("hatch_ready_at") or 0)
                    claimed_at = int(egg.get("claimed_at") or 0)
                    if hatch_ready_at > now:
                        status = f"Hatches in {format_seconds_remaining(hatch_ready_at - now)}"
                    elif claimed_at:
                        status = "Ready to hatch"
                    else:
                        status = "Waiting to be claimed"
                lines.append(f"`[{index}]` {format_source_label(str(egg.get('source') or 'breed'))}: `{status}`")
        else:
            lines.append("__No eggs in your bag.__")
        
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        if cooldown_ends_at > now:
            lines.append(f"Daycare cooldown: `{format_seconds_remaining(cooldown_ends_at - now)}`")
        elif active_pair:
            lines.append("Daycare cooldown: `Pending current pair`")
        else:
            lines.append("Daycare cooldown: `Ready`")

        return "\n".join(lines)

    def _breeddata_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> str:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return self._breeddata_text(
            trainer,
            self._daycare_state(trainer),
            self._egg_entries(trainer),
            cooldown_accelerated=self._team_has_hatch_accelerator(trainer, teams),
        )

    def _incubate_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        owns_incubator = inventories.key_item_count(trainer, "Egg Incubator") > 0
        eggs = list(self._egg_entries(trainer))
        return {
            "text": self._incubate_text(
                eggs,
                owns_incubator=owns_incubator,
                egg_energy=inventories.egg_energy(trainer),
            ),
            "buttons": self._incubate_buttons(eggs, owns_incubator=owns_incubator),
        }

    def _forcecomplete_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> list[str]:
        changed: list[str] = []
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        state = self._daycare_state(trainer)
        now = utcnow_ts() - 1
        active_pair = state.get("active_pair") or {}
        if active_pair:
            active_pair["complete_at"] = now
            state["active_pair"] = active_pair
            changed.append("breeding")
        if int(state.get("cooldown_ends_at") or 0) > 0:
            state["cooldown_ends_at"] = now
            changed.append("daycare cooldown")
        self._store_daycare_state(trainer, state)

        eggs = self._egg_entries(trainer)
        hatched_targets = 0
        for egg in eggs:
            if egg.get("hatched_at"):
                continue
            if egg.get("shaking_started_at"):
                egg["shaking_started_at"] = now - 10
                egg["remaining_hatch_seconds"] = 0
                egg["hatch_progress_checked_at"] = now
                egg["hatch_ready_at"] = now
                hatched_targets += 1
                continue
            if egg.get("claimed_at"):
                egg["remaining_hatch_seconds"] = 0
                egg["hatch_progress_checked_at"] = now
                egg["hatch_ready_at"] = now
                hatched_targets += 1
        if hatched_targets:
            self._store_egg_entries(trainer, eggs)
            changed.append(f"{hatched_targets} egg(s)")
        return changed

    def _forcecomplete_existing_trainer(self, trainer: Trainer) -> list[str]:
        changed: list[str] = []
        state = self._daycare_state(trainer)
        now = utcnow_ts() - 1
        active_pair = state.get("active_pair") or {}
        if active_pair:
            active_pair["complete_at"] = now
            state["active_pair"] = active_pair
            changed.append("breeding")
        if int(state.get("cooldown_ends_at") or 0) > 0:
            state["cooldown_ends_at"] = now
            changed.append("daycare cooldown")
        self._store_daycare_state(trainer, state)

        eggs = self._egg_entries(trainer)
        hatched_targets = 0
        for egg in eggs:
            if egg.get("hatched_at"):
                continue
            if egg.get("shaking_started_at"):
                egg["shaking_started_at"] = now - 10
                egg["remaining_hatch_seconds"] = 0
                egg["hatch_progress_checked_at"] = now
                egg["hatch_ready_at"] = now
                hatched_targets += 1
                continue
            if egg.get("claimed_at"):
                egg["remaining_hatch_seconds"] = 0
                egg["hatch_progress_checked_at"] = now
                egg["hatch_ready_at"] = now
                hatched_targets += 1
        if hatched_targets:
            self._store_egg_entries(trainer, eggs)
            changed.append(f"{hatched_targets} egg(s)")
        return changed

    async def forcecomplete_user(self, owner_id: int) -> list[str] | None:
        return await run_db_work_async(
            lambda session: (
                self._forcecomplete_existing_trainer(trainer)
                if (trainer := TrainerRepository(session).get_by_telegram_user_id(int(owner_id))) is not None
                else None
            ),
            read_only=False,
        )

    async def forcecomplete_all_users(self) -> dict[int, list[str]]:
        def work(session) -> dict[int, list[str]]:
            changed_by_user: dict[int, list[str]] = {}
            for trainer in session.scalars(select(Trainer).order_by(Trainer.telegram_user_id)):
                changed = self._forcecomplete_existing_trainer(trainer)
                if changed:
                    changed_by_user[int(trainer.telegram_user_id)] = list(changed)
            return changed_by_user

        return await run_db_work_async(work, read_only=False)

    async def on_breeddata(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await event.respond("Use /breeddata in private chat.")
            return
        await self._tick(user_id=event.sender_id)
        sender = await event.get_sender()
        response_text = await run_db_work_async(lambda session: self._breeddata_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(response_text)

    async def on_incubate(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await event.respond("Use /incubate in private chat.")
            return
        await self._tick(user_id=event.sender_id)
        sender = await event.get_sender()
        payload = await run_db_work_async(lambda session: self._incubate_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(payload["text"], buttons=payload["buttons"], parse_mode="md")

    async def on_forcecomplete(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        changed = await run_db_work_async(lambda session: self._forcecomplete_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))

        await self._tick(user_id=event.sender_id)
        if not changed:
            await event.respond("No active daycare or egg timers were running.")
            return
        await event.respond("Force completed: " + ", ".join(changed) + ".")

    async def maybe_find_hunt_egg(self, event: NewMessage.Event) -> bool:
        if not event.is_private:
            return False
        if self.encounters.safari_state(event.sender_id) is not None:
            return False
        can_hunt, _reason = self.battle_service.can_start_hunt(event.sender_id)
        if not can_hunt:
            return False
        if self.encounters.active_by_user.get(event.sender_id) is not None:
            return False
        if random.randint(1, HUNT_EGG_ODDS) != 1:
            return False

        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            teams = TeamRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            species_pool = await self._egg_species()
            if not species_pool:
                return False
            chosen_species = random.choice(species_pool)
            pokemon_data = await self.generator.generate_pokemon(
                species=chosen_species,
                level=1,
                region=trainer.current_region,
                source_kind="egg",
                friendship=DEFAULT_EGG_FRIENDSHIP,
                allow_hidden_ability=False,
                shiny=False,
                item="",
                iv_profile="hunt",
            )
            egg_cycles = self._egg_cycles_for_species(pokemon_data["species"])
            eggs = self._egg_entries(trainer)
            now = utcnow_ts()
            eggs.append(
                self._activate_claimed_egg(
                    {
                        "id": secrets.token_hex(6),
                        "source": "hunt",
                        "egg_cycles": egg_cycles,
                        "created_at": now,
                        "pokemon_data": pokemon_data,
                    },
                    claimed_at=now,
                    accelerated=self._team_has_hatch_accelerator(trainer, teams),
                )
            )
            self._store_egg_entries(trainer, eggs)

        await event.respond("You found a mysterious Egg during your hunt!\nIt has been added to your bag.")
        return True

    async def _show_daycare_status(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        edit: bool,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            state = self._daycare_state(trainer)
            text = self._daycare_status_text(state)
            buttons = self._daycare_status_buttons(state)
        if edit:
            await self._edit_or_respond(event, text, buttons=buttons, parse_mode="md", link_preview=True)
            return
        await event.respond(text, buttons=buttons, parse_mode="md", link_preview=True)

    async def _show_first_picker(self, event: CallbackQuery.Event, *, page: int) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            state = self._daycare_state(trainer)
            if state.get("active_pair") or state.get("pending_egg") or int(state.get("cooldown_ends_at") or 0) > utcnow_ts():
                await self._show_daycare_status(event, edit=True)
                return
            trainer_cache_id = int(trainer.id)
            pokemon_ids = self._cached_picker_ids(self._first_picker_cache, trainer_cache_id)
            if pokemon_ids is None:
                all_items = await self._breedable_first_pool(trainer, pokemons)
                pokemon_ids = [int(pokemon.id) for pokemon in all_items]
                self._store_cached_picker_ids(self._first_picker_cache, trainer_cache_id, pokemon_ids)
            items, total, current_page = self._picker_page_from_ids(
                trainer,
                pokemons,
                pokemon_ids=pokemon_ids,
                page=page,
            )
            await self._edit_or_respond(
                event,
                self._picker_text_plain(
                    trainer,
                    title="Choose the first Pokemon to breed.",
                    page=current_page,
                    total=total,
                    items=items,
                ),
                buttons=self._first_picker_buttons(page=current_page, total=total, items=items),
            )

    async def _compatible_partner_pool(self, trainer: Trainer, pokemons: PokemonRepository, *, first) -> list:
        first_profile = await self._breeding_profile(first.species)
        candidates = self._sorted_owned_pokemon(trainer, pokemons, exclude_ids={int(first.id)})
        candidate_profiles = await self._breeding_profiles([pokemon.species for pokemon in candidates])
        compatible: list[Any] = []
        for candidate in candidates:
            second_profile = candidate_profiles.get(species_key(candidate.species))
            if second_profile is None:
                second_profile = await self._breeding_profile(candidate.species)
            if self._compatibility_error(first, candidate, first_profile, second_profile) is None:
                compatible.append(candidate)
        return compatible

    async def _show_second_picker(self, event: CallbackQuery.Event, *, first_id: int, page: int) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            first = pokemons.get_owned_pokemon(trainer, int(first_id))
            if first is None:
                await self._edit_or_respond(event, "That Pokemon is no longer available.", buttons=None)
                return
            cache_key = (int(trainer.id), int(first_id))
            pokemon_ids = self._cached_picker_ids(self._partner_picker_cache, cache_key)
            if pokemon_ids is None:
                compatible = await self._compatible_partner_pool(trainer, pokemons, first=first)
                pokemon_ids = [int(pokemon.id) for pokemon in compatible]
                self._store_cached_picker_ids(self._partner_picker_cache, cache_key, pokemon_ids)
            items, total, current_page = self._picker_page_from_ids(
                trainer,
                pokemons,
                pokemon_ids=pokemon_ids,
                page=page,
            )
            await self._edit_or_respond(
                event,
                self._picker_text_plain(
                    trainer,
                    title=f"Choose a partner for {first.species}.",
                    page=current_page,
                    total=total,
                    items=items,
                ),
                buttons=self._second_picker_buttons(first_id=int(first_id), page=current_page, total=total, items=items),
            )

    def _compatible_egg_groups(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_groups = {str(group) for group in (left.get("egg_groups") or [])}
        right_groups = {str(group) for group in (right.get("egg_groups") or [])}
        return bool(left_groups & right_groups)

    def _is_undiscovered(self, profile: dict[str, Any]) -> bool:
        return "Undiscovered" in {str(group) for group in (profile.get("egg_groups") or [])}

    def _compatibility_error(
        self,
        first,
        second,
        first_profile: dict[str, Any],
        second_profile: dict[str, Any],
    ) -> str | None:
        first_key = species_key(first.species)
        second_key = species_key(second.species)
        if first_key == "ditto" and second_key == "ditto":
            return "Ditto cannot breed with Ditto."
        if self._is_undiscovered(first_profile) or self._is_undiscovered(second_profile):
            return "Pokemon in the Undiscovered Egg Group cannot breed."
        if first_key == "ditto" or second_key == "ditto":
            return None
        if first.gender == second.gender:
            return "One parent must be male and the other must be female."
        if {first.gender, second.gender} != {"M", "F"}:
            return "Only male and female Pokemon can breed here."
        if not self._compatible_egg_groups(first_profile, second_profile):
            return "Those Pokemon do not share a compatible Egg Group."
        return None

    def _dominant_ev_nature(self, species: str) -> str:
        entry = self.pokemon_data.species_entry(species)
        raw = entry.get("ev_yield") or []
        best_stat: str | None = None
        best_value = 0
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            stat_name = str(item[0]).replace("-", "_").lower()
            try:
                amount = int(item[1])
            except (TypeError, ValueError):
                continue
            stat_key = {
                "special_attack": "spa",
                "special_defense": "spd",
                "attack": "atk",
                "defense": "def",
                "speed": "spe",
                "hp": "hp",
            }.get(stat_name, stat_name)
            if stat_key in STAT_ORDER and amount > best_value:
                best_value = amount
                best_stat = stat_key
        return DOMINANT_EV_NATURES.get(str(best_stat or ""), NEUTRAL_BREEDING_NATURE)

    def _breeding_moves(self, profile: dict[str, Any]) -> list[str]:
        egg_moves = [
            str(entry.get("name") or "").strip()
            for entry in (profile.get("egg_moves") or [])
            if isinstance(entry, dict) and str(entry.get("name") or "").strip()
        ]
        if len(egg_moves) >= 4:
            return random.sample(egg_moves, 4)

        chosen = list(egg_moves)
        level_entries = [
            entry
            for entry in (profile.get("level_up_moves") or [])
            if isinstance(entry, dict) and str(entry.get("name") or "").strip()
        ]
        level_entries.sort(key=lambda entry: (int(entry.get("level") or 999), str(entry.get("name") or "")))
        for entry in level_entries:
            move_name = str(entry.get("name") or "").strip()
            if move_name and move_name not in chosen:
                chosen.append(move_name)
            if len(chosen) >= 4:
                break
        return chosen[:4]

    def _best_parent_ivs(self, first, second) -> dict[str, int]:
        inherited_pool = []
        for stat in STAT_ORDER:
            best_value = max(int(getattr(first, f"iv_{stat}")), int(getattr(second, f"iv_{stat}")))
            inherited_pool.append((stat, best_value))
        inherited_pool.sort(key=lambda item: item[1], reverse=True)

        chosen_stats: list[str] = []
        index = 0
        while index < len(inherited_pool) and len(chosen_stats) < 3:
            value = inherited_pool[index][1]
            tied_stats = [stat for stat, stat_value in inherited_pool[index:] if stat_value == value]
            remaining_slots = 3 - len(chosen_stats)
            if len(tied_stats) <= remaining_slots:
                chosen_stats.extend(tied_stats)
                index += len(tied_stats)
                continue
            chosen_stats.extend(random.sample(tied_stats, remaining_slots))
            break

        chosen_set = set(chosen_stats[:3])
        ivs: dict[str, int] = {}
        for stat in STAT_ORDER:
            if stat in chosen_set:
                ivs[stat] = max(int(getattr(first, f"iv_{stat}")), int(getattr(second, f"iv_{stat}")))
            else:
                ivs[stat] = random.randint(21, 31)
        return ivs

    def _apply_everstone_form(self, offspring_species: str, first, second, trainer: Trainer) -> str:
        native_species = self.encounters.apply_regional_form(offspring_species, trainer.current_region)
        native_base = self.encounters.base_species_key(native_species)
        for parent in (first, second):
            if normalize_lookup(parent.item or "") != EVERSTONE_ITEM_KEY:
                continue
            if self.encounters.base_species_key(parent.species) != native_base:
                continue
            if species_key(parent.species) != species_key(native_species):
                return parent.species
        return native_species

    def _offspring_species(
        self,
        *,
        first,
        second,
        trainer: Trainer,
        first_profile: dict[str, Any],
        second_profile: dict[str, Any],
    ) -> str:
        first_key = species_key(first.species)
        second_key = species_key(second.species)
        special = DAYCARE_SPECIAL_OFFSPRING.get((first_key, second_key))
        if special:
            return special

        if first_key == "ditto":
            source_parent = second
            source_profile = second_profile
        elif second_key == "ditto":
            source_parent = first
            source_profile = first_profile
        else:
            source_parent = first if first.gender == "F" else second
            source_profile = first_profile if source_parent is first else second_profile

        source_key = species_key(source_parent.species)
        if source_key in DAYCARE_COUNTERPARTS:
            return random.choice(list(DAYCARE_COUNTERPARTS[source_key]))

        base_species = str(source_profile.get("base_egg_species") or source_parent.species)
        return self._apply_everstone_form(base_species, first, second, trainer)

    def _offspring_shiny(self, first, second) -> bool:
        shiny_parents = int(bool(first.shiny)) + int(bool(second.shiny))
        shiny_odds = 674
        if shiny_parents == 1:
            shiny_odds = 449
        elif shiny_parents >= 2:
            shiny_odds = 337
        return random.randint(1, shiny_odds) == 1

    def _breeding_duration_seconds(
        self,
        first,
        second,
        first_profile: dict[str, Any],
        second_profile: dict[str, Any],
    ) -> int:
        avg_friendship = (int(first.friendship) + int(second.friendship)) / 2
        avg_bst = (int(first_profile.get("bst") or 0) + int(second_profile.get("bst") or 0)) / 2
        minutes = 30 + max(0, int(round((avg_bst - 250) / 4))) - int(round(avg_friendship / 6))
        minutes = max(30, min(minutes, 240))
        return minutes * 60

    async def _build_breeding_egg(self, trainer: Trainer, first, second) -> dict[str, Any]:
        first_profile = await self._breeding_profile(first.species)
        second_profile = await self._breeding_profile(second.species)
        offspring_species = self._offspring_species(
            first=first,
            second=second,
            trainer=trainer,
            first_profile=first_profile,
            second_profile=second_profile,
        )
        offspring_profile = await self._breeding_profile(offspring_species)
        abilities = list(offspring_profile.get("abilities") or [])
        hidden_entry = next((entry for entry in abilities if bool(entry.get("hidden"))), None)
        chosen_ability = str((hidden_entry or abilities[0] if abilities else {}).get("name") or "")
        pokemon_data = await self.generator.generate_pokemon(
            species=offspring_species,
            level=1,
            region=trainer.current_region,
            source_kind="egg",
            friendship=DEFAULT_EGG_FRIENDSHIP,
            allow_hidden_ability=True,
            shiny=self._offspring_shiny(first, second),
            item="",
            ivs=self._best_parent_ivs(first, second),
            moves=self._breeding_moves(offspring_profile),
            nature=self._dominant_ev_nature(offspring_species),
            ability=chosen_ability or None,
        )
        egg_move_keys = {
            normalize_lookup(str(entry.get("name") or ""))
            for entry in list(offspring_profile.get("egg_moves") or [])
            if isinstance(entry, dict) and normalize_lookup(str(entry.get("name") or ""))
        }
        remembered_egg_moves = [
            str(move_name)
            for move_name in list(pokemon_data.get("moves") or [])
            if normalize_lookup(str(move_name)) in egg_move_keys
        ]
        if remembered_egg_moves:
            pokemon_data["move_history_json"] = dump_move_history({"egg": remembered_egg_moves})
        return {
            "id": secrets.token_hex(6),
            "source": "breed",
            "created_at": utcnow_ts(),
            "egg_cycles": self._egg_cycles_for_species(pokemon_data["species"]),
            "pokemon_data": pokemon_data,
        }

    async def _start_breeding(self, event: CallbackQuery.Event, *, first_id: int, second_id: int) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            state = self._daycare_state(trainer)
            if state.get("active_pair"):
                await event.answer("You already have a breeding pair in the daycare.", alert=True)
                return
            if state.get("pending_egg"):
                await event.answer("Pick up your waiting egg first.", alert=True)
                return
            if int(state.get("cooldown_ends_at") or 0) > utcnow_ts():
                await event.answer("The daycare is resting right now.", alert=True)
                return

            first = pokemons.get_owned_pokemon(trainer, int(first_id))
            second = pokemons.get_owned_pokemon(trainer, int(second_id))
            if first is None or second is None or first.id == second.id:
                await event.answer("Choose two different Pokemon that you still own.", alert=True)
                return

            first_profile = await self._breeding_profile(first.species)
            second_profile = await self._breeding_profile(second.species)
            error_text = self._compatibility_error(first, second, first_profile, second_profile)
            if error_text:
                await event.answer(error_text, alert=True)
                return

            egg_payload = await self._build_breeding_egg(trainer, first, second)
            duration_seconds = self._breeding_duration_seconds(first, second, first_profile, second_profile)
            state["active_pair"] = {
                "parents": [
                    {"pokemon_id": int(first.id), "species": first.species},
                    {"pokemon_id": int(second.id), "species": second.species},
                ],
                "started_at": utcnow_ts(),
                "complete_at": utcnow_ts() + duration_seconds,
                "egg": egg_payload,
            }
            self._store_daycare_state(trainer, state)

        await self._show_daycare_status(event, edit=True)
        await event.answer("Breeding started.")

    async def _claim_pending_egg(self, event: CallbackQuery.Event) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            teams = TeamRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            state = self._daycare_state(trainer)
            pending_egg = state.get("pending_egg")
            if not isinstance(pending_egg, dict):
                await event.answer("There is no egg waiting right now.", alert=True)
                return
            eggs = self._egg_entries(trainer)
            claimed_at = utcnow_ts()
            eggs.append(
                self._activate_claimed_egg(
                    pending_egg,
                    claimed_at=claimed_at,
                    accelerated=self._team_has_hatch_accelerator(trainer, teams),
                )
            )
            self._store_egg_entries(trainer, eggs)
            state.pop("pending_egg", None)
            self._store_daycare_state(trainer, state)

        await safe_event_edit(event, "The Egg was moved to your bag.", buttons=None)
        await event.answer("Egg received.")

    async def _withdraw_active_pair(self, event: CallbackQuery.Event) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            state = self._daycare_state(trainer)
            if not state.get("active_pair"):
                await event.answer("There is no active breeding pair right now.", alert=True)
                return
            state.pop("active_pair", None)
            self._store_daycare_state(trainer, state)
        await safe_event_edit(event, "The breeding pair was withdrawn.\nBreeding was cancelled.", buttons=None)
        await event.answer("Breeding cancelled.")

    def _hatch_egg_in_session(
        self,
        trainer: Trainer,
        pokemons: PokemonRepository,
        trainers: TrainerRepository,
        eggs: list[dict[str, Any]],
        egg: dict[str, Any],
    ) -> dict[str, Any]:
        pokemon_data = dict(egg.get("pokemon_data") or {})
        pokemon_data["source_kind"] = "egg"
        pokemon_data["origin_region"] = pokemon_data.get("origin_region") or trainer.current_region
        pokemon_data["untradeable"] = True
        owned = pokemons.create_owned_pokemon(trainer=trainer, data=pokemon_data)
        trainers.place_in_first_party_slot(trainer, owned)
        eggs[:] = [entry for entry in eggs if str(entry.get("id") or "") != str(egg.get("id") or "")]
        return {"species": owned.species, "shiny": bool(owned.shiny)}

    async def _use_incubator(self, event: CallbackQuery.Event, egg_id: str) -> None:
        sender = await event.get_sender()
        hatch_notice: dict[str, Any] | None = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            eggs = self._egg_entries(trainer)
            target = next((egg for egg in eggs if str(egg.get("id") or "") == egg_id), None)
            if target is None:
                await event.answer("That egg is no longer available.", alert=True)
                return
            if inventories.key_item_count(trainer, "Egg Incubator") <= 0:
                await event.answer("You need an Egg Incubator first.", alert=True)
                return
            egg_cycles = int(target.get("egg_cycles") or FALLBACK_EGG_CYCLES)
            if not inventories.consume_egg_energy(trainer, egg_cycles):
                await event.answer("You do not have enough Egg Energy.", alert=True)
                return

            hatch_notice = self._hatch_egg_in_session(trainer, pokemons, trainers, eggs, target)
            self._store_egg_entries(trainer, eggs)

        if hatch_notice is not None:
            await self._send_hatch_notice(event.sender_id, hatch_notice, immediate=True)
        await safe_event_edit(event, "The Egg Incubator finished the egg instantly.", buttons=None)
        await event.answer("Egg hatched.")

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        data = event.data.decode("utf-8")
        if not (data.startswith("breed:") or data.startswith("incubate:")):
            return False
        if not event.is_private:
            await event.answer("Use daycare in private chat.", alert=True)
            return True

        await self._tick(user_id=event.sender_id)

        if data == "breed:noop":
            await event.answer()
            return True
        if data == "breed:leave":
            await safe_event_edit(event, "Hope we see you soon...", buttons=None)
            await event.answer()
            return True
        if data in {"breed:entry", "breed:start"}:
            if data == "breed:start":
                sender = await event.get_sender()
                with db_session() as session:
                    trainers = TrainerRepository(session)
                    trainer = trainers.ensure_trainer(
                        telegram_user_id=event.sender_id,
                        username=getattr(sender, "username", None),
                        display_name=display_name(sender),
                    )
                    state = self._daycare_state(trainer)
                    if (
                        not state.get("active_pair")
                        and not state.get("pending_egg")
                        and int(state.get("cooldown_ends_at") or 0) <= utcnow_ts()
                    ):
                        self._clear_picker_cache(int(trainer.id))
                        await self._show_first_picker(event, page=0)
                        await event.answer()
                        return True
            await self._show_daycare_status(event, edit=True)
            await event.answer()
            return True
        if data == "breed:withdraw":
            await self._withdraw_active_pair(event)
            return True
        if data == "breed:claim":
            await self._claim_pending_egg(event)
            return True
        if data.startswith("breed:pick1:"):
            await self._show_first_picker(event, page=int(data.split(":")[-1]))
            await event.answer()
            return True
        if data.startswith("breed:sel1:"):
            parts = data.split(":")
            if len(parts) != 4:
                await event.answer("Unknown daycare action.", alert=True)
                return True
            await self._show_second_picker(event, first_id=int(parts[3]), page=0)
            await event.answer()
            return True
        if data.startswith("breed:pick2:"):
            parts = data.split(":")
            if len(parts) != 4:
                await event.answer("Unknown daycare action.", alert=True)
                return True
            await self._show_second_picker(event, first_id=int(parts[2]), page=int(parts[3]))
            await event.answer()
            return True
        if data.startswith("breed:sel2:"):
            parts = data.split(":")
            if len(parts) != 5:
                await event.answer("Unknown daycare action.", alert=True)
                return True
            await self._start_breeding(event, first_id=int(parts[2]), second_id=int(parts[4]))
            return True
        if data.startswith("incubate:use:"):
            await self._use_incubator(event, data.split(":", 2)[2])
            return True

        await event.answer("Unknown daycare action.", alert=True)
        return True

    async def _tick(self, user_id: int | None = None) -> None:
        async with self._tick_lock:
            notifications = await run_db_work_async(lambda session: self._tick_payload(session, user_id=user_id))

            for kind, target_user_id, payload in notifications:
                if kind == "breed_complete":
                    await self.battle_service.client.send_message(target_user_id, "Your Pokemon has breeded.")
                    continue
                if kind == "egg_shaking":
                    await self.battle_service.client.send_message(target_user_id, "Oh! The egg is shaking!")
                    continue
                if kind == "egg_hatched":
                    await self._send_hatch_notice(target_user_id, payload, immediate=False)

    def _tick_payload(self, session, *, user_id: int | None) -> list[tuple[str, int, dict[str, Any]]]:
        notifications: list[tuple[str, int, dict[str, Any]]] = []
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        teams = TeamRepository(session)
        trainer_query = select(Trainer)
        if user_id is not None:
            trainer_query = trainer_query.where(Trainer.telegram_user_id == int(user_id))
        else:
            trainer_query = trainer_query.where(
                or_(
                    Trainer.daycare_state_json.is_not(None),
                    Trainer.eggs_json.is_not(None),
                )
            )
        trainer_rows = list(session.scalars(trainer_query))

        for trainer in trainer_rows:
            state = self._daycare_state(trainer)
            eggs = self._egg_entries(trainer)
            now = utcnow_ts()
            state_changed = False
            eggs_changed = False
            team_accelerated = self._team_has_hatch_accelerator(trainer, teams)

            active_pair = state.get("active_pair") or {}
            if active_pair and int(active_pair.get("complete_at") or 0) <= now:
                pending_egg = dict(active_pair.get("egg") or {})
                state.pop("active_pair", None)
                if pending_egg:
                    state["pending_egg"] = pending_egg
                    egg_cycles = int(pending_egg.get("egg_cycles") or FALLBACK_EGG_CYCLES)
                    cooldown_seconds = self._egg_duration_seconds(
                        egg_cycles,
                        accelerated=team_accelerated,
                    )
                    state["cooldown_ends_at"] = now + cooldown_seconds
                    notifications.append(("breed_complete", int(trainer.telegram_user_id), {}))
                state_changed = True

            cooldown_ends_at = int(state.get("cooldown_ends_at") or 0)
            if cooldown_ends_at and cooldown_ends_at <= now:
                state.pop("cooldown_ends_at", None)
                state_changed = True

            hatch_notices: list[dict[str, Any]] = []
            for egg in list(eggs):
                if not egg.get("claimed_at"):
                    continue
                if self._sync_claimed_egg_timer(egg, accelerated=team_accelerated, now=now):
                    eggs_changed = True
                hatch_ready_at = int(egg.get("hatch_ready_at") or 0)
                shaking_started_at = int(egg.get("shaking_started_at") or 0)
                if not shaking_started_at and hatch_ready_at and hatch_ready_at <= now:
                    egg["shaking_started_at"] = now
                    eggs_changed = True
                    notifications.append(("egg_shaking", int(trainer.telegram_user_id), {}))
                    continue
                if shaking_started_at and (now - shaking_started_at) >= 5:
                    hatch_notices.append(self._hatch_egg_in_session(trainer, pokemons, trainers, eggs, egg))
                    eggs_changed = True

            if state_changed:
                self._store_daycare_state(trainer, state)
            if eggs_changed:
                self._store_egg_entries(trainer, eggs)
            for notice in hatch_notices:
                notifications.append(("egg_hatched", int(trainer.telegram_user_id), notice))

        return notifications

    async def _send_hatch_notice(self, user_id: int, payload: dict[str, Any], *, immediate: bool) -> None:
        species = str(payload.get("species") or "Pokemon")
        shiny = bool(payload.get("shiny"))
        title = f"{species} 🥚"
        text = f"{title} hatched!" if immediate else f"{title} hatched from the egg!"
        candidates = self.pokemon_data.artwork_candidates(species, shiny=shiny)
        for candidate in candidates:
            try:
                await self.battle_service.client.send_message(user_id, text, file=candidate)
                return
            except Exception:
                continue
        await self.battle_service.client.send_message(user_id, text)
