from pathlib import Path
from typing import Any, Sequence

from bot.battle.protocol import details_name
from bot.build_safari_pool import LEGENDARY_KEYS, MYTHICAL_KEYS, PARADOX_KEYS, ULTRA_BEAST_KEYS
from bot.game.fusion import effective_species, lookup_species_name
from bot.game.services.pokemon_data import PokemonDataService, move_key, species_key


BROCK_IMAGE_PATH = Path(__file__).resolve().parents[2] / "assets" / "trainercard" / "sprites-trainers" / "brock.png"

GYM_REGIONS: tuple[tuple[str, str], ...] = (("kanto", "Kanto"),)
GYMS_BY_REGION: dict[str, tuple[tuple[str, str, str], ...]] = {
    "kanto": (("pewter", "Pewter Gym", "Brock"),),
}

MAX_IVS = {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31}
MAX_EVS = {"hp": 252, "atk": 252, "def": 252, "spa": 252, "spd": 252, "spe": 252}

BROCK_TEAM_SPECS: tuple[dict[str, Any], ...] = (
    {
        "species": "Golem-Alola",
        "ability": "Galvanize",
        "item": "Air Balloon",
        "moves": ["Supercell Slam", "Earthquake", "Stealth Rock", "Explosion"],
        "nature": "Serious",
        "ivs": MAX_IVS,
        "evs": MAX_EVS,
    },
    {
        "species": "Arcanine-Hisui",
        "ability": "Rock Head",
        "item": "Focus Sash",
        "moves": ["Head Smash", "Extreme Speed", "Flare Blitz", "Wild Charge"],
        "nature": "Serious",
        "ivs": MAX_IVS,
        "evs": MAX_EVS,
    },
    {
        "species": "Rhyperior",
        "ability": "Solid Rock",
        "item": "Weakness Policy",
        "moves": ["Supercell Slam", "Rock Polish", "Earthquake", "Rock Blast"],
        "nature": "Serious",
        "ivs": MAX_IVS,
        "evs": MAX_EVS,
    },
    {
        "species": "Aerodactyl",
        "ability": "Rock Head",
        "item": "Heavy-Duty Boots",
        "moves": ["Take Down", "Stone Edge", "Giga Impact", "Dragon Claw"],
        "nature": "Serious",
        "ivs": MAX_IVS,
        "evs": MAX_EVS,
    },
    # The requested order omitted Tyranitar, but it was listed in the team and
    # given its own first-turn strategy, so it is placed before Steelix here.
    {
        "species": "Tyranitar",
        "ability": "Sand Stream",
        "item": "Leftovers",
        "moves": ["Knock Off", "Rock Slide", "Earthquake", "Dragon Dance"],
        "nature": "Serious",
        "ivs": MAX_IVS,
        "evs": MAX_EVS,
    },
    {
        "species": "Steelix",
        # Steelix must enter battle as a legal base-form set. It mega evolves
        # immediately, which then gives it Sand Force in-battle.
        "ability": "Sturdy",
        "item": "Steelixite",
        "moves": ["Heavy Slam", "Body Press", "Earthquake", "Iron Defense"],
        "nature": "Serious",
        "ivs": MAX_IVS,
        "evs": MAX_EVS,
    },
)

RESTRICTED_SPECIES_KEYS = {
    species_key(value)
    for value in (*LEGENDARY_KEYS, *MYTHICAL_KEYS, *PARADOX_KEYS, *ULTRA_BEAST_KEYS)
}

