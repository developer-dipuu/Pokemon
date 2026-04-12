from __future__ import annotations

import json
import re
from typing import Any


KEY_ITEM_METEORITE = "Meteorite"
KEY_ITEM_DNA_SPLICERS = "DNA Splicers"
KEY_ITEM_PRISON_BOTTLE = "Prison Bottle"
KEY_ITEM_N_SOLARIZER = "N-Solarizer"
KEY_ITEM_N_LUNARIZER = "N-Lunarizer"
KEY_ITEM_REINS_OF_UNITY = "Reins of Unity"
FORM_CHANGE_ITEM_COST_VP = 100000

FORM_CHANGE_ITEM_ORDER = (
    KEY_ITEM_METEORITE,
    KEY_ITEM_DNA_SPLICERS,
    KEY_ITEM_PRISON_BOTTLE,
    KEY_ITEM_N_SOLARIZER,
    KEY_ITEM_N_LUNARIZER,
    KEY_ITEM_REINS_OF_UNITY,
)


def normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


ITEM_NAME_BY_KEY = {normalize_token(name): name for name in FORM_CHANGE_ITEM_ORDER}
ITEM_HOST_FAMILY = {
    normalize_token(KEY_ITEM_METEORITE): "deoxys",
    normalize_token(KEY_ITEM_DNA_SPLICERS): "kyurem",
    normalize_token(KEY_ITEM_PRISON_BOTTLE): "hoopa",
    normalize_token(KEY_ITEM_N_SOLARIZER): "necrozma",
    normalize_token(KEY_ITEM_N_LUNARIZER): "necrozma",
    normalize_token(KEY_ITEM_REINS_OF_UNITY): "calyrex",
}
FORM_TARGETS_BY_ITEM = {
    normalize_token(KEY_ITEM_METEORITE): ["Deoxys", "Deoxys-Attack", "Deoxys-Defense", "Deoxys-Speed"],
}
FUSION_TARGETS_BY_ITEM = {
    normalize_token(KEY_ITEM_DNA_SPLICERS): {
        "reshiram": "Kyurem-White",
        "zekrom": "Kyurem-Black",
    },
    normalize_token(KEY_ITEM_N_SOLARIZER): {
        "solgaleo": "Necrozma-Dusk-Mane",
    },
    normalize_token(KEY_ITEM_N_LUNARIZER): {
        "lunala": "Necrozma-Dawn-Wings",
    },
    normalize_token(KEY_ITEM_REINS_OF_UNITY): {
        "glastrier": "Calyrex-Ice-Rider",
        "spectrier": "Calyrex-Shadow-Rider",
    },
}
TOGGLE_TARGETS_BY_ITEM = {
    normalize_token(KEY_ITEM_PRISON_BOTTLE): ("Hoopa", "Hoopa-Unbound"),
}
DISPLAY_SPECIES_ALIASES = {
    normalize_token("Calyrex-Ice"): "Calyrex-Ice-Rider",
    normalize_token("Calyrex-Shadow"): "Calyrex-Shadow-Rider",
}
ABILITY_OVERRIDES_BY_SPECIES = {
    normalize_token("Kyurem-White"): "Turboblaze",
    normalize_token("Kyurem-Black"): "Teravolt",
    normalize_token("Calyrex-Ice"): "As One (Glastrier)",
    normalize_token("Calyrex-Shadow"): "As One (Spectrier)",
    normalize_token("Calyrex-Ice-Rider"): "As One (Glastrier)",
    normalize_token("Calyrex-Shadow-Rider"): "As One (Spectrier)",
}
SIGNATURE_MOVES_BY_SPECIES = {
    normalize_token("Kyurem-White"): ["Fusion Flare", "Ice Burn"],
    normalize_token("Kyurem-Black"): ["Fusion Bolt", "Freeze Shock"],
    normalize_token("Calyrex-Ice-Rider"): ["Glacial Lance"],
    normalize_token("Calyrex-Shadow-Rider"): ["Astral Barrage"],
}
LOOKUP_SPECIES_ALIASES = {
    normalize_token("Deoxys"): "Deoxys-normal",
    normalize_token("Calyrex-Ice-Rider"): "Calyrex-ice",
    normalize_token("Calyrex-Shadow-Rider"): "Calyrex-shadow",
    normalize_token("Calyrex-Ice"): "Calyrex-ice",
    normalize_token("Calyrex-Shadow"): "Calyrex-shadow",
    normalize_token("Necrozma-Dusk-Mane"): "Necrozma-dusk",
    normalize_token("Necrozma-Dawn-Wings"): "Necrozma-dawn",
}
PACKED_STATS = ("hp", "atk", "def", "spa", "spd", "spe")


