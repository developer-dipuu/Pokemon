from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Sequence

from bot.bridge.dex_tools import run_dex_tool
from bot.config import BOT_DIR, EXP_CHART_PATH, GENERATOR_DEFAULTS_PATH, GROWTH_DATA_PATH, SHOWDOWN_DIR


class PokemonGeneratorService:
    def __init__(self, defaults_path: Path = GENERATOR_DEFAULTS_PATH) -> None:
        self.defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
        self.growth_data = json.loads(GROWTH_DATA_PATH.read_text(encoding="utf-8"))
        self.exp_chart = json.loads(EXP_CHART_PATH.read_text(encoding="utf-8"))

    def _species_key(self, value: str) -> str:
        text = value.strip().lower().replace("♀", "-f").replace("♂", "-m")
        text = text.replace(" ", "-").replace(".", "").replace("'", "")
        return re.sub(r"[^a-z0-9-]+", "", text)

    def _starting_experience(self, species: str, level: int) -> int:
        growth = self.growth_data.get(self._species_key(species), {})
        growth_rate = str(growth.get("growth_rate", "medium"))
        curve = self.exp_chart.get(growth_rate, {})
        return int(curve.get(str(max(1, min(int(level), 100))), 0))

    def _roll_ivs(self, tier: str) -> dict[str, int]:
        """Rolls IVs based on the encounter tier."""
        stats = ["hp", "atk", "def", "spa", "spd", "spe"]
        
        # Start with standard wild 0-31 rolls
        ivs = {stat: random.randint(0, 31) for stat in stats}
        
        guaranteed_perfect = 0
        stat_floor = 0
        
        # Define our scenarios
        if tier == "starter":
            guaranteed_perfect = 3
            stat_floor = 10
        elif tier == "legendary":
            guaranteed_perfect = 3
            stat_floor = 15
        elif tier == "safari":
            guaranteed_perfect = 1
            stat_floor = 5
        # "hunt" or "normal" defaults to 0 guaranteed and 0 floor
        
        # Apply the floor
        if stat_floor > 0:
            for stat in stats:
                ivs[stat] = max(stat_floor, ivs[stat])
                
        # Apply guaranteed perfect stats
        if guaranteed_perfect > 0:
            perfect_stats = random.sample(stats, guaranteed_perfect)
            for stat in perfect_stats:
                ivs[stat] = 31
                
        return ivs

    def _roll_budgeted_ivs(self, *, total_min: int, total_max: int, floor: int = 0) -> dict[str, int]:
        stats = ["hp", "atk", "def", "spa", "spd", "spe"]
        target_min = max(int(total_min), int(floor) * len(stats))
        target_max = min(31 * len(stats), max(target_min, int(total_max)))
        target_total = random.randint(target_min, target_max)
        order = random.sample(stats, len(stats))
        ivs: dict[str, int] = {}
        remaining = target_total

        for index, stat in enumerate(order):
            remaining_slots = len(order) - index - 1
            min_allowed = remaining if remaining_slots == 0 else max(int(floor), remaining - (remaining_slots * 31))
            max_allowed = remaining if remaining_slots == 0 else min(31, remaining - (remaining_slots * int(floor)))
            value = random.randint(min_allowed, max_allowed)
            ivs[stat] = value
            remaining -= value

        return ivs

    def _weekend_boost_ivs(self) -> dict[str, int]:
        return self._roll_budgeted_ivs(total_min=104, total_max=148, floor=5)

    async def generate_pokemon(
        self,
        *,
        species: str,
        level: int,
        region: str,
        source_kind: str,
        friendship: int | None = None,
        allow_hidden_ability: bool | None = None,
        shiny: bool = False,
        item: str = "",
        untradeable: bool = False,
        unreleasable: bool = False,
        ivs: dict[str, int] | None = None,
        evs: dict[str, int] | None = None,
        moves: list[str] | None = None,
        nature: str | None = None,
        ability: str | None = None,
        gender: str | None = None,
        tera_type: str | None = None,
        iv_profile: str | None = None,
    ) -> dict[str, Any]:
        payload = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "generate-pokemon",
                "species": species,
                "level": int(level),
                "friendship": int(friendship if friendship is not None else self.defaults.get("wild_friendship", 70)),
                "allowHiddenAbility": bool(
                    self.defaults["allow_hidden_ability_for_starters"]
                    if allow_hidden_ability is None
                    else allow_hidden_ability
                ),
                "legalMinEvs": bool(self.defaults["legal_min_evs"]),
                "formatid": self.defaults["default_formatid"],
                "mod": self.defaults["default_mod"],
                "item": item,
                "shiny": shiny,
                "ivs": ivs,
                "evs": evs,
                "moves": moves,
                "nature": nature,
                "ability": ability,
                "gender": gender,
                "teraType": tera_type,
                "ivProfile": iv_profile,
            },
        )

        return {
            "species": payload.get("species", species),
            "nickname": None,
            "origin_region": region,
            "source_kind": source_kind,
            "level": payload.get("level", level),
            "experience": self._starting_experience(payload.get("species", species), payload.get("level", level)),
            "friendship": payload.get("friendship", 70),
            "ability": payload.get("ability", "Unknown"),
            "nature": payload.get("nature", "Serious"),
            "gender": payload.get("gender", ""),
            "item": payload.get("item", ""),
            "status": "",
            "tera_type": str(payload.get("teraType") or (payload.get("types", [""])[0] if payload.get("types") else "")),
            "current_hp": payload.get("currentHp", 10),
            "max_hp": payload.get("maxHp", 10),
            "shiny": shiny,
            "untradeable": bool(untradeable or str(source_kind).strip().lower() == "egg"),
            "unreleasable": unreleasable,
            "ivs": payload.get("ivs", {"hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0}),
            "evs": payload.get("evs", {"hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0}),
            "moves": payload.get("moves", ["Tackle"]),
            "export_text": payload.get("exportText", ""),
            "packed_set": payload.get("packedTeam", ""),
            "generator_problems": payload.get("problems") or [],
            "total_iv": payload.get("totalIv", 0),
            "types": payload.get("types") or [],
            "current_hp_exact": payload.get("currentHp", 10),
            "max_hp_exact": payload.get("maxHp", 10),
        }

    async def generate_starter(self, *, species: str, region: str) -> dict[str, Any]:
        custom_ivs = self._roll_ivs(tier="starter")
        
        return await self.generate_pokemon(
            species=species,
            level=int(self.defaults["starter_level"]),
            region=region,
            source_kind="starter",
            friendship=int(self.defaults["starter_friendship"]),
            allow_hidden_ability=bool(self.defaults["allow_hidden_ability_for_starters"]),
            shiny=False,
            item="",
            untradeable=bool(self.defaults["starter_untradeable"]),
            unreleasable=bool(self.defaults["starter_unreleasable"]),
            ivs=custom_ivs,
        )

    async def generate_wild(
        self,
        *,
        species: str,
        level: int,
        region: str,
        source_kind: str,
        shiny: bool = False,
        item: str = "",
        iv_profile: str | None = None,
        weekend_boost: bool = False,
    ) -> dict[str, Any]:
        custom_ivs = self._weekend_boost_ivs() if weekend_boost else None
        return await self.generate_pokemon(
            species=species,
            level=level,
            region=region,
            source_kind=source_kind,
            friendship=int(self.defaults.get("wild_friendship", 70)),
            allow_hidden_ability=bool(self.defaults.get("allow_hidden_ability_for_wild", False)),
            shiny=shiny,
            item=item,
            ivs=custom_ivs,
            untradeable=False,
            unreleasable=False,
            iv_profile=iv_profile or source_kind,
        )

    async def get_levelup_moves(self, species: str, level: int) -> list[str]:
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "get-levelup-moves",
                "species": species,
                "level": level,
                "mod": self.defaults["default_mod"],
            }
        )
        return result.get("moves") or []

    async def list_held_items(self) -> list[str]:
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "list-held-items",
                "mod": self.defaults["default_mod"],
            },
        )
        return [str(item) for item in (result.get("items") or []) if str(item).strip()]

    async def list_training_moves(self, species: str) -> list[dict[str, Any]]:
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "list-training-moves",
                "species": species,
                "mod": self.defaults["default_mod"],
            },
        )
        moves: list[dict[str, Any]] = []
        for entry in result.get("moves") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            move_id = str(entry.get("id") or "").strip()
            methods = [str(value) for value in (entry.get("methods") or []) if str(value).strip()]
            level = entry.get("level")
            try:
                level_value = int(level) if level is not None else None
            except (TypeError, ValueError):
                level_value = None
            moves.append({
                "id": move_id,
                "name": name,
                "methods": methods,
                "level": level_value,
            })
        return moves

    async def list_abilities(self, species: str) -> list[dict[str, Any]]:
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "list-abilities",
                "species": species,
                "mod": self.defaults["default_mod"],
            },
        )
        abilities: list[dict[str, Any]] = []
        for entry in result.get("abilities") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            abilities.append({
                "slot": str(entry.get("slot") or "").strip(),
                "name": name,
                "hidden": bool(entry.get("hidden")),
                "special": bool(entry.get("special")),
            })
        return abilities

    async def breeding_profile(self, species: str) -> dict[str, Any]:
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "get-breeding-profile",
                "species": species,
                "mod": self.defaults["default_mod"],
            },
        )
        return {
            "species": str(result.get("species") or species),
            "base_species": str(result.get("baseSpecies") or result.get("species") or species),
            "base_egg_species": str(result.get("baseEggSpecies") or result.get("species") or species),
            "egg_groups": [str(value) for value in (result.get("eggGroups") or []) if str(value).strip()],
            "gender": str(result.get("gender") or ""),
            "gender_ratio": dict(result.get("genderRatio") or {}),
            "can_hatch": bool(result.get("canHatch")),
            "bst": int(result.get("bst") or 0),
            "abilities": list(result.get("abilities") or []),
            "egg_moves": list(result.get("eggMoves") or []),
            "level_up_moves": list(result.get("levelUpMoves") or []),
        }

    async def breeding_profiles(self, species_list: Sequence[str]) -> dict[str, dict[str, Any]]:
        requested_species = [str(species).strip() for species in species_list if str(species).strip()]
        if not requested_species:
            return {}
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "get-breeding-profiles",
                "speciesList": requested_species,
                "mod": self.defaults["default_mod"],
            },
        )
        profiles: dict[str, dict[str, Any]] = {}
        for entry in result.get("profiles") or []:
            if not isinstance(entry, dict):
                continue
            payload = {
                "species": str(entry.get("species") or ""),
                "base_species": str(entry.get("baseSpecies") or entry.get("species") or ""),
                "base_egg_species": str(entry.get("baseEggSpecies") or entry.get("species") or ""),
                "egg_groups": [str(value) for value in (entry.get("eggGroups") or []) if str(value).strip()],
                "gender": str(entry.get("gender") or ""),
                "gender_ratio": dict(entry.get("genderRatio") or {}),
                "can_hatch": bool(entry.get("canHatch")),
                "bst": int(entry.get("bst") or 0),
                "abilities": list(entry.get("abilities") or []),
                "egg_moves": list(entry.get("eggMoves") or []),
                "level_up_moves": list(entry.get("levelUpMoves") or []),
            }
            key = self._species_key(payload["species"])
            if key:
                profiles[key] = payload
        return profiles

    async def list_egg_species(self) -> list[str]:
        result = await run_dex_tool(
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            payload={
                "type": "list-egg-species",
                "mod": self.defaults["default_mod"],
            },
        )
        return [str(item) for item in (result.get("species") or []) if str(item).strip()]
