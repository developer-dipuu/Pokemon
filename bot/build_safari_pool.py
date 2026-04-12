from __future__ import annotations

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import EVOLUTION_CHAINS_PATH, SAFARI_POOLS_PATH, SPECIES_REFERENCE_PATH, STARTERS_PATH

SAFARI_REGION_ID = "national"
SAFARI_REGION_LABEL = "National Safari Reserve"
SAFARI_BALLS = 30
SAFARI_CATCH_MULTIPLIER = 4.0

STARTER_STAGE_SETTINGS = {
    0: {"min_level": 18, "max_level": 30, "weight": 12},
    1: {"min_level": 30, "max_level": 44, "weight": 8},
    2: {"min_level": 45, "max_level": 60, "weight": 5},
}
PSEUDO_STAGE_SETTINGS = {
    0: {"min_level": 28, "max_level": 42, "weight": 8},
    1: {"min_level": 43, "max_level": 57, "weight": 5},
    2: {"min_level": 58, "max_level": 75, "weight": 3},
}
LEGENDARY_SETTINGS = {"min_level": 60, "max_level": 75, "weight": 2}
MYTHICAL_SETTINGS = {"min_level": 58, "max_level": 72, "weight": 2}
PARADOX_SETTINGS = {"min_level": 58, "max_level": 74, "weight": 3}
ULTRA_BEAST_SETTINGS = {"min_level": 58, "max_level": 74, "weight": 2}

PSEUDO_LEGENDARY_ROOTS = [
    "dratini",
    "larvitar",
    "bagon",
    "beldum",
    "gible",
    "deino",
    "goomy",
    "jangmo-o",
    "dreepy",
    "frigibax",
]

LEGENDARY_KEYS = [
    "articuno",
    "zapdos",
    "moltres",
    "mewtwo",
    "raikou",
    "entei",
    "suicune",
    "lugia",
    "ho-oh",
    "regirock",
    "regice",
    "registeel",
    "latias",
    "latios",
    "kyogre",
    "groudon",
    "rayquaza",
    "uxie",
    "mesprit",
    "azelf",
    "dialga",
    "palkia",
    "heatran",
    "regigigas",
    "giratina-altered",
    "cresselia",
    "cobalion",
    "terrakion",
    "virizion",
    "tornadus-incarnate",
    "thundurus-incarnate",
    "reshiram",
    "zekrom",
    "landorus-incarnate",
    "kyurem",
    "xerneas",
    "yveltal",
    "zygarde-50",
    "type-null",
    "silvally",
    "tapu-koko",
    "tapu-lele",
    "tapu-bulu",
    "tapu-fini",
    "cosmog",
    "cosmoem",
    "solgaleo",
    "lunala",
    "necrozma",
    "zacian",
    "zamazenta",
    "eternatus",
    "kubfu",
    "urshifu-single-strike",
    "urshifu-rapid-strike",
    "regieleki",
    "regidrago",
    "glastrier",
    "spectrier",
    "calyrex",
    "enamorus-incarnate",
    "articuno-galar",
    "zapdos-galar",
    "moltres-galar",
    "koraidon",
    "miraidon",
    "wo-chien",
    "chien-pao",
    "ting-lu",
    "chi-yu",
    "okidogi",
    "munkidori",
    "fezandipiti",
    "ogerpon",
    "terapagos",
]

MYTHICAL_KEYS = [
    "mew",
    "celebi",
    "jirachi",
    "deoxys-normal",
    "phione",
    "manaphy",
    "darkrai",
    "shaymin-land",
    "arceus",
    "victini",
    "keldeo-ordinary",
    "meloetta-aria",
    "genesect",
    "diancie",
    "hoopa",
    "volcanion",
    "magearna",
    "marshadow",
    "zeraora",
    "meltan",
    "melmetal",
    "zarude",
    "pecharunt",
]

PARADOX_KEYS = [
    "great-tusk",
    "scream-tail",
    "brute-bonnet",
    "flutter-mane",
    "slither-wing",
    "sandy-shocks",
    "roaring-moon",
    "iron-treads",
    "iron-bundle",
    "iron-hands",
    "iron-jugulis",
    "iron-moth",
    "iron-thorns",
    "iron-valiant",
    "walking-wake",
    "iron-leaves",
    "gouging-fire",
    "raging-bolt",
    "iron-boulder",
    "iron-crown",
    "koraidon",
    "miraidon",
]

ULTRA_BEAST_KEYS = [
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
]


def species_key(name: str) -> str:
    text = name.strip().lower().replace("♀", "-f").replace("♂", "-m")
    text = text.replace(" ", "-").replace(".", "").replace("'", "")
    return re.sub(r"[^a-z0-9-]+", "", text)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_forward_graph(chains: list[dict[str, Any]]) -> dict[str, list[str]]:
    forward: dict[str, set[str]] = defaultdict(set)
    for chain in chains:
        current = species_key(str(chain.get("current_pokemon") or ""))
        evolved = species_key(str(chain.get("evolved_pokemon") or ""))
        if current and evolved:
            forward[current].add(evolved)
    return {key: sorted(values) for key, values in forward.items()}


def expand_line_depths(roots: list[str], forward: dict[str, list[str]]) -> dict[str, int]:
    depths: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque((species_key(root), 0) for root in roots)
    while queue:
        key, depth = queue.popleft()
        if not key:
            continue
        previous = depths.get(key)
        if previous is not None and previous <= depth:
            continue
        depths[key] = depth
        for evolved in forward.get(key, []):
            queue.append((evolved, depth + 1))
    return depths