def item_key(item_name: str) -> str:
    return normalize_token(item_name)


def item_name_from_key(raw_key: str) -> str | None:
    return ITEM_NAME_BY_KEY.get(normalize_token(raw_key))


def item_requires_partner(raw_key: str) -> bool:
    return normalize_token(raw_key) in FUSION_TARGETS_BY_ITEM


def item_form_targets(raw_key: str) -> list[str]:
    return list(FORM_TARGETS_BY_ITEM.get(normalize_token(raw_key), []))


def lookup_species_name(species: str) -> str:
    return LOOKUP_SPECIES_ALIASES.get(normalize_token(species), species)


def signature_moves_for_species(species: str) -> list[str]:
    return list(SIGNATURE_MOVES_BY_SPECIES.get(normalize_token(species), []))


def species_family(species: str) -> str:
    key = normalize_token(species)
    for family in (
        "deoxys",
        "kyurem",
        "hoopa",
        "necrozma",
        "calyrex",
        "reshiram",
        "zekrom",
        "solgaleo",
        "lunala",
        "glastrier",
        "spectrier",
    ):
        if key.startswith(family):
            return family
    return key


def load_form_state(pokemon) -> dict[str, Any]:
    raw = getattr(pokemon, "form_state_json", None)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def dump_form_state(state: dict[str, Any] | None) -> str | None:
    if not isinstance(state, dict):
        return None
    cleaned = {key: value for key, value in state.items() if value not in (None, [], {}, "")}
    return json.dumps(cleaned, sort_keys=True) if cleaned else None


def set_form_state(pokemon, state: dict[str, Any] | None) -> None:
    pokemon.form_state_json = dump_form_state(state)


def has_form_state(pokemon) -> bool:
    return bool(load_form_state(pokemon))


def active_item_key(pokemon) -> str | None:
    key = normalize_token(str(load_form_state(pokemon).get("item") or ""))
    return key or None


def is_active_fusion(pokemon) -> bool:
    return str(load_form_state(pokemon).get("kind") or "") == "fusion"


def effective_species(pokemon) -> str:
    display_species = str(load_form_state(pokemon).get("display_species") or "").strip()
    if display_species:
        return DISPLAY_SPECIES_ALIASES.get(normalize_token(display_species), display_species)
    return str(pokemon.species)


def effective_ability(pokemon) -> str:
    return ABILITY_OVERRIDES_BY_SPECIES.get(normalize_token(effective_species(pokemon)), str(pokemon.ability or ""))


def _signature_bonus_moves(state: dict[str, Any]) -> list[str]:
    moves: list[str] = []
    for move in list(state.get("signature_bonus_moves") or []):
        move_name = str(move).strip()
        if move_name and normalize_token(move_name) not in {normalize_token(existing) for existing in moves}:
            moves.append(move_name)
    return moves


def _signature_move_slots(state: dict[str, Any]) -> dict[str, int]:
    slots: dict[str, int] = {}
    raw = state.get("signature_move_slots") or {}
    if not isinstance(raw, dict):
        return slots
    for move_name, slot in raw.items():
        normalized = normalize_token(str(move_name))
        if not normalized:
            continue
        try:
            slot_index = int(slot)
        except (TypeError, ValueError):
            continue
        if slot_index > 0:
            slots[normalized] = slot_index
    return slots


def ensure_signature_prompt_state(pokemon) -> dict[str, Any]:
    state = load_form_state(pokemon)
    if not state or str(state.get("kind") or "") != "fusion":
        return state
    state["signature_mode"] = "prompted"
    bonus_moves = _signature_bonus_moves(state)
    move_slots = _signature_move_slots(state)
    if bonus_moves:
        state["signature_bonus_moves"] = bonus_moves
    else:
        state.pop("signature_bonus_moves", None)
    if move_slots:
        state["signature_move_slots"] = move_slots
    else:
        state.pop("signature_move_slots", None)
    set_form_state(pokemon, state)
    return state


