from __future__ import annotations

import json
import math
import re
from functools import cached_property
from pathlib import Path
from typing import Any

from bot.config import (
    BASE_STATS_PATH,
    EVOLUTION_CHAINS_PATH,
    EXP_CHART_PATH,
    GROWTH_DATA_PATH,
    MOVE_INFO_PATH,
    SHINY_ART_PATH,
    SPECIES_REFERENCE_PATH,
)
from bot.db.models import OwnedPokemon
from bot.db.repositories import (
    DISPLAY_ATK,
    DISPLAY_CATEGORY,
    DISPLAY_DEF,
    DISPLAY_EVS,
    DISPLAY_HP,
    DISPLAY_IVS,
    DISPLAY_LEVEL,
    DISPLAY_NATURE,
    DISPLAY_NONE,
    DISPLAY_SPA,
    DISPLAY_SPD,
    DISPLAY_SPE,
    DISPLAY_TOTAL,
    DISPLAY_TYPE,
    DISPLAY_TYPE_SYMBOL,
    SORT_ATK,
    SORT_CAUGHT,
    SORT_CATEGORY,
    SORT_DEF,
    SORT_EVS,
    SORT_HP,
    SORT_IVS,
    SORT_LEVEL,
    SORT_NAME,
    SORT_NONE,
    SORT_POKEDEX,
    SORT_SPA,
    SORT_SPD,
    SORT_SPE,
    SORT_TOTAL,
    pokemon_total_ev,
)
from bot.game.fusion import (
    effective_ability,
    effective_moves,
    effective_species,
    has_form_state,
    load_form_state,
    lookup_species_name,
)


TYPE_ICONS = {
    "normal": "🔘",
    "fire": "🔥",
    "water": "💧",
    "electric": "⚡",
    "grass": "🌿",
    "ice": "❄️",
    "fighting": "🥊",
    "poison": "☠️",
    "ground": "🌍",
    "flying": "🪽",
    "psychic": "🔮",
    "bug": "🐛",
    "rock": "🪨",
    "ghost": "👻",
    "dragon": "🐉",
    "dark": "🌑",
    "steel": "⚙️",
    "fairy": "✨",
}

NATURES = {
    "Hardy": (None, None),
    "Lonely": ("atk", "def"),
    "Brave": ("atk", "spe"),
    "Adamant": ("atk", "spa"),
    "Naughty": ("atk", "spd"),
    "Bold": ("def", "atk"),
    "Docile": (None, None),
    "Relaxed": ("def", "spe"),
    "Impish": ("def", "spa"),
    "Lax": ("def", "spd"),
    "Timid": ("spe", "atk"),
    "Hasty": ("spe", "def"),
    "Serious": (None, None),
    "Jolly": ("spe", "spa"),
    "Naive": ("spe", "spd"),
    "Modest": ("spa", "atk"),
    "Mild": ("spa", "def"),
    "Quiet": ("spa", "spe"),
    "Bashful": (None, None),
    "Rash": ("spa", "spd"),
    "Calm": ("spd", "atk"),
    "Gentle": ("spd", "def"),
    "Sassy": ("spd", "spe"),
    "Careful": ("spd", "spa"),
    "Quirky": (None, None),
}

STAT_LABELS = {
    "hp": "HP",
    "atk": "Attack",
    "def": "Defense",
    "spa": "Special Attack",
    "spd": "Special Defense",
    "spe": "Speed",
}

BASE_STAT_KEYS = {
    "hp": "hp",
    "atk": "attack",
    "def": "defense",
    "spa": "special_attack",
    "spd": "special_defense",
    "spe": "speed",
}

LOCAL_SPRITE_ROOT = Path(__file__).resolve().parents[3] / "assets" / "sprite"

DEFAULT_ITEM_EVOLUTION_LEVEL = 36
ITEM_EVOLUTION_LEVEL_OVERRIDES = {
    "charcadet": 30,
}
IMMEDIATE_EVOLUTION_SPECIES = {
    "azurill",
    "budew",
    "buneary",
    "chansey",
    "chingling",
    "cleffa",
    "eevee",
    "golbat",
    "igglybuff",
    "munchlax",
    "pichu",
    "riolu",
    "snom",
    "swadloon",
    "togepi",
    "type-null",
    "woobat",
}
EVOLUTION_GENDER_RULES = {
    ("burmy", "mothim"): "M",
    ("burmy", "wormadam"): "F",
    ("combee", "vespiquen"): "F",
    ("kirlia", "gallade"): "M",
    ("salandit", "salazzle"): "F",
    ("snorunt", "froslass"): "F",
}
SPECIAL_EVOLUTION_BRANCHES = {
    ("kubfu", "urshifu"): ["Urshifu-Single-Strike", "Urshifu-Rapid-Strike"],
}
REGIONAL_EVOLUTION_OVERRIDES = {
    "alola": {
        ("cubone", "marowak"): "Marowak-Alola",
        ("exeggcute", "exeggutor"): "Exeggutor-Alola",
        ("pikachu", "raichu"): "Raichu-Alola",
    },
    "galar": {
        ("koffing", "weezing"): "Weezing-Galar",
        ("mime-jr", "mr-mime"): "Mr. Mime-Galar",
    },
    "hisui": {
        ("bergmite", "avalugg"): "Avalugg-Hisui",
        ("goomy", "sliggoo"): "Sliggoo-Hisui",
        ("growlithe", "arcanine"): "Arcanine-Hisui",
        ("petilil", "lilligant"): "Lilligant-Hisui",
        ("rufflet", "braviary"): "Braviary-Hisui",
        ("sliggoo", "goodra"): "Goodra-Hisui",
        ("voltorb", "electrode"): "Electrode-Hisui",
    },
}


def species_key(value: str) -> str:
    text = value.strip().lower().replace("♀", "-f").replace("♂", "-m")
    text = text.replace(" ", "-").replace(".", "").replace("'", "")
    return re.sub(r"[^a-z0-9-]+", "", text)


def move_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def artwork_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", species_key(value))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▒" * width
    ratio = max(0.0, min(1.0, current / total))
    filled = round(ratio * width)
    return ("█" * filled) + ("▒" * (width - filled))


