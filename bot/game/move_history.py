from __future__ import annotations

import json
from typing import Iterable

from bot.db.repositories import normalize_lookup


MOVE_HISTORY_CATEGORIES: tuple[str, ...] = ("tm", "egg", "tutor")


def _empty_history() -> dict[str, list[str]]:
    return {category: [] for category in MOVE_HISTORY_CATEGORIES}


def load_move_history(pokemon) -> dict[str, list[str]]:
    raw = getattr(pokemon, "move_history_json", "{}") or "{}"
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    history = _empty_history()
    for category in MOVE_HISTORY_CATEGORIES:
        seen: set[str] = set()
        for move_name in list(payload.get(category) or []):
            move_text = str(move_name).strip()
            move_key_value = normalize_lookup(move_text)
            if not move_key_value or move_key_value in seen:
                continue
            seen.add(move_key_value)
            history[category].append(move_text)
    return history


def dump_move_history(history: dict[str, Iterable[str]] | None) -> str:
    payload = _empty_history()
    if isinstance(history, dict):
        for category in MOVE_HISTORY_CATEGORIES:
            seen: set[str] = set()
            for move_name in list(history.get(category) or []):
                move_text = str(move_name).strip()
                move_key_value = normalize_lookup(move_text)
                if not move_key_value or move_key_value in seen:
                    continue
                seen.add(move_key_value)
                payload[category].append(move_text)
    return json.dumps(payload, sort_keys=True)


def record_move_history(
    pokemon,
    *,
    categories: Iterable[str],
    move_names: Iterable[str],
) -> bool:
    history = load_move_history(pokemon)
    changed = False
    normalized_categories = {
        str(category).strip().lower()
        for category in list(categories or [])
        if str(category).strip()
    }
    valid_categories = [category for category in MOVE_HISTORY_CATEGORIES if category in normalized_categories]
    if not valid_categories:
        return False

    prepared_moves: list[str] = []
    prepared_move_keys: list[str] = []
    for move_name in list(move_names or []):
        move_text = str(move_name).strip()
        move_key_value = normalize_lookup(move_text)
        if not move_key_value:
            continue
        prepared_moves.append(move_text)
        prepared_move_keys.append(move_key_value)

    if not prepared_moves:
        return False

    for category in valid_categories:
        existing_keys = {normalize_lookup(move_name) for move_name in history[category]}
        for move_text, move_key_value in zip(prepared_moves, prepared_move_keys):
            if move_key_value in existing_keys:
                continue
            history[category].append(move_text)
            existing_keys.add(move_key_value)
            changed = True

    if changed:
        pokemon.move_history_json = dump_move_history(history)
    return changed