def add_signature_bonus_move(pokemon, move_name: str) -> None:
    state = ensure_signature_prompt_state(pokemon)
    if not state:
        return
    move_name = str(move_name).strip()
    if not move_name:
        return
    bonus_moves = _signature_bonus_moves(state)
    if normalize_token(move_name) in {normalize_token(existing) for existing in bonus_moves}:
        return
    bonus_moves.append(move_name)
    state["signature_bonus_moves"] = bonus_moves
    set_form_state(pokemon, state)


def set_signature_move_slot(pokemon, move_name: str, slot: int) -> None:
    state = ensure_signature_prompt_state(pokemon)
    if not state:
        return
    move_name = str(move_name).strip()
    if not move_name:
        return
    move_slots = _signature_move_slots(state)
    move_slots[normalize_token(move_name)] = int(slot)
    state["signature_move_slots"] = move_slots
    bonus_moves = [
        existing
        for existing in _signature_bonus_moves(state)
        if normalize_token(existing) != normalize_token(move_name)
    ]
    if bonus_moves:
        state["signature_bonus_moves"] = bonus_moves
    else:
        state.pop("signature_bonus_moves", None)
    set_form_state(pokemon, state)


def _base_moves(pokemon) -> list[str]:
    try:
        payload = json.loads(getattr(pokemon, "moves_json", "[]") or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(move).strip() for move in payload if str(move).strip()]


def effective_moves(pokemon) -> list[str]:
    moves = _base_moves(pokemon)
    required = signature_moves_for_species(effective_species(pokemon))
    if not required:
        return moves
    state = load_form_state(pokemon)
    bonus_moves = _signature_bonus_moves(state)
    move_slots = _signature_move_slots(state)
    prompt_mode = str(state.get("signature_mode") or "") == "prompted"

    if not prompt_mode and not bonus_moves and not move_slots:
        # Backward-compatible fallback for older fusion states created before
        # the prompt-driven signature move flow existed.
        required_keys = {normalize_token(move) for move in required}
        preserved = [move for move in moves if normalize_token(move) not in required_keys]
        keep_count = max(0, 4 - len(required))
        return preserved[:keep_count] + required[:4]

    result = list(moves[:4])
    for move_name in bonus_moves:
        if normalize_token(move_name) in {normalize_token(existing) for existing in result}:
            continue
        if len(result) < 4:
            result.append(move_name)

    for move_name in required:
        if normalize_token(move_name) in {normalize_token(existing) for existing in result}:
            continue
        slot = move_slots.get(normalize_token(move_name))
        if slot is None:
            continue
        if 1 <= slot <= len(result):
            result[slot - 1] = move_name
        elif len(result) < 4 and slot == len(result) + 1:
            result.append(move_name)

    return result[:4]


def _pack_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or ""))


def _unpack_existing_misc(packed_set: str) -> tuple[str, str, bool, int | None]:
    parts = str(packed_set or "").split("|")
    if len(parts) <= 10:
        return "", "", False, None

    level_field = str(parts[10] or "")
    misc_field = str(parts[11] or "") if len(parts) > 11 else ""
    if "," in level_field and not misc_field:
        split_level = level_field.split(",", 1)
        level_field = split_level[0]
        misc_field = split_level[1] if len(split_level) > 1 else ""

    misc = misc_field.split(",", 5) if misc_field else []
    happiness = str(misc[0] or "") if len(misc) > 0 else ""
    hp_type = str(misc[1] or "") if len(misc) > 1 else ""
    pokeball = str(misc[2] or "") if len(misc) > 2 else ""
    gigantamax = bool(str(misc[3] or "").strip()) if len(misc) > 3 else False
    dynamax_level_raw = str(misc[4] or "").strip() if len(misc) > 4 else ""
    try:
        dynamax_level = int(dynamax_level_raw) if dynamax_level_raw else None
    except ValueError:
        dynamax_level = None
    return hp_type, pokeball, gigantamax, dynamax_level


