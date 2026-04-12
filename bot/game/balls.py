from __future__ import annotations

import json
import re
from typing import Any


BALL_POKE = "poke"
BALL_GREAT = "great"
BALL_ULTRA = "ultra"
BALL_MASTER = "master"
BALL_PREMIER = "premier"
BALL_HEAL = "heal"
BALL_LUXURY = "luxury"
BALL_QUICK = "quick"
BALL_DUSK = "dusk"
BALL_TIMER = "timer"
BALL_REPEAT = "repeat"
BALL_NEST = "nest"
BALL_NET = "net"
BALL_DIVE = "dive"
BALL_FAST = "fast"
BALL_LEVEL = "level"
BALL_LURE = "lure"
BALL_HEAVY = "heavy"
BALL_LOVE = "love"
BALL_FRIEND = "friend"
BALL_MOON = "moon"
BALL_DREAM = "dream"
BALL_BEAST = "beast"
BALL_CHERISH = "cherish"
BALL_SPORT = "sport"
BALL_PARK = "park"

BALL_ORDER = [
    BALL_POKE,
    BALL_GREAT,
    BALL_ULTRA,
    BALL_MASTER,
    BALL_PREMIER,
    BALL_HEAL,
    BALL_LUXURY,
    BALL_QUICK,
    BALL_DUSK,
    BALL_TIMER,
    BALL_REPEAT,
    BALL_NEST,
    BALL_NET,
    BALL_DIVE,
    BALL_FAST,
    BALL_LEVEL,
    BALL_LURE,
    BALL_HEAVY,
    BALL_LOVE,
    BALL_FRIEND,
    BALL_MOON,
    BALL_DREAM,
    BALL_BEAST,
    BALL_CHERISH,
    BALL_SPORT,
    BALL_PARK,
]

BALL_DEFINITIONS: dict[str, dict[str, Any]] = {
    BALL_POKE: {
        "label": "Poke Ball",
        "short_label": "Poke",
        "field": "poke_balls",
        "aliases": ("poke", "pokeball", "pokeballs", "pokeballs"),
    },
    BALL_GREAT: {
        "label": "Great Ball",
        "short_label": "Great",
        "field": "great_balls",
        "aliases": ("great", "greatball", "greatballs"),
    },
    BALL_ULTRA: {
        "label": "Ultra Ball",
        "short_label": "Ultra",
        "field": "ultra_balls",
        "aliases": ("ultra", "ultraball", "ultraballs"),
    },
    BALL_MASTER: {
        "label": "Master Ball",
        "short_label": "Master",
        "aliases": ("master", "masterball", "masterballs"),
    },
    BALL_PREMIER: {
        "label": "Premier Ball",
        "short_label": "Premier",
        "aliases": ("premier", "premierball", "premierballs"),
    },
    BALL_HEAL: {
        "label": "Heal Ball",
        "short_label": "Heal",
        "aliases": ("heal", "healball", "healballs"),
    },
    BALL_LUXURY: {
        "label": "Luxury Ball",
        "short_label": "Luxury",
        "aliases": ("luxury", "luxuryball", "luxuryballs"),
    },
    BALL_QUICK: {
        "label": "Quick Ball",
        "short_label": "Quick",
        "aliases": ("quick", "quickball", "quickballs"),
    },
    BALL_DUSK: {
        "label": "Dusk Ball",
        "short_label": "Dusk",
        "aliases": ("dusk", "duskball", "duskballs"),
    },
    BALL_TIMER: {
        "label": "Timer Ball",
        "short_label": "Timer",
        "aliases": ("timer", "timerball", "timerballs"),
    },
    BALL_REPEAT: {
        "label": "Repeat Ball",
        "short_label": "Repeat",
        "aliases": ("repeat", "repeatball", "repeatballs"),
    },
    BALL_NEST: {
        "label": "Nest Ball",
        "short_label": "Nest",
        "aliases": ("nest", "nestball", "nestballs"),
    },
    BALL_NET: {
        "label": "Net Ball",
        "short_label": "Net",
        "aliases": ("net", "netball", "netballs"),
    },
    BALL_DIVE: {
        "label": "Dive Ball",
        "short_label": "Dive",
        "aliases": ("dive", "diveball", "diveballs"),
    },
    BALL_FAST: {
        "label": "Fast Ball",
        "short_label": "Fast",
        "aliases": ("fast", "fastball", "fastballs"),
    },
    BALL_LEVEL: {
        "label": "Level Ball",
        "short_label": "Level",
        "aliases": ("level", "levelball", "levelballs"),
    },
    BALL_LURE: {
        "label": "Lure Ball",
        "short_label": "Lure",
        "aliases": ("lure", "lureball", "lureballs"),
    },
    BALL_HEAVY: {
        "label": "Heavy Ball",
        "short_label": "Heavy",
        "aliases": ("heavy", "heavyball", "heavyballs"),
    },
    BALL_LOVE: {
        "label": "Love Ball",
        "short_label": "Love",
        "aliases": ("love", "loveball", "loveballs"),
    },
    BALL_FRIEND: {
        "label": "Friend Ball",
        "short_label": "Friend",
        "aliases": ("friend", "friendball", "friendballs"),
    },
    BALL_MOON: {
        "label": "Moon Ball",
        "short_label": "Moon",
        "aliases": ("moon", "moonball", "moonballs"),
    },
    BALL_DREAM: {
        "label": "Dream Ball",
        "short_label": "Dream",
        "aliases": ("dream", "dreamball", "dreamballs"),
    },
    BALL_BEAST: {
        "label": "Beast Ball",
        "short_label": "Beast",
        "aliases": ("beast", "beastball", "beastballs"),
    },
    BALL_CHERISH: {
        "label": "Cherish Ball",
        "short_label": "Cherish",
        "aliases": ("cherish", "cherishball", "cherishballs"),
    },
    BALL_SPORT: {
        "label": "Sport Ball",
        "short_label": "Sport",
        "aliases": ("sport", "sportball", "sportballs"),
    },
    BALL_PARK: {
        "label": "Park Ball",
        "short_label": "Park",
        "aliases": ("park", "parkball", "parkballs"),
    },
}

