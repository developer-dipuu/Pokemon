from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import LOCATION_ENCOUNTERS_PATH, REGION_LOCATIONS_PATH


LATEST_GEN_MAP = {
    "kanto": 7,
    "johto": 4,
    "hoenn": 6,
    "sinnoh": 8,
    "unova": 5,
    "kalos": 6,
    "alola": 7,
    "galar": 8,
    "paldea": 9,
    "hisui": 8,
}

REGION_LABELS = {
    "kanto": "Kanto",
    "johto": "Johto",
    "hoenn": "Hoenn",
    "sinnoh": "Sinnoh",
    "unova": "Unova",
    "kalos": "Kalos",
    "alola": "Alola",
    "galar": "Galar",
    "paldea": "Paldea",
    "hisui": "Hisui",
}

RARITY_WEIGHTS = {
    "common": 20.0,
    "uncommon": 10.0,
    "rare": 5.0,
    "very rare": 2.0,
    "limited": 1.0,
    "special": 1.0,
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Showdownreal PokemonDB Importer)"}


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def clean_levels(text: str) -> tuple[int, int]:
    numbers = [int(value) for value in re.findall(r"\d+", text or "")]
    if not numbers:
        return 1, 1
    return (numbers[0], numbers[0]) if len(numbers) == 1 else (min(numbers), max(numbers))


def rarity_value(raw: str) -> float:
    text = raw.strip().lower()
    if not text:
        return 1.0
    if text.endswith("%"):
        return float(text.rstrip("%"))
    return RARITY_WEIGHTS.get(text, 1.0)


def build_location_url(region_id: str, location_name: str) -> str:
    return f"https://pokemondb.net/location/{region_id}-{slugify(location_name)}"


def load_existing_images() -> dict[tuple[str, str], dict]:
    path = Path(LOCATION_ENCOUNTERS_PATH)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[tuple[str, str], dict] = {}
    for region_id, region in raw.get("regions", {}).items():
        for location in region.get("locations", []):
            mapping[(str(region_id), str(location.get("name", "")))] = location
    return mapping


def iter_generation_nodes(header: Tag) -> list[Tag]:
    nodes: list[Tag] = []
    node = header
    while node is not None:
        node = node.find_next_sibling()
        if node is None:
            break
        if getattr(node, "name", "") == "h2":
            break
        nodes.append(node)
    return nodes


def normalize_species(cell: Tag) -> str:
    base = ""
    name_link = cell.select_one("a.ent-name")
    if name_link:
        base = name_link.get_text(" ", strip=True)
    if not base:
        image = cell.find("img")
        if image and image.get("alt"):
            base = str(image["alt"]).strip()
    regional_label = cell.find("small")
    if regional_label:
        label = regional_label.get_text(" ", strip=True)
        if label:
            return label.replace("Alolan ", "") + "-Alola" if label.startswith("Alolan ") else (
                label.replace("Galarian ", "") + "-Galar" if label.startswith("Galarian ") else (
                    label.replace("Hisuian ", "") + "-Hisui" if label.startswith("Hisuian ") else (
                        label.replace("Paldean ", "") + "-Paldea" if label.startswith("Paldean ") else label
                    )
                )
            )
    alt = cell.find("img")
    if alt and alt.get("alt"):
        alt_text = str(alt["alt"]).strip()
        if alt_text.startswith(("Alolan ", "Galarian ", "Hisuian ", "Paldean ")):
            return normalize_species_from_alt(alt_text)
    return base


def normalize_species_from_alt(value: str) -> str:
    return (
        value.replace("Alolan ", "") + "-Alola" if value.startswith("Alolan ") else
        value.replace("Galarian ", "") + "-Galar" if value.startswith("Galarian ") else
        value.replace("Hisuian ", "") + "-Hisui" if value.startswith("Hisuian ") else
        value.replace("Paldean ", "") + "-Paldea" if value.startswith("Paldean ") else
        value
    )


def parse_rarity(cell: Tag) -> str:
    text = cell.get_text(" ", strip=True)
    if text:
        return text
    image = cell.find("img")
    if image and image.get("alt"):
        return str(image["alt"]).strip()
    return ""


def parse_encounters(soup: BeautifulSoup, generation: int) -> list[dict]:
    header = next((h for h in soup.find_all("h2") if f"Generation {generation}" in h.get_text(" ", strip=True)), None)
    if header is None:
        return []

    encounters: list[dict] = []
    seen: set[tuple[str, int, int, str]] = set()
    for node in iter_generation_nodes(header):
        tables: list[Tag] = []
        if getattr(node, "name", "") == "table" and "data-table" in (node.get("class") or []):
            tables.append(node)
        tables.extend(node.select("table.data-table"))
        for table in tables:
            rows = table.find_all("tr")
            if not rows:
                continue
            for row in rows[1:]:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                species = normalize_species(cells[0]).strip()
                rarity_raw = parse_rarity(cells[-3])
                min_level, max_level = clean_levels(cells[-2].get_text(" ", strip=True))
                if not species or not rarity_raw:
                    continue
                item = {
                    "species": species,
                    "min_level": min_level,
                    "max_level": max_level,
                    "spawn_rate": rarity_value(rarity_raw),
                    "spawn_rate_raw": rarity_raw,
                }
                key = (item["species"], item["min_level"], item["max_level"], item["spawn_rate_raw"])
                if key in seen:
                    continue
                seen.add(key)
                encounters.append(item)
    return encounters


def import_locations() -> dict:
    source_locations = json.loads(Path(REGION_LOCATIONS_PATH).read_text(encoding="utf-8"))
    existing = load_existing_images()
    output = {
        "source": {
            "name": "Pokemon Database",
            "url": "https://pokemondb.net/location",
            "notes": "Encounter tables imported from PokemonDB location pages; only locations with encounter data are kept.",
        },
        "regions": {},
    }

    with requests.Session() as session:
        session.headers.update(HEADERS)
        for region_label, locations in source_locations.items():
            region_id = slugify(region_label)
            generation = LATEST_GEN_MAP.get(region_id)
            if not generation or not isinstance(locations, list):
                continue
            region_output = {"label": REGION_LABELS.get(region_id, region_label), "locations": []}
            print(f"Importing {region_label} ({len(locations)} locations)")
            for location in locations:
                location_name = str(location.get("name") or "").strip()
                if not location_name:
                    continue
                print(f"  - {location_name}")
                url = build_location_url(region_id, location_name)
                try:
                    response = session.get(url, timeout=20)
                    if response.status_code != 200:
                        continue
                    soup = BeautifulSoup(response.text, "html.parser")
                    encounters = parse_encounters(soup, generation)
                    if not encounters:
                        continue
                    previous = existing.get((region_id, location_name), {})
                    region_output["locations"].append(
                        {
                            "id": slugify(location_name),
                            "name": location_name,
                            "url": url,
                            "image_url": previous.get("image_url"),
                            "encounters": encounters,
                        }
                    )
                except Exception:
                    continue
            output["regions"][region_id] = region_output
    return output


def main() -> None:
    data = import_locations()
    Path(LOCATION_ENCOUNTERS_PATH).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {LOCATION_ENCOUNTERS_PATH}")


if __name__ == "__main__":
    main()