def build_effective_packed_set(pokemon) -> str:
    packed_set = str(getattr(pokemon, "packed_set", "") or "")

    display_species = effective_species(pokemon)
    base_species = str(getattr(pokemon, "species", "") or "")
    if not display_species:
        return packed_set
    nickname = str(getattr(pokemon, "nickname", None) or "").strip()
    transformed = normalize_token(display_species) != normalize_token(base_species)

    # Let the species slot speak for transformed Pokemon so Showdown does not
    # keep showing the host/base name during battle previews.
    if nickname:
        name_field = nickname
    elif transformed:
        name_field = ""
    else:
        name_field = base_species if len(base_species) <= 18 else ""

    moves = effective_moves(pokemon)
    hp_type, pokeball, gigantamax, dynamax_level = _unpack_existing_misc(packed_set)
    evs = {stat: int(getattr(pokemon, f"ev_{stat}", 0) or 0) for stat in PACKED_STATS}
    if sum(evs.values()) <= 0:
        fallback_seed = int(getattr(pokemon, "id", 0) or 0)
        if fallback_seed <= 0:
            fallback_seed = sum(ord(char) for char in f"{base_species}|{nickname}")
        evs[PACKED_STATS[fallback_seed % len(PACKED_STATS)]] = 1
    ivs = {
        stat: int(getattr(pokemon, f"iv_{stat}", 31) or 0)
        for stat in PACKED_STATS
    }

    level = max(1, int(getattr(pokemon, "level", 1) or 1))
    friendship = max(0, min(255, int(getattr(pokemon, "friendship", 255) or 255)))
    misc_parts = [
        str(friendship) if friendship != 255 else "",
        hp_type,
        _pack_name(pokeball),
        "G" if gigantamax else "",
        str(dynamax_level) if dynamax_level not in (None, 10) else "",
        str(getattr(pokemon, "tera_type", "") or ""),
    ]

    parts = [
        name_field,
        "" if _pack_name(name_field) == _pack_name(display_species) else _pack_name(display_species),
        _pack_name(str(getattr(pokemon, "item", "") or "")),
        _pack_name(effective_ability(pokemon)),
        ",".join(_pack_name(move) for move in moves),
        str(getattr(pokemon, "nature", "") or "").strip(),
        ",".join(str(evs[stat]) for stat in PACKED_STATS),
        str(getattr(pokemon, "gender", "") or ""),
        ",".join("" if ivs[stat] == 31 else str(ivs[stat]) for stat in PACKED_STATS),
        "S" if bool(getattr(pokemon, "shiny", False)) else "",
        "" if level == 100 else str(level),
        ",".join(misc_parts),
    ]
    return "|".join(parts)


def build_effective_export_text(pokemon) -> str:
    display_species = effective_species(pokemon)
    held_item = str(getattr(pokemon, "item", "") or "")
    header = display_species if not held_item else f"{display_species} @ {held_item}"
    lines = [
        header,
        f"Level: {int(getattr(pokemon, 'level', 1) or 1)}",
        f"Ability: {effective_ability(pokemon)}",
        f"Nature: {str(getattr(pokemon, 'nature', '') or '').strip()}",
    ]
    lines.extend(f"- {move}" for move in effective_moves(pokemon))
    return "\n".join(lines)


def compatible_host(item_name_or_key: str, pokemon) -> bool:
    key = normalize_token(item_name_or_key)
    item_family = ITEM_HOST_FAMILY.get(key)
    if not item_family:
        return False
    if active_item_key(pokemon) not in {None, key}:
        return False
    return item_family in {
        species_family(str(getattr(pokemon, "species", "") or "")),
        species_family(effective_species(pokemon)),
    }


def compatible_partner(item_name_or_key: str, host, candidate) -> bool:
    key = normalize_token(item_name_or_key)
    if getattr(host, "id", None) == getattr(candidate, "id", None):
        return False
    if has_form_state(candidate):
        return False
    if species_family(str(getattr(host, "species", "") or "")) != ITEM_HOST_FAMILY.get(key):
        return False
    return species_family(str(getattr(candidate, "species", "") or "")) in FUSION_TARGETS_BY_ITEM.get(key, {})


def fusion_result_species(item_name_or_key: str, partner_species: str) -> str | None:
    return FUSION_TARGETS_BY_ITEM.get(normalize_token(item_name_or_key), {}).get(species_family(partner_species))