TYPE_CHART: dict[str, dict[str, float]] = {
    "normal": {"rock": 0.5, "ghost": 0.0, "steel": 0.5},
    "fire": {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 2.0, "bug": 2.0, "rock": 0.5, "dragon": 0.5, "steel": 2.0},
    "water": {"fire": 2.0, "water": 0.5, "grass": 0.5, "ground": 2.0, "rock": 2.0, "dragon": 0.5},
    "electric": {"water": 2.0, "electric": 0.5, "grass": 0.5, "ground": 0.0, "flying": 2.0, "dragon": 0.5},
    "grass": {"fire": 0.5, "water": 2.0, "grass": 0.5, "poison": 0.5, "ground": 2.0, "flying": 0.5, "bug": 0.5, "rock": 2.0, "dragon": 0.5, "steel": 0.5},
    "ice": {"fire": 0.5, "water": 0.5, "grass": 2.0, "ground": 2.0, "flying": 2.0, "dragon": 2.0, "steel": 0.5, "ice": 0.5},
    "fighting": {"normal": 2.0, "ice": 2.0, "rock": 2.0, "dark": 2.0, "steel": 2.0, "poison": 0.5, "flying": 0.5, "psychic": 0.5, "bug": 0.5, "ghost": 0.0, "fairy": 0.5},
    "poison": {"grass": 2.0, "fairy": 2.0, "poison": 0.5, "ground": 0.5, "rock": 0.5, "ghost": 0.5, "steel": 0.0},
    "ground": {"fire": 2.0, "electric": 2.0, "grass": 0.5, "poison": 2.0, "flying": 0.0, "bug": 0.5, "rock": 2.0, "steel": 2.0},
    "flying": {"electric": 0.5, "grass": 2.0, "fighting": 2.0, "bug": 2.0, "rock": 0.5, "steel": 0.5},
    "psychic": {"fighting": 2.0, "poison": 2.0, "psychic": 0.5, "dark": 0.0, "steel": 0.5},
    "bug": {"fire": 0.5, "grass": 2.0, "fighting": 0.5, "poison": 0.5, "flying": 0.5, "psychic": 2.0, "ghost": 0.5, "dark": 2.0, "steel": 0.5, "fairy": 0.5},
    "rock": {"fire": 2.0, "ice": 2.0, "fighting": 0.5, "ground": 0.5, "flying": 2.0, "bug": 2.0, "steel": 0.5},
    "ghost": {"normal": 0.0, "psychic": 2.0, "ghost": 2.0, "dark": 0.5},
    "dragon": {"dragon": 2.0, "steel": 0.5, "fairy": 0.0},
    "dark": {"fighting": 0.5, "psychic": 2.0, "ghost": 2.0, "dark": 0.5, "fairy": 0.5},
    "steel": {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2.0, "rock": 2.0, "steel": 0.5, "fairy": 2.0},
    "fairy": {"fire": 0.5, "fighting": 2.0, "poison": 0.5, "dragon": 2.0, "dark": 2.0, "steel": 0.5},
}


def gym_requirement_text() -> str:
    return (
        "Gym entry rules:\n"
        "- Your active /myteam must have 6 Pokemon\n"
        "- All 6 must be level 100\n"
        "- No duplicate Pokemon\n"
        "- No repeated types anywhere on the team\n"
        "- No Legendary, Mythical, Paradox, or Ultra Beast Pokemon"
    )


def validate_gym_challenger_team(members: Sequence[Any], data_service: PokemonDataService) -> list[str]:
    reasons: list[str] = []
    team = [pokemon for pokemon in members if pokemon is not None]
    if len(team) != 6:
        reasons.append("Your active /myteam must have all 6 slots filled.")

    low_level = [f"{effective_species(pokemon)} Lv.{int(getattr(pokemon, 'level', 0) or 0)}" for pokemon in team if int(getattr(pokemon, "level", 0) or 0) != 100]
    if low_level:
        reasons.append("All six Pokemon must be level 100: " + ", ".join(low_level))

    duplicate_names: set[str] = set()
    seen_species: dict[str, str] = {}
    repeated_types: dict[str, set[str]] = {}
    seen_types: dict[str, str] = {}
    restricted_names: list[str] = []

    restricted_numbers = {
        number
        for number in (data_service.pokedex_number(key) for key in RESTRICTED_SPECIES_KEYS)
        if number is not None
    }

    for pokemon in team:
        display_species = effective_species(pokemon)
        lookup_species = lookup_species_name(display_species)
        normalized_species = species_key(lookup_species)
        number = data_service.pokedex_number(lookup_species)
        identity = f"num:{number}" if number is not None else f"key:{normalized_species}"
        if identity in seen_species:
            duplicate_names.add(display_species)
            duplicate_names.add(seen_species[identity])
        else:
            seen_species[identity] = display_species

        if (number is not None and number in restricted_numbers) or normalized_species in RESTRICTED_SPECIES_KEYS:
            restricted_names.append(display_species)

        for raw_type in data_service.types_for_species(lookup_species):
            type_name = str(raw_type).strip().lower()
            if not type_name:
                continue
            if type_name in seen_types:
                repeated_types.setdefault(type_name, {seen_types[type_name]}).add(display_species)
            else:
                seen_types[type_name] = display_species

    if duplicate_names:
        reasons.append("Duplicate Pokemon are not allowed: " + ", ".join(sorted(duplicate_names)))

    if repeated_types:
        repeated_bits = [
            f"{type_name.title()} ({', '.join(sorted(names))})"
            for type_name, names in sorted(repeated_types.items())
        ]
        reasons.append("Each Pokemon type can only appear once: " + ", ".join(repeated_bits))

    if restricted_names:
        reasons.append(
            "Legendary, Mythical, Paradox, and Ultra Beast Pokemon are not allowed: "
            + ", ".join(sorted(set(restricted_names)))
        )

    return reasons