def stage_settings(depth: int, mapping: dict[int, dict[str, int]]) -> dict[str, int]:
    if depth in mapping:
        return mapping[depth]
    return mapping[max(mapping)]


def ensure_species(keys: list[str], species_reference: dict[str, Any], *, label: str) -> list[str]:
    missing = [key for key in keys if key not in species_reference]
    if missing:
        raise SystemExit(f"Missing {label} species in reference data: {', '.join(sorted(missing))}")
    return keys


def add_entry(
    entries_by_key: dict[str, dict[str, Any]],
    category_sets: dict[str, set[str]],
    species_reference: dict[str, Any],
    *,
    key: str,
    category: str,
    settings: dict[str, int],
) -> None:
    if key not in species_reference:
        raise SystemExit(f"Species {key} is missing from species reference data.")
    name = str(species_reference[key].get("name") or key)
    existing = entries_by_key.get(key)
    if existing is None:
        existing = {
            "species": name,
            "min_level": int(settings["min_level"]),
            "max_level": int(settings["max_level"]),
            "weight": int(settings["weight"]),
            "categories": [category],
        }
        entries_by_key[key] = existing
    else:
        existing["min_level"] = min(int(existing["min_level"]), int(settings["min_level"]))
        existing["max_level"] = max(int(existing["max_level"]), int(settings["max_level"]))
        existing["weight"] = max(int(existing["weight"]), int(settings["weight"]))
        if category not in existing["categories"]:
            existing["categories"].append(category)
    category_sets[category].add(name)


def main() -> None:
    species_reference = load_json(SPECIES_REFERENCE_PATH)
    starters_raw = load_json(STARTERS_PATH).get("regions", [])
    evolution_chains = load_json(EVOLUTION_CHAINS_PATH).get("evolution_chains", [])

    if not isinstance(species_reference, dict):
        raise SystemExit("Species reference data is not a mapping.")
    if not isinstance(starters_raw, list):
        raise SystemExit("Starter data is not a list.")
    if not isinstance(evolution_chains, list):
        raise SystemExit("Evolution chain data is not a list.")

    forward = build_forward_graph(evolution_chains)
    starter_roots = [
        species_key(starter)
        for region in starters_raw
        if isinstance(region, dict)
        for starter in region.get("starters", [])
    ]
    starter_roots = ensure_species(sorted(set(starter_roots)), species_reference, label="starter")
    pseudo_roots = ensure_species(PSEUDO_LEGENDARY_ROOTS, species_reference, label="pseudo-legendary")
    legendary_keys = ensure_species(LEGENDARY_KEYS, species_reference, label="legendary")
    mythical_keys = ensure_species(MYTHICAL_KEYS, species_reference, label="mythical")
    paradox_keys = ensure_species(PARADOX_KEYS, species_reference, label="paradox")
    ultra_beast_keys = ensure_species(ULTRA_BEAST_KEYS, species_reference, label="ultra beast")

    starter_depths = expand_line_depths(starter_roots, forward)
    pseudo_depths = expand_line_depths(pseudo_roots, forward)

    entries_by_key: dict[str, dict[str, Any]] = {}
    category_sets: dict[str, set[str]] = defaultdict(set)

    for key, depth in starter_depths.items():
        add_entry(
            entries_by_key,
            category_sets,
            species_reference,
            key=key,
            category="starters",
            settings=stage_settings(depth, STARTER_STAGE_SETTINGS),
        )

    for key, depth in pseudo_depths.items():
        add_entry(
            entries_by_key,
            category_sets,
            species_reference,
            key=key,
            category="pseudo_legendaries",
            settings=stage_settings(depth, PSEUDO_STAGE_SETTINGS),
        )

    for key in legendary_keys:
        add_entry(
            entries_by_key,
            category_sets,
            species_reference,
            key=key,
            category="legendaries",
            settings=LEGENDARY_SETTINGS,
        )

    for key in mythical_keys:
        add_entry(
            entries_by_key,
            category_sets,
            species_reference,
            key=key,
            category="mythicals",
            settings=MYTHICAL_SETTINGS,
        )

    for key in paradox_keys:
        add_entry(
            entries_by_key,
            category_sets,
            species_reference,
            key=key,
            category="paradox",
            settings=PARADOX_SETTINGS,
        )

    for key in ultra_beast_keys:
        add_entry(
            entries_by_key,
            category_sets,
            species_reference,
            key=key,
            category="ultra_beasts",
            settings=ULTRA_BEAST_SETTINGS,
        )

    entries = sorted(entries_by_key.values(), key=lambda item: str(item["species"]).lower())
    for entry in entries:
        entry["categories"] = sorted(entry["categories"])

    all_species = [str(entry["species"]) for entry in entries]
    payload = {
        "meta": {
            "region_id": SAFARI_REGION_ID,
            "region_label": SAFARI_REGION_LABEL,
            "safari_balls": SAFARI_BALLS,
            "catch_multiplier": SAFARI_CATCH_MULTIPLIER,
            "actions": ["Safari Ball", "Run"],
            "pokemon_count": len(entries),
            "category_counts": {
                category: len(names)
                for category, names in sorted(category_sets.items(), key=lambda item: item[0])
            },
            "species": all_species,
        },
        "regions": {
            SAFARI_REGION_ID: entries,
        },
    }

    SAFARI_POOLS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(entries)} Safari species to {SAFARI_POOLS_PATH}")


if __name__ == "__main__":
    main()