def toggle_result_species(item_name_or_key: str, current_species: str) -> str | None:
    targets = TOGGLE_TARGETS_BY_ITEM.get(normalize_token(item_name_or_key))
    if not targets:
        return None
    current_key = normalize_token(current_species)
    return targets[0] if current_key == normalize_token(targets[1]) else targets[1]


def snapshot_owned_pokemon(pokemon) -> dict[str, Any]:
    return {
        "species": str(pokemon.species),
        "nickname": str(pokemon.nickname) if pokemon.nickname is not None else None,
        "origin_region": str(pokemon.origin_region),
        "source_kind": str(pokemon.source_kind),
        "level": int(pokemon.level),
        "experience": int(pokemon.experience),
        "friendship": int(pokemon.friendship),
        "ability": str(pokemon.ability),
        "nature": str(pokemon.nature),
        "gender": str(pokemon.gender or ""),
        "item": str(pokemon.item or ""),
        "status": str(pokemon.status or ""),
        "tera_type": str(pokemon.tera_type or ""),
        "current_hp": int(pokemon.current_hp),
        "max_hp": int(pokemon.max_hp),
        "shiny": bool(pokemon.shiny),
        "untradeable": bool(pokemon.untradeable),
        "unreleasable": bool(pokemon.unreleasable),
        "ivs": {
            "hp": int(pokemon.iv_hp),
            "atk": int(pokemon.iv_atk),
            "def": int(pokemon.iv_def),
            "spa": int(pokemon.iv_spa),
            "spd": int(pokemon.iv_spd),
            "spe": int(pokemon.iv_spe),
        },
        "evs": {
            "hp": int(pokemon.ev_hp),
            "atk": int(pokemon.ev_atk),
            "def": int(pokemon.ev_def),
            "spa": int(pokemon.ev_spa),
            "spd": int(pokemon.ev_spd),
            "spe": int(pokemon.ev_spe),
        },
        "moves_json": str(pokemon.moves_json or "[]"),
        "move_history_json": str(getattr(pokemon, "move_history_json", "{}") or "{}"),
        "export_text": str(pokemon.export_text or ""),
        "packed_set": str(pokemon.packed_set or ""),
        "form_state_json": getattr(pokemon, "form_state_json", None),
        "party_slots": [int(slot.slot_index) for slot in list(getattr(pokemon, "party_slots", []))],
        "team_slots": [
            {"team_id": int(slot.team_id), "slot_index": int(slot.slot_index)}
            for slot in list(getattr(pokemon, "team_slots", []))
        ],
    }


def restore_payload_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "species": str(snapshot.get("species") or ""),
        "nickname": snapshot.get("nickname"),
        "origin_region": str(snapshot.get("origin_region") or ""),
        "source_kind": str(snapshot.get("source_kind") or "wild"),
        "level": int(snapshot.get("level") or 1),
        "experience": int(snapshot.get("experience") or 0),
        "friendship": int(snapshot.get("friendship") or 0),
        "ability": str(snapshot.get("ability") or ""),
        "nature": str(snapshot.get("nature") or "Serious"),
        "gender": str(snapshot.get("gender") or ""),
        "item": str(snapshot.get("item") or ""),
        "status": str(snapshot.get("status") or ""),
        "tera_type": str(snapshot.get("tera_type") or ""),
        "current_hp": int(snapshot.get("current_hp") or 1),
        "max_hp": int(snapshot.get("max_hp") or 1),
        "shiny": bool(snapshot.get("shiny")),
        "untradeable": bool(snapshot.get("untradeable")),
        "unreleasable": bool(snapshot.get("unreleasable")),
        "ivs": dict(snapshot.get("ivs") or {}),
        "evs": dict(snapshot.get("evs") or {}),
        "moves_json": str(snapshot.get("moves_json") or "[]"),
        "move_history_json": str(snapshot.get("move_history_json") or "{}"),
        "export_text": str(snapshot.get("export_text") or ""),
        "packed_set": str(snapshot.get("packed_set") or ""),
    }
    if snapshot.get("form_state_json") is not None:
        payload["form_state_json"] = snapshot.get("form_state_json")
    return payload
