from __future__ import annotations

import re


EXP_CANDY_DROP_ODDS = 300

MEDICINE_DEFINITIONS: dict[str, dict[str, int | str | None]] = {
    "rare-candy": {
        "name": "Rare Candy",
        "kind": "rare-candy",
        "shop_price": 100,
        "exp": None,
        "ev_stat": None,
        "ev_amount": None,
    },
    "health-mochi": {
        "name": "Health Mochi",
        "kind": "mochi",
        "shop_price": 100,
        "exp": None,
        "ev_stat": "hp",
        "ev_amount": 10,
    },
    "muscle-mochi": {
        "name": "Muscle Mochi",
        "kind": "mochi",
        "shop_price": 100,
        "exp": None,
        "ev_stat": "atk",
        "ev_amount": 10,
    },
    "resist-mochi": {
        "name": "Resist Mochi",
        "kind": "mochi",
        "shop_price": 100,
        "exp": None,
        "ev_stat": "def",
        "ev_amount": 10,
    },
    "genius-mochi": {
        "name": "Genius Mochi",
        "kind": "mochi",
        "shop_price": 100,
        "exp": None,
        "ev_stat": "spa",
        "ev_amount": 10,
    },
    "clever-mochi": {
        "name": "Clever Mochi",
        "kind": "mochi",
        "shop_price": 100,
        "exp": None,
        "ev_stat": "spd",
        "ev_amount": 10,
    },
    "swift-mochi": {
        "name": "Swift Mochi",
        "kind": "mochi",
        "shop_price": 100,
        "exp": None,
        "ev_stat": "spe",
        "ev_amount": 10,
    },
    "fresh-start-mochi": {
        "name": "Fresh-Start Mochi",
        "kind": "mochi-lower",
        "shop_price": 75,
        "exp": None,
        "ev_stat": None,
        "ev_amount": 10,
    },
    "health-feather": {
        "name": "Health Feather",
        "kind": "feather",
        "shop_price": 10,
        "exp": None,
        "ev_stat": "hp",
        "ev_amount": 1,
    },
    "muscle-feather": {
        "name": "Muscle Feather",
        "kind": "feather",
        "shop_price": 10,
        "exp": None,
        "ev_stat": "atk",
        "ev_amount": 1,
    },
    "resist-feather": {
        "name": "Resist Feather",
        "kind": "feather",
        "shop_price": 10,
        "exp": None,
        "ev_stat": "def",
        "ev_amount": 1,
    },
    "genius-feather": {
        "name": "Genius Feather",
        "kind": "feather",
        "shop_price": 10,
        "exp": None,
        "ev_stat": "spa",
        "ev_amount": 1,
    },
    "clever-feather": {
        "name": "Clever Feather",
        "kind": "feather",
        "shop_price": 10,
        "exp": None,
        "ev_stat": "spd",
        "ev_amount": 1,
    },
    "swift-feather": {
        "name": "Swift Feather",
        "kind": "feather",
        "shop_price": 10,
        "exp": None,
        "ev_stat": "spe",
        "ev_amount": 1,
    },
    "pretty-feather": {
        "name": "Pretty Feather",
        "kind": "feather-lower",
        "shop_price": 10,
        "exp": None,
        "ev_stat": None,
        "ev_amount": 1,
    },
    "exp-candy-xs": {
        "name": "Exp. Candy XS",
        "kind": "exp-candy",
        "shop_price": None,
        "exp": 100,
        "ev_stat": None,
        "ev_amount": None,
    },
    "exp-candy-s": {
        "name": "Exp. Candy S",
        "kind": "exp-candy",
        "shop_price": None,
        "exp": 800,
        "ev_stat": None,
        "ev_amount": None,
    },
    "exp-candy-m": {
        "name": "Exp. Candy M",
        "kind": "exp-candy",
        "shop_price": None,
        "exp": 3000,
        "ev_stat": None,
        "ev_amount": None,
    },
    "exp-candy-l": {
        "name": "Exp. Candy L",
        "kind": "exp-candy",
        "shop_price": None,
        "exp": 10000,
        "ev_stat": None,
        "ev_amount": None,
    },
    "exp-candy-xl": {
        "name": "Exp. Candy XL",
        "kind": "exp-candy",
        "shop_price": None,
        "exp": 30000,
        "ev_stat": None,
        "ev_amount": None,
    },
}

MEDICINE_NAME_LOOKUP = {
    re.sub(r"[^a-z0-9]+", "", str(definition["name"]).lower()): key
    for key, definition in MEDICINE_DEFINITIONS.items()
}
MEDICINE_NAME_LOOKUP.update(
    {
        "rarecandy": "rare-candy",
        "healthmochi": "health-mochi",
        "musclemochi": "muscle-mochi",
        "resistmochi": "resist-mochi",
        "geniusmochi": "genius-mochi",
        "clevermochi": "clever-mochi",
        "swiftmochi": "swift-mochi",
        "freshstartmochi": "fresh-start-mochi",
        "expcandyxs": "exp-candy-xs",
        "xsexpcandy": "exp-candy-xs",
        "expcandys": "exp-candy-s",
        "sexpcandy": "exp-candy-s",
        "expcandym": "exp-candy-m",
        "mexpcandy": "exp-candy-m",
        "expcandyl": "exp-candy-l",
        "lexpcandy": "exp-candy-l",
        "expcandyxl": "exp-candy-xl",
        "xlexpcandy": "exp-candy-xl",
    }
)

MOCHI_KEYS: tuple[str, ...] = (
    "health-mochi",
    "muscle-mochi",
    "resist-mochi",
    "genius-mochi",
    "clever-mochi",
    "swift-mochi",
    "fresh-start-mochi",
)

FEATHER_KEYS: tuple[str, ...] = (
    "health-feather",
    "muscle-feather",
    "resist-feather",
    "genius-feather",
    "clever-feather",
    "swift-feather",
    "pretty-feather",
)

CANDY_KEYS: tuple[str, ...] = (
    "rare-candy",
    "exp-candy-xs",
    "exp-candy-s",
    "exp-candy-m",
    "exp-candy-l",
    "exp-candy-xl",
)

SHOP_MEDICINE_KEYS: tuple[str, ...] = (
    "rare-candy",
    "health-mochi",
    "muscle-mochi",
    "resist-mochi",
    "genius-mochi",
    "clever-mochi",
    "swift-mochi",
    "fresh-start-mochi",
    "health-feather",
    "muscle-feather",
    "resist-feather",
    "genius-feather",
    "clever-feather",
    "swift-feather",
    "pretty-feather",
)

EXP_CANDY_DROP_KEYS: tuple[str, ...] = (
    "exp-candy-xs",
    "exp-candy-s",
    "exp-candy-m",
    "exp-candy-l",
    "exp-candy-xl",
)


def normalize_medicine_key(value: str) -> str | None:
    cleaned = re.sub(r"[^a-z0-9]+", "", value.lower())
    if not cleaned:
        return None
    return MEDICINE_NAME_LOOKUP.get(cleaned)


def medicine_name(key: str) -> str:
    definition = MEDICINE_DEFINITIONS.get(key, {})
    return str(definition.get("name") or key)


def medicine_shop_price(key: str) -> int | None:
    value = MEDICINE_DEFINITIONS.get(key, {}).get("shop_price")
    return int(value) if isinstance(value, int) else None