BALL_FIELDS = {
    ball_kind: str(spec["field"])
    for ball_kind, spec in BALL_DEFINITIONS.items()
    if spec.get("field")
}

BALL_ALIAS_LOOKUP: dict[str, str] = {}
for ball_kind, spec in BALL_DEFINITIONS.items():
    aliases = set(spec.get("aliases") or ())
    aliases.add(ball_kind)
    aliases.add(str(spec["label"]).lower())
    aliases.add(str(spec["short_label"]).lower())
    for alias in aliases:
        cleaned = re.sub(r"[^a-z0-9]+", "", str(alias).lower())
        if cleaned:
            BALL_ALIAS_LOOKUP[cleaned] = ball_kind


def ball_label(ball_kind: str) -> str:
    spec = BALL_DEFINITIONS.get(ball_kind)
    return str(spec["label"]) if spec else ball_kind.replace("-", " ").title()


def ball_short_label(ball_kind: str) -> str:
    spec = BALL_DEFINITIONS.get(ball_kind)
    return str(spec["short_label"]) if spec else ball_label(ball_kind)


def normalize_ball_kind(value: str) -> str | None:
    cleaned = re.sub(r"[^a-z0-9]+", "", value.lower())
    if not cleaned:
        return None
    return BALL_ALIAS_LOOKUP.get(cleaned)


def parse_extra_ball_counts(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    counts: dict[str, int] = {}
    for ball_kind in BALL_ORDER:
        if ball_kind in BALL_FIELDS:
            continue
        value = payload.get(ball_kind)
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            counts[ball_kind] = amount
    return counts


def serialize_extra_ball_counts(counts: dict[str, int]) -> str:
    payload = {
        ball_kind: max(int(counts.get(ball_kind, 0)), 0)
        for ball_kind in BALL_ORDER
        if ball_kind not in BALL_FIELDS and int(counts.get(ball_kind, 0)) > 0
    }
    return json.dumps(payload, sort_keys=True)