def choose_brock_action(battle: Any, request: dict[str, Any], data_service: PokemonDataService) -> str | None:
    if request.get("teamPreview"):
        return "team 1"
    if request.get("forceSwitch"):
        return _first_switch_choice(request)

    active = (request.get("active") or [{}])[0]
    moves = [(index, move) for index, move in enumerate(active.get("moves") or [], start=1) if not move.get("disabled")]
    if not moves:
        return None

    state = battle.metadata.setdefault("gym_ai_state", {})
    attacker_state = battle.public_view.active.get("p2") or {}
    target_state = battle.public_view.active.get("p1") or {}
    attacker_types = [str(item).strip().lower() for item in (attacker_state.get("types") or []) if str(item).strip()]
    target_types = [str(item).strip().lower() for item in (target_state.get("types") or []) if str(item).strip()]
    hp_percent = int(attacker_state.get("percent") or 100)
    active_species = _active_species_key(request)

    if active_species.startswith("golem-alola"):
        if not state.get("brock_golem_stealth_rock_used"):
            index = _find_legal_move_index(moves, "Stealth Rock")
            if index is not None:
                state["brock_golem_stealth_rock_used"] = True
                return f"move {index}"
        if hp_percent < 50:
            index = _find_legal_move_index(moves, "Explosion")
            if index is not None and _move_multiplier_for_index(moves, index, target_types, data_service) > 0:
                return f"move {index}"

    if active_species.startswith("arcanine-hisui") and hp_percent < 20:
        index = _find_legal_move_index(moves, "Extreme Speed")
        if index is not None:
            return f"move {index}"

    if active_species.startswith("rhyperior") and not state.get("brock_rhyperior_rock_polish_used"):
        index = _find_legal_move_index(moves, "Rock Polish")
        if index is not None:
            state["brock_rhyperior_rock_polish_used"] = True
            return f"move {index}"

    if active_species.startswith("tyranitar") and not state.get("brock_tyranitar_dragon_dance_used"):
        index = _find_legal_move_index(moves, "Dragon Dance")
        if index is not None:
            state["brock_tyranitar_dragon_dance_used"] = True
            return f"move {index}"

    if active_species.startswith("steelix") and not state.get("brock_steelix_opened"):
        index = _find_legal_move_index(moves, "Iron Defense")
        if index is not None:
            state["brock_steelix_opened"] = True
            suffix = " mega" if active.get("canMegaEvo") else ""
            return f"move {index}{suffix}"

    power_boosts = {"bodypress": 2.6} if active_species.startswith("steelix") and state.get("brock_steelix_opened") else None
    index = _choose_best_damage_move(
        moves,
        attacker_types,
        target_types,
        data_service,
        power_boosts=power_boosts,
        attacker_species=active_species,
    )
    if index is not None:
        return f"move {index}"

    return f"move {moves[0][0]}"


def _restricted_key_to_species_name(data_service: PokemonDataService, raw_key: str) -> str:
    if raw_key in RESTRICTED_SPECIES_KEYS:
        return raw_key
    return data_service.species_name(raw_key)


def _active_species_key(request: dict[str, Any]) -> str:
    for pokemon in (request.get("side") or {}).get("pokemon") or []:
        if pokemon.get("active"):
            details = str(pokemon.get("details") or pokemon.get("ident") or "")
            return species_key(details_name(details))
    return ""