class PokemonDataService:
    @cached_property
    def species_reference(self) -> dict[str, dict[str, Any]]:
        return load_json(SPECIES_REFERENCE_PATH)

    @cached_property
    def growth_data(self) -> dict[str, dict[str, Any]]:
        return load_json(GROWTH_DATA_PATH)

    @cached_property
    def base_stats(self) -> dict[str, dict[str, int]]:
        return load_json(BASE_STATS_PATH)

    @cached_property
    def exp_chart(self) -> dict[str, dict[str, int]]:
        return load_json(EXP_CHART_PATH)

    @cached_property
    def move_info(self) -> dict[str, dict[str, Any]]:
        raw = load_json(MOVE_INFO_PATH)
        by_name: dict[str, dict[str, Any]] = {}
        for value in raw.values():
            if not isinstance(value, dict) or not value.get("name"):
                continue
            by_name[move_key(str(value["name"]))] = value
        return by_name

    @cached_property
    def move_info_by_id(self) -> dict[str, dict[str, Any]]:
        raw = load_json(MOVE_INFO_PATH)
        return {
            str(key): value
            for key, value in raw.items()
            if isinstance(value, dict) and value.get("name")
        }

    @cached_property
    def evolution_map(self) -> dict[str, list[dict[str, Any]]]:
        raw = load_json(EVOLUTION_CHAINS_PATH).get("evolution_chains", [])
        mapping: dict[str, list[dict[str, Any]]] = {}
        for item in raw:
            current = species_key(str(item.get("current_pokemon", "")))
            if not current:
                continue
            mapping.setdefault(current, []).append(item)
        return mapping

    @cached_property
    def shiny_art(self) -> dict[str, str]:
        raw = load_json(SHINY_ART_PATH)
        mapping: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("name") and item.get("shiny_url"):
                mapping[species_key(str(item["name"]))] = str(item["shiny_url"])
        return mapping

    @cached_property
    def local_artwork(self) -> dict[str, dict[str, str]]:
        mapping: dict[str, dict[str, str]] = {"normal": {}, "shiny": {}}
        folders = {
            "normal": LOCAL_SPRITE_ROOT / "image",
            "shiny": LOCAL_SPRITE_ROOT / "image-shiny",
        }
        for variant, folder in folders.items():
            if not folder.exists():
                continue
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                key = artwork_key(path.stem)
                if key and key not in mapping[variant]:
                    mapping[variant][key] = str(path)
        return mapping

    @cached_property
    def species_by_pokedex_number(self) -> dict[int, str]:
        grouped: dict[int, list[str]] = {}
        for key, payload in self.species_reference.items():
            if not isinstance(payload, dict):
                continue
            value = payload.get("pokedex_number")
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(number, []).append(str(payload.get("name") or key))

        return {
            number: sorted(names, key=self._species_choice_sort_key)[0]
            for number, names in grouped.items()
            if names
        }

    def _species_choice_sort_key(self, species: str) -> tuple[int, int, int, str]:
        key = species_key(species)
        special_tokens = (
            "mega",
            "gmax",
            "totem",
            "primal",
            "origin",
            "therian",
            "school",
            "blade",
            "busted",
            "crowned",
            "eternamax",
        )
        is_special = any(token in key for token in special_tokens)
        return (1 if "-" in key else 0, 1 if is_special else 0, len(key), key)

    def species_entry(self, species: str) -> dict[str, Any]:
        return self.species_reference.get(species_key(species), {})

    def species_name(self, species: str) -> str:
        entry = self.species_entry(species)
        value = str(entry.get("name") or "").strip()
        if value:
            return value
        return str(species).replace("-", " ").title()

    def artwork_candidates(self, species: str, *, shiny: bool = False) -> list[str]:
        key = species_key(species)
        local_key = artwork_key(species)
        candidates: list[str] = []
        
        # Priority 1: Local Shiny Sprite
        if shiny:
            local_shiny = self.local_artwork["shiny"].get(local_key)
            if local_shiny:
                candidates.append(local_shiny)
                
        # Priority 2: Local Normal Sprite (Fallback for missing shinies)
        local_normal = self.local_artwork["normal"].get(local_key)
        if local_normal:
            candidates.append(local_normal)

        seen: set[str] = set()
        unique: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                unique.append(candidate)
                
        return unique

    def artwork_url(self, species: str, *, shiny: bool = False) -> str | None:
        candidates = self.artwork_candidates(species, shiny=shiny)
        return candidates[0] if candidates else None

    def types_for_species(self, species: str) -> list[str]:
        entry = self.species_entry(species)
        raw = entry.get("types") or []
        return [str(item) for item in raw]

    def formatted_types(self, species: str) -> str:
        types = self.types_for_species(species)
        if not types:
            return "Unknown"
        return " / ".join(f"{name.title()} {TYPE_ICONS.get(name.lower(), '')}".strip() for name in types)

    def plain_types(self, species: str) -> str:
        types = self.types_for_species(species)
        if not types:
            return "Unknown"
        return "/".join(name.title() for name in types)

    def type_symbols(self, species: str) -> str:
        types = self.types_for_species(species)
        if not types:
            return "-"
        return "".join(TYPE_ICONS.get(name.lower(), "?") for name in types)

    def pokedex_number(self, species: str) -> int | None:
        entry = self.species_entry(species)
        value = entry.get("pokedex_number")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def species_from_pokedex_number(self, pokedex_number: int) -> str | None:
        try:
            return self.species_by_pokedex_number.get(int(pokedex_number))
        except (TypeError, ValueError):
            return None

    def _display_move_name(self, value: str) -> str:
        parts = [part for part in str(value).replace("-", " ").split() if part]
        return " ".join(part.upper() if len(part) == 1 else part.capitalize() for part in parts)

    def move_name_from_id(self, move_id: str | int) -> str | None:
        entry = self.move_info_by_id.get(str(move_id).strip())
        raw_name = str(entry.get("name") or "").strip() if isinstance(entry, dict) else ""
        if not raw_name:
            return None
        return self._display_move_name(raw_name)

    def move_entries(self, query: str = "") -> list[tuple[str, str]]:
        query_text = str(query or "").strip()
        query_key = move_key(query_text)
        entries: list[tuple[str, str]] = []
        for move_id, payload in self.move_info_by_id.items():
            raw_name = str(payload.get("name") or "").strip()
            if not raw_name:
                continue
            display_name = self._display_move_name(raw_name)
            if query_key:
                if query_text not in move_id and query_key not in move_key(raw_name) and query_key not in move_key(display_name):
                    continue
            entries.append((move_id, display_name))
        return sorted(
            entries,
            key=lambda item: (
                0 if query_text and str(item[0]) == query_text else 1,
                item[1].lower(),
                int(item[0]),
            ),
        )

    def category_label(self, pokemon: OwnedPokemon) -> str:
        mapping = {
            "starter": "Starter",
            "hunt": "Wild",
            "wild": "Wild",
            "safari": "Safari",
            "egg": "Egg",
        }
        key = str(pokemon.source_kind or "").strip().lower()
        if not key:
            return "Unknown"
        return mapping.get(key, key.replace("_", " ").title())

    def stat_points(self, pokemon: OwnedPokemon, stat_key: str) -> int:
        return int(self.calculate_stats(pokemon).get(stat_key, 0))

    def total_stat_points(self, pokemon: OwnedPokemon) -> int:
        return sum(self.calculate_stats(pokemon).values())

    def collection_entry_suffix(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        if display_mode == DISPLAY_NONE:
            return ""
        if display_mode == DISPLAY_LEVEL:
            return f"lv {pokemon.level}"
        if display_mode == DISPLAY_NATURE:
            return pokemon.nature
        if display_mode == DISPLAY_TYPE:
            return self.plain_types(pokemon.species)
        if display_mode == DISPLAY_TYPE_SYMBOL:
            return self.type_symbols(pokemon.species)
        if display_mode == DISPLAY_CATEGORY:
            return self.category_label(pokemon)
        if display_mode == DISPLAY_IVS:
            return f"iv {pokemon.total_iv}"
        if display_mode == DISPLAY_EVS:
            return f"ev {pokemon_total_ev(pokemon)}"
        if display_mode == DISPLAY_HP:
            return f"HP {self.stat_points(pokemon, 'hp')}"
        if display_mode == DISPLAY_ATK:
            return f"Attack {self.stat_points(pokemon, 'atk')}"
        if display_mode == DISPLAY_DEF:
            return f"Defense {self.stat_points(pokemon, 'def')}"
        if display_mode == DISPLAY_SPA:
            return f"Sp. Atk {self.stat_points(pokemon, 'spa')}"
        if display_mode == DISPLAY_SPD:
            return f"Sp. Def {self.stat_points(pokemon, 'spd')}"
        if display_mode == DISPLAY_SPE:
            return f"Speed {self.stat_points(pokemon, 'spe')}"
        if display_mode == DISPLAY_TOTAL:
            return f"Total {self.total_stat_points(pokemon)}"
        return ""

    def collection_entry_text(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        name = f"{pokemon.species}{' ✨' if pokemon.shiny else ''}"
        suffix = self.collection_entry_suffix(pokemon, display_mode)
        if not suffix:
            return name
        if display_mode == DISPLAY_LEVEL:
            return f"{name} {suffix}"
        return f"{name} - {suffix}"

    def sort_value(self, pokemon: OwnedPokemon, sort_mode: str) -> tuple[Any, ...]:
        species_name = str(pokemon.species).lower()
        if sort_mode == SORT_NAME:
            return (species_name, pokemon.id)
        if sort_mode == SORT_POKEDEX:
            return (self.pokedex_number(pokemon.species) or 999999, species_name, pokemon.id)
        if sort_mode == SORT_LEVEL:
            return (pokemon.level, species_name, pokemon.id)
        if sort_mode == SORT_CATEGORY:
            return (self.category_label(pokemon).lower(), species_name, pokemon.id)
        if sort_mode == SORT_IVS:
            return (pokemon.total_iv, species_name, pokemon.id)
        if sort_mode == SORT_EVS:
            return (pokemon_total_ev(pokemon), species_name, pokemon.id)
        if sort_mode == SORT_HP:
            return (self.stat_points(pokemon, "hp"), species_name, pokemon.id)
        if sort_mode == SORT_ATK:
            return (self.stat_points(pokemon, "atk"), species_name, pokemon.id)
        if sort_mode == SORT_DEF:
            return (self.stat_points(pokemon, "def"), species_name, pokemon.id)
        if sort_mode == SORT_SPA:
            return (self.stat_points(pokemon, "spa"), species_name, pokemon.id)
        if sort_mode == SORT_SPD:
            return (self.stat_points(pokemon, "spd"), species_name, pokemon.id)
        if sort_mode == SORT_SPE:
            return (self.stat_points(pokemon, "spe"), species_name, pokemon.id)
        if sort_mode == SORT_TOTAL:
            return (self.total_stat_points(pokemon), species_name, pokemon.id)
        if sort_mode in {SORT_NONE, SORT_CAUGHT}:
            return (pokemon.id,)
        return (pokemon.id,)

    def sort_owned_pokemon(
        self,
        pokemon_list: list[OwnedPokemon],
        *,
        sort_mode: str,
        descending: bool,
    ) -> list[OwnedPokemon]:
        return sorted(
            list(pokemon_list),
            key=lambda pokemon: self.sort_value(pokemon, sort_mode),
            reverse=bool(descending),
        )

    def ev_yield_text(self, species: str) -> str:
        entry = self.species_entry(species)
        raw = entry.get("ev_yield") or []
        gains = []
        for stat_name, amount in raw:
            if int(amount) <= 0:
                continue
            label = str(stat_name).replace("-", " ").title()
            gains.append(f"{label} +{int(amount)}")
        if not gains:
            return "EV Yield: None"
        return "EV Yield\n" + "\n".join(gains)

    def growth_rate(self, species: str) -> str:
        entry = self.growth_data.get(species_key(species), {})
        return str(entry.get("growth_rate", "medium"))

    def base_experience(self, species: str) -> int:
        entry = self.growth_data.get(species_key(species), {})
        value = entry.get("base_experience", 100)
        try:
            return int(value) if value is not None else 100
        except (TypeError, ValueError):
            return 100

    def level_curve_value(self, growth_rate: str, level: int) -> int:
        curve = self.exp_chart.get(growth_rate, {})
        return int(curve.get(str(level), 0))

    def starting_experience(self, species: str, level: int) -> int:
        growth = self.growth_rate(species)
        return self.level_curve_value(growth, max(1, min(int(level), 100)))

    def exp_progress(self, pokemon: OwnedPokemon) -> tuple[int, int]:
        growth = self.growth_rate(pokemon.species)
        lvl_total = self.level_curve_value(growth, pokemon.level)
        next_lvl_total = self.level_curve_value(growth, min(pokemon.level + 1, 100))
        
        # Calculate how much EXP we have gained since the start of this level
        level_exp = max(0, pokemon.experience - lvl_total)
        # Calculate how much EXP is needed to go from the current level to the next
        level_needed = max(1, next_lvl_total - lvl_total)
        
        return int(level_exp), int(level_needed)

    def nature_marks(self, nature_name: str) -> tuple[str | None, str | None]:
        return NATURES.get(nature_name, (None, None))

    def calculate_stats(self, pokemon: OwnedPokemon) -> dict[str, int]:
        base = self.base_stats.get(species_key(pokemon.species), {})
        plus, minus = self.nature_marks(pokemon.nature)
        values: dict[str, int] = {}
        for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
            base_stat = int(base.get(BASE_STAT_KEYS[stat], 0))
            iv = int(getattr(pokemon, f"iv_{stat}"))
            ev = int(getattr(pokemon, f"ev_{stat}"))
            if stat == "hp":
                if base_stat == 1:
                    values[stat] = 1
                else:
                    values[stat] = math.floor(((2 * base_stat + iv + math.floor(ev / 4)) * pokemon.level) / 100) + pokemon.level + 10
                continue
            raw = math.floor(((2 * base_stat + iv + math.floor(ev / 4)) * pokemon.level) / 100) + 5
            multiplier = 1.1 if plus == stat else 0.9 if minus == stat else 1.0
            values[stat] = math.floor(raw * multiplier)
        return values

    def item_page_text(self, pokemon: OwnedPokemon) -> str:
        item = pokemon.item or "None"
        status = pokemon.status or "Healthy"
        return (
            f"Held Item: {item}\n"
            f"Status: {status}\n"
            f"Tera Type: {pokemon.tera_type or 'None'}\n"
            f"Untradeable: {'Yes' if pokemon.untradeable else 'No'}\n"
            f"Unreleasable: {'Yes' if pokemon.unreleasable else 'No'}"
        )

    def _evolution_required_level(self, current_key: str, option: dict[str, Any]) -> int:
        if current_key in IMMEDIATE_EVOLUTION_SPECIES:
            return 0

        raw_level = option.get("evolution_level")
        try:
            if raw_level is not None:
                return max(0, int(raw_level))
        except (TypeError, ValueError):
            pass

        method = str(option.get("evolution_method") or "").strip().lower()
        if method == "use-item":
            return int(ITEM_EVOLUTION_LEVEL_OVERRIDES.get(current_key, DEFAULT_ITEM_EVOLUTION_LEVEL))
        return 0

    def _evolution_gender_rule(self, current_key: str, evolved_key: str) -> str | None:
        return EVOLUTION_GENDER_RULES.get((current_key, evolved_key))

    def _resolved_evolution_species(self, current_key: str, evolved_species: str, region_id: str | None) -> list[str]:
        evolved_key = species_key(evolved_species)
        special_choices = SPECIAL_EVOLUTION_BRANCHES.get((current_key, evolved_key))
        if special_choices is not None:
            return list(special_choices)
        target_region = str(region_id or "").strip().lower()
        if not target_region:
            return [evolved_species]
        return [REGIONAL_EVOLUTION_OVERRIDES.get(target_region, {}).get((current_key, evolved_key), evolved_species)]

    def evolution_choices(self, pokemon: OwnedPokemon, *, region_id: str | None = None) -> list[dict[str, Any]]:
        current_key = species_key(pokemon.species)
        if current_key.endswith("-gmax"):
            return []
        options = self.evolution_map.get(current_key, [])
        choices: list[dict[str, Any]] = []

        for option in options:
            base_target = self.species_name(str(option.get("evolved_pokemon") or ""))
            for evolved_species in self._resolved_evolution_species(current_key, base_target, region_id):
                evolved_key = species_key(evolved_species)
                if not evolved_key:
                    continue

                required_level = self._evolution_required_level(current_key, option)
                gender_rule = self._evolution_gender_rule(current_key, evolved_key)
                gender_ok = gender_rule is None or pokemon.gender == gender_rule
                level_ok = int(pokemon.level) >= required_level
                ready = gender_ok and level_ok

                blockers: list[str] = []
                if not level_ok:
                    blockers.append(f"level {required_level}")
                if gender_rule == "M" and not gender_ok:
                    blockers.append("male only")
                elif gender_rule == "F" and not gender_ok:
                    blockers.append("female only")

                if ready:
                    status_text = "Ready now"
                elif blockers:
                    status_text = "Needs " + ", ".join(blockers)
                else:
                    status_text = "Not ready"

                choices.append(
                    {
                        "species": evolved_species,
                        "target_key": evolved_key,
                        "required_level": required_level,
                        "gender_rule": gender_rule,
                        "ready": ready,
                        "status_text": status_text,
                    }
                )

        return choices

    def eligible_evolution_choices(self, pokemon: OwnedPokemon, *, region_id: str | None = None) -> list[dict[str, Any]]:
        return [choice for choice in self.evolution_choices(pokemon, region_id=region_id) if bool(choice.get("ready"))]

    def evolution_choice(self, pokemon: OwnedPokemon, evolved_species: str, *, region_id: str | None = None) -> dict[str, Any] | None:
        target_key = species_key(evolved_species)
        return next(
            (choice for choice in self.evolution_choices(pokemon, region_id=region_id) if str(choice.get("target_key")) == target_key),
            None,
        )

    def evolution_page_text(self, pokemon: OwnedPokemon, *, region_id: str | None = None) -> str:
        choices = self.evolution_choices(pokemon, region_id=region_id)
        if not choices:
            if species_key(pokemon.species).endswith("-gmax"):
                return f"{pokemon.species} cannot evolve while it is in its Gmax form."
            return f"{pokemon.species} has no recorded evolution path."

        lines = [
            f"Evolution Paths: {pokemon.species}",
            f"Current level: {pokemon.level}",
            "",
        ]
        for choice in choices:
            lines.append(f"{choice['species']}: {choice['status_text']}")

        ready_count = len([choice for choice in choices if bool(choice.get("ready"))])
        if ready_count == 1:
            lines.extend(["", "Press the button below to evolve now."])
        elif ready_count > 1:
            lines.extend(["", "Choose one of the buttons below to evolve."])
        else:
            lines.extend(["", "Keep leveling and check back here."])

        return "\n".join(lines)

    def summary_text(self, pokemon: OwnedPokemon) -> str:
        current_exp, needed_exp = self.exp_progress(pokemon)
        next_exp = 0 if pokemon.level >= 100 else max(needed_exp - current_exp, 0)
        progress_total = 1 if pokemon.level >= 100 else needed_exp
        progress_current = progress_total if pokemon.level >= 100 else current_exp
        
        display_species = effective_species(pokemon)
        lookup_species = lookup_species_name(display_species)
        
        # Format Title
        title_name = pokemon.nickname or display_species
        if pokemon.nickname and pokemon.nickname != display_species:
            title_name = f"{pokemon.nickname} ({display_species})"
            
        shiny_icon = " ✨" if pokemon.shiny else ""
        egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
        
        # Format Gender
        if pokemon.gender == "M":
            gender_icon = " ♂️"
        elif pokemon.gender == "F":
            gender_icon = " ♀️"
        else:
            gender_icon = " ⚧" # Genderless/Unknown
            
        title = f"**{title_name}**{egg_icon}{shiny_icon}{gender_icon}".strip()
        
        lines = [
            title,
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 **Level:** `{pokemon.level}`",
            f"🧬 **Type:** {self.formatted_types(lookup_species)}",
            f"🎭 **Nature:** {pokemon.nature}",
            f"🌟 **Ability:** {effective_ability(pokemon)}",
            f"🎒 **Held Item:** {pokemon.item or 'None'}",
            f"❤️ **HP:** `{pokemon.current_hp} / {pokemon.max_hp}`",
            f"🤝 **Friendship:** `{pokemon.friendship} / 255`",
            "",
            f"🔵 **EXP:** `{pokemon.experience:,}`",
            f"📈 **Next Level:** `{next_exp:,}` EXP",
            f"`{progress_bar(progress_current, progress_total, width=10)}`"
        ]
        return "\n".join(lines)

    def collection_entry_text(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
        shiny_icon = " ✨" if pokemon.shiny else ""
        name = f"{pokemon.species}{egg_icon}{shiny_icon}"
        suffix = self.collection_entry_suffix(pokemon, display_mode)
        if not suffix:
            return name
        if display_mode == DISPLAY_LEVEL:
            return f"{name} {suffix}"
        return f"{name} - {suffix}"

    def summary_text(self, pokemon: OwnedPokemon) -> str:
        current_exp, needed_exp = self.exp_progress(pokemon)
        next_exp = max(needed_exp - current_exp, 0)
        types = self.formatted_types(pokemon.species)
        gender_icon = "♀️" if pokemon.gender == "F" else "♂️" if pokemon.gender == "M" else "⚲"
        nickname_text = f" \"{pokemon.nickname}\"" if pokemon.nickname else ""
        shiny_icon = "✨" if pokemon.shiny else ""
        egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
        title = f"{shiny_icon}**{pokemon.species}{egg_icon}**{nickname_text} {gender_icon}".strip()
        lines = [
            f"📊 {title} — **Lv. {pokemon.level}**",
            f"🧬 **Types:** {types}",
            f"🎨 **Nature:** {pokemon.nature} | 🌟 **Ability:** {pokemon.ability}",
            f"🎒 **Held Item:** {pokemon.item or 'None'}",
            f"❤️ **HP:** {pokemon.current_hp} / {pokemon.max_hp}",
            f"📈 **Exp:** {current_exp} / {needed_exp} (Next: {next_exp})",
            f"`{progress_bar(current_exp, needed_exp, width=10)}`",
            f"🤔 **Friendship:** {pokemon.friendship}/255",
        ]
        return "\n".join(lines)

    def stats_page_text(self, pokemon: OwnedPokemon) -> str:
        values = self.calculate_stats(pokemon)
        plus, minus = self.nature_marks(pokemon.nature)
        
        lines = [f"📈 **Combat Stats for {pokemon.species}**\n"]
        for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
            suffix = ""
            if stat == plus:
                suffix = " 🔺 **(+10%)**"
            elif stat == minus:
                suffix = " 🔻 **(-10%)**"
                
            lines.append(f"**{STAT_LABELS[stat]}:** {values[stat]}{suffix}")
        return "\n".join(lines)

    def iv_evs_text(self, pokemon: OwnedPokemon) -> str:
        rows = [
            ("HP ",  pokemon.iv_hp,  pokemon.ev_hp),
            ("Atk", pokemon.iv_atk, pokemon.ev_atk),
            ("Def", pokemon.iv_def, pokemon.ev_def),
            ("SpA", pokemon.iv_spa, pokemon.ev_spa),
            ("SpD", pokemon.iv_spd, pokemon.ev_spd),
            ("Spe", pokemon.iv_spe, pokemon.ev_spe),
        ]
        total_ev = sum(row[2] for row in rows)
        lines = [
            f"🧪 **IVs & EVs \u2014 {pokemon.species}**",
            "🔯 `Stat  IV   EV`",
            "━" * 18,
        ]
        for label, iv_value, ev_value in rows:
            lines.append(f"`{label}    {iv_value:>3}  {ev_value:>3}`")
        lines.extend([
            "━" * 18,
            f"`{'Tot'}   {pokemon.total_iv:>3}  {total_ev:>3}/510`",
        ])
        return "\n".join(lines)

    def moves_page_text(self, pokemon: OwnedPokemon) -> str:
        move_names = json.loads(pokemon.moves_json)
        lines = [f"⚔️ **Moveset for {pokemon.species}**\n"]
        
        if not move_names:
            return lines[0] + "No moves recorded."
            
        for move_name in move_names:
            info = self.move_info.get(move_key(str(move_name)), {})
            move_type = str(info.get("type", "?"))
            power = info.get("power", "--")
            accuracy = info.get("accuracy", "--")
            category = str(info.get("category", "Unknown")).title()
            
            type_label = f"{move_type.title()} {TYPE_ICONS.get(move_type.lower(), '')}"
            
            lines.append(f"🔹 **{move_name}** [{type_label}]")
            lines.append(f"└ Pwr: {power} | Acc: {accuracy} | Cat: {category}\n")
            
        return "\n".join(lines).strip()

    def collection_entry_text(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        name = pokemon.species + (" ✨" if pokemon.shiny else "")
        suffix = self.collection_entry_suffix(pokemon, display_mode)
        if not suffix:
            return name
        if display_mode == DISPLAY_LEVEL:
            return f"{name} {suffix}"
        return f"{name} - {suffix}"

    def summary_text(self, pokemon: OwnedPokemon) -> str:
        current_exp, needed_exp = self.exp_progress(pokemon)
        next_exp = 0 if pokemon.level >= 100 else max(needed_exp - current_exp, 0)
        progress_total = 1 if pokemon.level >= 100 else needed_exp
        progress_current = progress_total if pokemon.level >= 100 else current_exp
        gender_icon = "♀️" if pokemon.gender == "F" else "♂️" if pokemon.gender == "M" else ""
        shiny_icon = "✨" if pokemon.shiny else ""
        title_name = pokemon.nickname or pokemon.species
        if pokemon.nickname and pokemon.nickname != pokemon.species:
            title_name = f"{pokemon.nickname} ({pokemon.species})"
        title = f"➤ {title_name}{shiny_icon}"
        if gender_icon:
            title = f"{title} {gender_icon}"

        lines = [
            title,
            f"Level: {pokemon.level} | Nature: {pokemon.nature}",
            f"Ability: {pokemon.ability}",
            f"Held Item: {pokemon.item or 'None'}",
            f"Types: {self.plain_types(pokemon.species)}",
            f"EXP: {pokemon.experience:,}",
            f"Need To Next Level: {next_exp:,}",
            progress_bar(progress_current, progress_total, width=10),
            f"Friendship: {pokemon.friendship}/255",
        ]
        return "\n".join(lines)

    def stats_page_text(self, pokemon: OwnedPokemon) -> str:
        values = self.calculate_stats(pokemon)
        plus, minus = self.nature_marks(pokemon.nature)
        lines = [f"Combat Stats: {pokemon.species}", ""]
        for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
            suffix = ""
            if stat == plus:
                suffix = " (+10%)"
            elif stat == minus:
                suffix = " (-10%)"
            lines.append(f"{STAT_LABELS[stat]}: {values[stat]}{suffix}")
        return "\n".join(lines)

    def iv_evs_text(self, pokemon: OwnedPokemon) -> str:
        rows = [
            ("HP ",  pokemon.iv_hp,  pokemon.ev_hp),
            ("Atk", pokemon.iv_atk, pokemon.ev_atk),
            ("Def", pokemon.iv_def, pokemon.ev_def),
            ("SpA", pokemon.iv_spa, pokemon.ev_spa),
            ("SpD", pokemon.iv_spd, pokemon.ev_spd),
            ("Spe", pokemon.iv_spe, pokemon.ev_spe),
        ]
        total_ev = sum(row[2] for row in rows)
        lines = [
            f"IV / EVs: {pokemon.species}",
            "`Stat  IV   EV`",
            "━" * 18,
        ]
        for label, iv_value, ev_value in rows:
            lines.append(f"`{label}    {iv_value:>3}  {ev_value:>3}`")
        lines.extend([
            "━" * 18,
            f"`{'Tot'}   {pokemon.total_iv:>3}  {total_ev:>3}/510`",
        ])
        return "\n".join(lines)

    def moves_page_text(self, pokemon: OwnedPokemon) -> str:
        move_names = json.loads(pokemon.moves_json)
        lines = [f"Moves: {pokemon.species}", ""]
        if not move_names:
            return "\n".join(lines + ["No moves recorded."])

        for index, move_name in enumerate(move_names, start=1):
            info = self.move_info.get(move_key(str(move_name)), {})
            move_type = str(info.get("type", "?")).title()
            power = info.get("power", "--")
            accuracy = info.get("accuracy", "--")
            category = str(info.get("category", "Unknown")).title()
            lines.append(f"{index}. {self._display_move_name(str(move_name))}")
            lines.append(f"Type: {move_type} | Power: {power} | Accuracy: {accuracy} | Category: {category}")
            if index != len(move_names):
                lines.append("")
        return "\n".join(lines).strip()

    def collection_entry_text(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
        shiny_icon = " ✨" if pokemon.shiny else ""
        name = f"{pokemon.species}{egg_icon}{shiny_icon}"
        suffix = self.collection_entry_suffix(pokemon, display_mode)
        if not suffix:
            return name
        if display_mode == DISPLAY_LEVEL:
            return f"{name} {suffix}"
        return f"{name} - {suffix}"

    def summary_text(self, pokemon: OwnedPokemon) -> str:
        current_exp, needed_exp = self.exp_progress(pokemon)
        next_exp = 0 if pokemon.level >= 100 else max(needed_exp - current_exp, 0)
        progress_total = 1 if pokemon.level >= 100 else needed_exp
        progress_current = progress_total if pokemon.level >= 100 else current_exp
        gender_icon = "♀️" if pokemon.gender == "F" else "♂️" if pokemon.gender == "M" else "⚲"
        shiny_icon = " ✨" if pokemon.shiny else ""
        egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
        title_name = pokemon.nickname or pokemon.species
        if pokemon.nickname and pokemon.nickname != pokemon.species:
            title_name = f"{pokemon.nickname} ({pokemon.species})"
        title = f" {title_name}{egg_icon}{shiny_icon}"
        if gender_icon:
            title = f"{title} {gender_icon}"

        lines = [
            title,
            f"Level: {pokemon.level}",
            f"Types: {self.plain_types(pokemon.species)}",
            f"Nature: {pokemon.nature}",
            f"Ability: {pokemon.ability}",
            f"Held Item: {pokemon.item or 'None'}",
            f"HP: {pokemon.current_hp} / {pokemon.max_hp}",
            f"EXP: {progress_current} / {progress_total} (Next: {next_exp})",
            f"Friendship: {pokemon.friendship}/255",
        ]
        return "\n".join(lines)

    def collection_entry_suffix(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        display_species = effective_species(pokemon)
        lookup_species = lookup_species_name(display_species)
        if display_mode == DISPLAY_NONE:
            return ""
        if display_mode == DISPLAY_LEVEL:
            return f"lv {pokemon.level}"
        if display_mode == DISPLAY_NATURE:
            return pokemon.nature
        if display_mode == DISPLAY_TYPE:
            return self.plain_types(lookup_species)
        if display_mode == DISPLAY_TYPE_SYMBOL:
            return self.type_symbols(lookup_species)
        if display_mode == DISPLAY_CATEGORY:
            return self.category_label(pokemon)
        if display_mode == DISPLAY_IVS:
            return f"iv {pokemon.total_iv}"
        if display_mode == DISPLAY_EVS:
            return f"ev {pokemon_total_ev(pokemon)}"
        if display_mode == DISPLAY_HP:
            return f"HP {self.stat_points(pokemon, 'hp')}"
        if display_mode == DISPLAY_ATK:
            return f"Attack {self.stat_points(pokemon, 'atk')}"
        if display_mode == DISPLAY_DEF:
            return f"Defense {self.stat_points(pokemon, 'def')}"
        if display_mode == DISPLAY_SPA:
            return f"Sp. Atk {self.stat_points(pokemon, 'spa')}"
        if display_mode == DISPLAY_SPD:
            return f"Sp. Def {self.stat_points(pokemon, 'spd')}"
        if display_mode == DISPLAY_SPE:
            return f"Speed {self.stat_points(pokemon, 'spe')}"
        if display_mode == DISPLAY_TOTAL:
            return f"Total {self.total_stat_points(pokemon)}"
        return ""

    def sort_value(self, pokemon: OwnedPokemon, sort_mode: str) -> tuple[Any, ...]:
        resolved_species = effective_species(pokemon)
        lookup_species = lookup_species_name(resolved_species)
        display_species = str(resolved_species).lower()
        if sort_mode == SORT_NAME:
            return (display_species, pokemon.id)
        if sort_mode == SORT_POKEDEX:
            return (self.pokedex_number(lookup_species) or 999999, display_species, pokemon.id)
        if sort_mode == SORT_LEVEL:
            return (pokemon.level, display_species, pokemon.id)
        if sort_mode == SORT_CATEGORY:
            return (self.category_label(pokemon).lower(), display_species, pokemon.id)
        if sort_mode == SORT_IVS:
            return (pokemon.total_iv, display_species, pokemon.id)
        if sort_mode == SORT_EVS:
            return (pokemon_total_ev(pokemon), display_species, pokemon.id)
        if sort_mode == SORT_HP:
            return (self.stat_points(pokemon, "hp"), display_species, pokemon.id)
        if sort_mode == SORT_ATK:
            return (self.stat_points(pokemon, "atk"), display_species, pokemon.id)
        if sort_mode == SORT_DEF:
            return (self.stat_points(pokemon, "def"), display_species, pokemon.id)
        if sort_mode == SORT_SPA:
            return (self.stat_points(pokemon, "spa"), display_species, pokemon.id)
        if sort_mode == SORT_SPD:
            return (self.stat_points(pokemon, "spd"), display_species, pokemon.id)
        if sort_mode == SORT_SPE:
            return (self.stat_points(pokemon, "spe"), display_species, pokemon.id)
        if sort_mode == SORT_TOTAL:
            return (self.total_stat_points(pokemon), display_species, pokemon.id)
        if sort_mode in {SORT_NONE, SORT_CAUGHT}:
            return (pokemon.id,)
        return (pokemon.id,)

    def calculate_stats(self, pokemon: OwnedPokemon) -> dict[str, int]:
        base = self.base_stats.get(species_key(lookup_species_name(effective_species(pokemon))), {})
        plus, minus = self.nature_marks(pokemon.nature)
        values: dict[str, int] = {}
        for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
            base_stat = int(base.get(BASE_STAT_KEYS[stat], 0))
            iv = int(getattr(pokemon, f"iv_{stat}"))
            ev = int(getattr(pokemon, f"ev_{stat}"))
            if stat == "hp":
                if base_stat == 1:
                    values[stat] = 1
                else:
                    values[stat] = math.floor(((2 * base_stat + iv + math.floor(ev / 4)) * pokemon.level) / 100) + pokemon.level + 10
                continue
            raw = math.floor(((2 * base_stat + iv + math.floor(ev / 4)) * pokemon.level) / 100) + 5
            multiplier = 1.1 if plus == stat else 0.9 if minus == stat else 1.0
            values[stat] = math.floor(raw * multiplier)
        return values

    def item_page_text(self, pokemon: OwnedPokemon) -> str:
        state = load_form_state(pokemon)
        lines = [
            f"🎒 **Item & Status: {effective_species(pokemon)}**",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"**Held Item:** `{pokemon.item or 'None'}`",
            f"**Status:** `{pokemon.status or 'Healthy'}`",
            f"**Tera Type:** `{pokemon.tera_type or 'None'}`",
            "",
            f"**Untradeable:** `{'Yes' if pokemon.untradeable else 'No'}`",
            f"**Unreleasable:** `{'Yes' if pokemon.unreleasable else 'No'}`",
        ]
        
        if state:
            lines.extend([
                "",
                "🧬 **Active Transformation**",
                f"**Current Form:** `{effective_species(pokemon)}`",
                f"**Form Item:** `{state.get('item', 'Unknown')}`",
            ])
            
        return "\n".join(lines)

    def evolution_choices(self, pokemon: OwnedPokemon, *, region_id: str | None = None) -> list[dict[str, Any]]:
        if has_form_state(pokemon):
            return []
        current_key = species_key(pokemon.species)
        if current_key.endswith("-gmax"):
            return []
        options = self.evolution_map.get(current_key, [])
        choices: list[dict[str, Any]] = []

        for option in options:
            base_target = self.species_name(str(option.get("evolved_pokemon") or ""))
            for evolved_species in self._resolved_evolution_species(current_key, base_target, region_id):
                evolved_key = species_key(evolved_species)
                if not evolved_key:
                    continue

                required_level = self._evolution_required_level(current_key, option)
                gender_rule = self._evolution_gender_rule(current_key, evolved_key)
                gender_ok = gender_rule is None or pokemon.gender == gender_rule
                level_ok = int(pokemon.level) >= required_level
                ready = gender_ok and level_ok

                blockers: list[str] = []
                if not level_ok:
                    blockers.append(f"level {required_level}")
                if gender_rule == "M" and not gender_ok:
                    blockers.append("male only")
                elif gender_rule == "F" and not gender_ok:
                    blockers.append("female only")

                if ready:
                    status_text = "Ready now"
                elif blockers:
                    status_text = "Needs " + ", ".join(blockers)
                else:
                    status_text = "Not ready"

                choices.append(
                    {
                        "species": evolved_species,
                        "target_key": evolved_key,
                        "required_level": required_level,
                        "gender_rule": gender_rule,
                        "ready": ready,
                        "status_text": status_text,
                    }
                )

        return choices

    def evolution_page_text(self, pokemon: OwnedPokemon, *, region_id: str | None = None) -> str:
        if has_form_state(pokemon):
            return f"🧬 **Evolution**\n━━━━━━━━━━━━━━━━━━━━━━\n_{effective_species(pokemon)} cannot evolve while a transformation is active._"
            
        choices = self.evolution_choices(pokemon, region_id=region_id)
        if not choices:
            if species_key(pokemon.species).endswith("-gmax"):
                return f"🧬 **Evolution**\n━━━━━━━━━━━━━━━━━━━━━━\n_{pokemon.species} cannot evolve while it is in its Gmax form._"
            return f"🧬 **Evolution**\n━━━━━━━━━━━━━━━━━━━━━━\n_{pokemon.species} has no recorded evolution path._"

        lines = [
            f"🧬 **Evolution: {pokemon.species}**",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"**Current Level:** `{pokemon.level}`",
            "",
            "**Available Paths:**"
        ]
        
        for choice in choices:
            status = choice['status_text']
            marker = "✅" if choice.get('ready') else "❌"
            lines.append(f"• **{choice['species']}**\n  └ {marker} __{status}__")

        lines.append("")
        
        ready_count = sum(1 for choice in choices if bool(choice.get("ready")))
        if ready_count == 1:
            lines.append("__Press the button below to evolve now.__")
        elif ready_count > 1:
            lines.append("__Choose one of the buttons below to evolve.__")
        else:
            lines.append("__Keep leveling and check back here.__")

        return "\n".join(lines)

    def iv_evs_text(self, pokemon: OwnedPokemon) -> str:
        rows = [
            ("HP ",  pokemon.iv_hp,  pokemon.ev_hp),
            ("Atk", pokemon.iv_atk, pokemon.ev_atk),
            ("Def", pokemon.iv_def, pokemon.ev_def),
            ("SpA", pokemon.iv_spa, pokemon.ev_spa),
            ("SpD", pokemon.iv_spd, pokemon.ev_spd),
            ("Spe", pokemon.iv_spe, pokemon.ev_spe),
        ]
        total_ev = sum(row[2] for row in rows)
        lines = [
            f"**IVs & EVs \u2014 {effective_species(pokemon)}**",
            "`Stat    IV   EV`",
            "━" * 18,
        ]
        for label, iv_value, ev_value in rows:
            lines.append(f"`{label}    {iv_value:>3}  {ev_value:>3}`")
        lines.extend([
            "━" * 18,
            f"`{'Tot'}   {pokemon.total_iv:>3}  {total_ev:>3}/510`",
        ])
        return "\n".join(lines)

    def moves_page_text(self, pokemon: OwnedPokemon) -> str:
        move_names = effective_moves(pokemon)
        lines = [
            f"**Moves: {effective_species(pokemon)}**",
            "━━━━━━━━━━━━━━━━━━"
        ]
        
        if not move_names:
            return "\n".join(lines + ["_No moves recorded._"])
            
        for index, move_name in enumerate(move_names, start=1):
            info = self.move_info.get(move_key(str(move_name)), {})
            move_type = str(info.get("type", "?")).title()
            power = info.get("power", "--")
            accuracy = info.get("accuracy", "--")
            category = str(info.get("category", "Unknown")).title()
            
            # Formatted Move Header: 1. Thunderbolt [Electric]
            lines.append(f"{index}. **{self._display_move_name(str(move_name))}** `[{move_type}]`")
            
            # Formatted Stats: Indented with a corner branch for that RPG list feel
            lines.append(f"└ `Pwr: {power} | Acc: {accuracy} | Cat: {category}`")
            
            # Add a small gap between moves for readability, but not after the last move
            if index != len(move_names):
                lines.append("")
                
        return "\n".join(lines).strip()

    def collection_entry_text(self, pokemon: OwnedPokemon, display_mode: str) -> str:
        egg_icon = " \U0001F95A" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
        shiny_icon = " \u2728" if pokemon.shiny else ""
        name = f"{effective_species(pokemon)}{egg_icon}{shiny_icon}"
        suffix = self.collection_entry_suffix(pokemon, display_mode)
        if not suffix:
            return name
        if display_mode == DISPLAY_LEVEL:
            return f"{name} {suffix}"
        return f"{name} - {suffix}"

    def summary_text(self, pokemon: OwnedPokemon) -> str:
        current_exp, needed_exp = self.exp_progress(pokemon)
        next_exp = 0 if pokemon.level >= 100 else max(needed_exp - current_exp, 0)
        progress_total = 1 if pokemon.level >= 100 else needed_exp
        progress_current = progress_total if pokemon.level >= 100 else current_exp
        
        display_species = effective_species(pokemon)
        lookup_species = lookup_species_name(display_species)
        
        title_name = pokemon.nickname or display_species
        if pokemon.nickname and pokemon.nickname != display_species:
            title_name = f"{pokemon.nickname} ({display_species})"
            
        shiny_suffix = " [Shiny]" if pokemon.shiny else ""
        gender_suffix = f" [{pokemon.gender}]" if pokemon.gender in {"M", "F"} else ""
        
        title = f"**{title_name}**{shiny_suffix}{gender_suffix}".strip()
        
        lines = [
            title,
            "━━━━━━━━━━━━━━━━",
            f"Level: `{pokemon.level}`",
            f"Type: `{self.plain_types(lookup_species)}`",
            f"Nature: `{pokemon.nature}`",
            f"Ability: `{effective_ability(pokemon)}`",
            f"Held Item: `{pokemon.item or 'None'}`",
            f"Friendship: `{pokemon.friendship} / 255`",
            "",
            f"HP: `{pokemon.current_hp} / {pokemon.max_hp}`",
            f"EXP: `{progress_current:,} / {progress_total:,}` (Next: `{next_exp:,}`)",
            f"`{progress_bar(progress_current, progress_total, width=10)}`"
        ]
        return "\n".join(lines)

    def stats_page_text(self, pokemon: OwnedPokemon) -> str:
        values = self.calculate_stats(pokemon)
        plus, minus = self.nature_marks(pokemon.nature)
        
        lines = [
            f"📊 **Combat Stats: {effective_species(pokemon)}**",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"**Nature:** `{pokemon.nature}`",
            ""
        ]
        
        total_stats = 0
        for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
            val = values[stat]
            total_stats += val
            suffix = ""
            if stat == plus:
                suffix = " 🔺 `(+10%)`"
            elif stat == minus:
                suffix = " 🔻 `(-10%)`"
            
            lines.append(f"**{STAT_LABELS[stat]}:** `{val}`{suffix}")
            
        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"**Total Stats:** `{total_stats}`"
        ])
        return "\n".join(lines)


def progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "\u25B1" * width
    ratio = max(0.0, min(1.0, current / total))
    filled = round(ratio * width)
    return ("\u25B0" * filled) + ("\u25B1" * (width - filled))


def _broken_progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, current / total))
    filled = round(ratio * width)
    return ("█" * filled) + ("░" * (width - filled))