def _first_switch_choice(request: dict[str, Any]) -> str | None:
    for index, pokemon in enumerate((request.get("side") or {}).get("pokemon") or [], start=1):
        condition = str(pokemon.get("condition", ""))
        if not pokemon.get("active") and "fnt" not in condition.lower():
            return f"switch {index}"
    return None


def _find_legal_move_index(moves: Sequence[tuple[int, dict[str, Any]]], move_name: str) -> int | None:
    target = move_key(move_name)
    for index, move in moves:
        if move_key(str(move.get("move") or "")) == target:
            return index
    return None


def _move_multiplier_for_index(
    moves: Sequence[tuple[int, dict[str, Any]]],
    index: int,
    target_types: Sequence[str],
    data_service: PokemonDataService,
) -> float:
    for current_index, move in moves:
        if current_index != index:
            continue
        info = data_service.move_info.get(move_key(str(move.get("move") or "")), {})
        move_type = str(move.get("displayType") or info.get("type") or "").strip().lower()
        return type_multiplier(move_type, target_types)
    return 1.0


def _choose_best_damage_move(
    moves: Sequence[tuple[int, dict[str, Any]]],
    attacker_types: Sequence[str],
    target_types: Sequence[str],
    data_service: PokemonDataService,
    *,
    power_boosts: dict[str, float] | None = None,
    attacker_species: str = "",
) -> int | None:
    damaging: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    for index, move in moves:
        move_name = str(move.get("move") or "").strip()
        info = data_service.move_info.get(move_key(move_name), {})
        move_type = str(move.get("displayType") or info.get("type") or "").strip().lower()
        move_id = move_key(move_name)
        accuracy = move_accuracy(move.get("displayAccuracy", info.get("accuracy")))
        fallback.append({"index": index, "accuracy": accuracy})

        category = str(info.get("category") or "").strip().lower()
        power = move_power(info, move_id=move_id, attacker_species=attacker_species)
        if category == "status" or power <= 0:
            continue

        if power_boosts and move_id in power_boosts:
            power *= float(power_boosts[move_id])

        multiplier = type_multiplier(move_type, target_types)
        stab = move_type in {str(item).strip().lower() for item in attacker_types if str(item).strip()}
        estimate = power * max(multiplier, 0.0) * (1.5 if stab else 1.0) * (accuracy / 100.0)
        damaging.append(
            {
                "index": index,
                "stab": stab,
                "accuracy": accuracy,
                "multiplier": multiplier,
                "estimate": estimate,
                "power": power,
            }
        )

    super_effective = [entry for entry in damaging if entry["multiplier"] > 1.0]
    if super_effective:
        super_effective.sort(
            key=lambda entry: (
                entry["estimate"],
                entry["stab"],
                entry["accuracy"],
                entry["multiplier"],
                entry["power"],
            ),
            reverse=True,
        )
        return int(super_effective[0]["index"])

    hittable = [entry for entry in damaging if entry["multiplier"] > 0.0]
    if hittable:
        hittable.sort(
            key=lambda entry: (
                entry["stab"],
                entry["accuracy"],
                entry["estimate"],
                entry["multiplier"],
                entry["power"],
            ),
            reverse=True,
        )
        return int(hittable[0]["index"])

    if fallback:
        fallback.sort(key=lambda entry: (entry["accuracy"], -entry["index"]), reverse=True)
        return int(fallback[0]["index"])
    return None


def move_accuracy(value: Any) -> float:
    if isinstance(value, bool):
        return 101.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().rstrip("%")
    if text in {"", "--"}:
        return 101.0
    try:
        return float(text)
    except ValueError:
        return 100.0


def move_power(info: dict[str, Any], *, move_id: str, attacker_species: str) -> float:
    raw = info.get("power")
    try:
        power = float(raw)
    except (TypeError, ValueError):
        power = 0.0
    if move_id == "heavyslam" and attacker_species.startswith("steelix"):
        return max(power, 120.0)
    return power


def type_multiplier(move_type: str, target_types: Sequence[str]) -> float:
    normalized_move_type = str(move_type).strip().lower()
    if not normalized_move_type:
        return 1.0
    multiplier = 1.0
    for target_type in target_types:
        normalized_target = str(target_type).strip().lower()
        if not normalized_target:
            continue
        multiplier *= TYPE_CHART.get(normalized_move_type, {}).get(normalized_target, 1.0)
    return multiplier
