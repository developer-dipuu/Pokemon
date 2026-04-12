from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence

from bot.config import PROJECT_DIR
from bot.game.services.pokemon_data import artwork_key, species_key

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - optional runtime dependency
    Image = None
    ImageDraw = None


CANVAS_SIZE = (640, 360)

# Manual layout tuning for `/trstbimage`.
# Edit these values, run `/trstbimage`, and keep iterating until the placement feels right.
# - `anchor` is the sprite "feet" position on the battlefield.
# - `max_size` is the size clamp used before pasting the sprite.
# - `base_scale` lets you globally shrink or enlarge the sprite before the clamp.
# - `lift` raises the sprite above the platform.
# - `highlight_lift` is extra vertical lift for the acting side.
# - `dynamax_scale` is applied while the Pokemon is Dynamaxed/Gigantamaxed.
PLAYER_LAYOUT = {
    "anchor": (188, 312),
    "max_size": (250, 170),
    "base_scale": 1.00,
    "lift": -35,
    "highlight_lift": 10,
    "highlight_scale": 1.08,
    "dynamax_scale": 1.28,
}

OPPONENT_LAYOUT = {
    "anchor": (466, 184),
    "max_size": (220, 145),
    "base_scale": 1.00,
    "lift": -15,
    "highlight_lift": 10,
    "highlight_scale": 1.08,
    "dynamax_scale": 1.22,
}

# Platform tuning. These values are separate so you can move the bases independently from the sprites.
PLAYER_PLATFORM = {"anchor": PLAYER_LAYOUT["anchor"], "radius_x": 92, "radius_y": 16}
OPPONENT_PLATFORM = {"anchor": OPPONENT_LAYOUT["anchor"], "radius_x": 92, "radius_y": 16}

# Overlay opacity tuning.
OVERLAY_ALPHA = {
    "terrain": 150,
    "weather": 165,
    "room": 160,
}

DEFAULT_PREVIEW_PLAYER = "Pikachu"
DEFAULT_PREVIEW_OPPONENT = "Charizard"

WEATHER_ALIASES = {
    "raindance": "rain",
    "rain": "rain",
    "primordialsea": "rain",
    "sunnyday": "sunnyday",
    "sun": "sunnyday",
    "harshsunshine": "sunnyday",
    "desolateland": "sunnyday",
    "sandstorm": "sandstorm",
    "snow": "snowstrom",
    "hail": "snowstrom",
    "snowscape": "snowstrom",
    "snowstorm": "snowstrom",
    "snowstrom": "snowstrom",
}

TERRAIN_ALIASES = {
    "electricterrain": "electric",
    "grassyterrain": "grassy",
    "mistyterrain": "misty",
    "psychicterrain": "psychic",
}

ROOM_ALIASES = {
    "trickroom": "trickroom",
    "wonderroom": "wonderroom",
    "magicroom": "wonderroom",
}


def _safe_details_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "," in text:
        return text.split(",", 1)[0].strip()
    return text


class BattleVisualRenderer:
    def __init__(self) -> None:
        asset_root = PROJECT_DIR / "assets"
        sprite_root = asset_root / "sprite"
        self.terrain_paths = sorted((asset_root / "terrains").glob("*.*"))
        self.effect_terrain_paths = self._build_effect_index(asset_root / "effect_terrains")
        self.room_effect_paths = self._build_effect_index(asset_root / "room_effects")
        self.weather_effect_paths = self._build_effect_index(asset_root / "weather_effects")
        self.front_index = self._build_sprite_index(sprite_root / "sprites-gen5")
        self.front_shiny_index = self._build_sprite_index(sprite_root / "sprites-gen5-shiny")
        self.back_index = self._build_sprite_index(sprite_root / "sprites-gen5-back")
        self.back_shiny_index = self._build_sprite_index(sprite_root / "sprites-gen5-back-shiny")

    @property
    def available(self) -> bool:
        return Image is not None and bool(self.terrain_paths)

    def render(self, battle, *, highlight_slot: str | None) -> tuple[BytesIO, str] | None:
        if not self.available:
            return None

        scene = self._scene_payload(battle, highlight_slot=highlight_slot)
        return self._render_scene(scene)

    def render_preview(
        self,
        *,
        player_species: str,
        opponent_species: str,
        highlight_slot: str = "p1",
        terrain: str = "",
        weather: str = "",
        room_effects: Sequence[str] | None = None,
    ) -> tuple[BytesIO, str] | None:
        if not self.available:
            return None

        scene = {
            "battle_id": f"preview:{player_species}:{opponent_species}:{terrain}:{weather}:{','.join(room_effects or [])}",
            "battle_mode": "preview",
            "turn": 1,
            "highlight_slot": highlight_slot or "",
            "terrain": self._terrain_overlay_key(terrain),
            "weather": self._weather_overlay_key(weather),
            "room_effects": sorted(filter(None, (self._room_overlay_key(item) for item in (room_effects or [])))),
            "p1": {
                "species": player_species,
                "display_species": player_species,
                "shiny": False,
                "fainted": False,
                "dynamaxed": False,
                "gigantamax_species": "",
            },
            "p2": {
                "species": opponent_species,
                "display_species": opponent_species,
                "shiny": False,
                "fainted": False,
                "dynamaxed": False,
                "gigantamax_species": "",
            },
        }
        return self._render_scene(scene)

    def _render_scene(self, scene: dict[str, Any]) -> tuple[BytesIO, str]:
        fingerprint = hashlib.sha1(json.dumps(scene, sort_keys=True).encode("utf-8")).hexdigest()
        canvas = self._compose(scene)
        output = BytesIO()
        output.name = f"battle-{fingerprint[:10]}.png"
        canvas.save(output, format="PNG")
        output.seek(0)
        return output, fingerprint

    def _scene_payload(self, battle, *, highlight_slot: str | None) -> dict[str, Any]:
        return {
            "battle_id": battle.battle_id,
            "battle_mode": battle.battle_mode,
            "turn": int(getattr(battle.public_view, "turn", 0) or 0),
            "highlight_slot": highlight_slot or "",
            "terrain": self._terrain_overlay_key(getattr(battle.public_view, "terrain", "")),
            "weather": self._weather_overlay_key(getattr(battle.public_view, "weather", "")),
            "room_effects": sorted(
                filter(None, (self._room_overlay_key(item) for item in getattr(battle.public_view, "room_effects", set())))
            ),
            "p1": self._slot_payload(battle, "p1"),
            "p2": self._slot_payload(battle, "p2"),
        }

    def _slot_payload(self, battle, slot: str) -> dict[str, Any]:
        active = battle.public_view.active.get(slot) or {}
        details = _safe_details_name(str(active.get("details") or active.get("name") or "").strip())
        giganta_species = str(active.get("gigantamax_species") or "").strip()
        display_species = giganta_species or details or str(active.get("name") or "").strip()
        return {
            "species": details or str(active.get("name") or "").strip(),
            "display_species": display_species,
            "shiny": bool(active.get("shiny")),
            "fainted": bool(active.get("fainted")),
            "dynamaxed": bool(active.get("dynamaxed")),
            "gigantamax_species": giganta_species,
        }

    def _compose(self, scene: dict[str, Any]):
        assert Image is not None and ImageDraw is not None
        background = self._load_background(scene)
        canvas = background.resize(CANVAS_SIZE, self._resample_bilinear()).convert("RGBA")

        self._apply_overlay(canvas, self.effect_terrain_paths.get(str(scene.get("terrain") or "")), alpha=OVERLAY_ALPHA["terrain"])
        self._apply_overlay(canvas, self.weather_effect_paths.get(str(scene.get("weather") or "")), alpha=OVERLAY_ALPHA["weather"])
        for room_key in scene.get("room_effects") or []:
            self._apply_overlay(canvas, self.room_effect_paths.get(str(room_key)), alpha=OVERLAY_ALPHA["room"])

        atmosphere = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
        atmosphere_draw = ImageDraw.Draw(atmosphere, "RGBA")
        atmosphere_draw.rectangle((0, 0, CANVAS_SIZE[0], CANVAS_SIZE[1]), fill=(10, 20, 34, 24))
        atmosphere_draw.rectangle((0, 0, CANVAS_SIZE[0], 112), fill=(255, 255, 255, 18))
        atmosphere_draw.polygon([(0, 248), (240, 180), (640, 168), (640, 360), (0, 360)], fill=(29, 62, 42, 62))
        canvas = Image.alpha_composite(canvas, atmosphere)

        draw = ImageDraw.Draw(canvas, "RGBA")
        self._draw_platform(draw, OPPONENT_PLATFORM, highlight=scene["highlight_slot"] == "p2")
        self._draw_platform(draw, PLAYER_PLATFORM, highlight=scene["highlight_slot"] == "p1")

        self._paste_slot_sprite(
            canvas,
            scene["p2"],
            layout=OPPONENT_LAYOUT,
            back=False,
            highlight=scene["highlight_slot"] == "p2",
        )
        self._paste_slot_sprite(
            canvas,
            scene["p1"],
            layout=PLAYER_LAYOUT,
            back=True,
            highlight=scene["highlight_slot"] == "p1",
        )
        return canvas

    def _draw_platform(self, draw, config: dict[str, Any], *, highlight: bool) -> None:
        x, y = config["anchor"]
        radius_x = int(config["radius_x"])
        radius_y = int(config["radius_y"])
        draw.ellipse((x - radius_x, y - radius_y, x + radius_x, y + radius_y), fill=(0, 0, 0, 96))
        draw.ellipse((x - 72, y - 10, x + 72, y + 10), fill=(38, 48, 60, 70), outline=(255, 255, 255, 70), width=2)
        if highlight:
            draw.ellipse((x - 108, y - 28, x + 108, y + 28), outline=(255, 214, 102, 230), width=4)
            draw.ellipse((x - 122, y - 38, x + 122, y + 38), outline=(255, 255, 255, 70), width=2)

    def _paste_slot_sprite(
        self,
        canvas,
        slot: dict[str, Any],
        *,
        layout: dict[str, Any],
        back: bool,
        highlight: bool,
    ) -> None:
        display_species = str(slot.get("display_species") or slot.get("species") or "").strip()
        if not display_species:
            return
        sprite_path = self._sprite_path(display_species, shiny=bool(slot.get("shiny")), back=back)
        if sprite_path is None:
            return

        assert Image is not None
        with Image.open(sprite_path) as raw_sprite:
            sprite = raw_sprite.convert("RGBA")

        sprite = self._fit_sprite(
            sprite,
            max_size=tuple(layout["max_size"]),
            base_scale=float(layout["base_scale"]),
            extra_scale=float(layout["dynamax_scale"]) if slot.get("dynamaxed") else 1.0,
            highlight=highlight,
            highlight_scale=float(layout["highlight_scale"]),
        )

        if bool(slot.get("fainted")):
            faint_alpha = sprite.getchannel("A").point(lambda value: int(value * 0.38))
            sprite.putalpha(faint_alpha)

        anchor_x, anchor_y = layout["anchor"]
        lift = int(layout["lift"]) + (int(layout["highlight_lift"]) if highlight else 0)
        x = int(anchor_x) - sprite.width // 2
        y = int(anchor_y) - sprite.height - lift
        canvas.alpha_composite(sprite, (x, y))

    def _load_background(self, scene: dict[str, Any]):
        assert Image is not None
        pool = self._terrain_pool(scene)
        terrain_path = pool[self._stable_index(scene["battle_id"], len(pool))]
        with Image.open(terrain_path) as raw_bg:
            return raw_bg.convert("RGBA")

    def _terrain_pool(self, scene: dict[str, Any]) -> list[Path]:
        if not self.terrain_paths:
            return []
        if scene.get("battle_mode") == "wild":
            keywords = ("forest", "meadow", "beach", "desert", "cave", "sea")
        elif scene.get("battle_mode") == "preview":
            keywords = ("meadow", "forest", "city")
        else:
            keywords = ("city", "library", "leader", "aquacorde", "skypillar", "meadow")
        filtered = [path for path in self.terrain_paths if any(keyword in path.stem.lower() for keyword in keywords)]
        return filtered or self.terrain_paths

    def _sprite_path(self, species: str, *, shiny: bool, back: bool) -> Path | None:
        key = artwork_key(species)
        if back:
            primary = self.back_shiny_index if shiny else self.back_index
            fallback = self.back_index
        else:
            primary = self.front_shiny_index if shiny else self.front_index
            fallback = self.front_index

        path = primary.get(key)
        if path is not None:
            return path
        return fallback.get(key)

    def _fit_sprite(
        self,
        sprite,
        *,
        max_size: tuple[int, int],
        base_scale: float,
        extra_scale: float,
        highlight: bool,
        highlight_scale: float,
    ):
        assert Image is not None
        width, height = sprite.size
        if width <= 0 or height <= 0:
            return sprite
        scale = min(max_size[0] / width, max_size[1] / height)
        scale = max(scale, 1.0)
        scale *= max(base_scale, 0.1)
        scale *= max(extra_scale, 0.1)
        if highlight:
            scale *= max(highlight_scale, 0.1)
        resized = sprite.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            self._resample_nearest(),
        )
        return resized

    def _apply_overlay(self, canvas, path: Path | None, *, alpha: int) -> None:
        if path is None or not path.exists():
            return
        assert Image is not None
        with Image.open(path) as raw_overlay:
            overlay = raw_overlay.convert("RGBA").resize(CANVAS_SIZE, self._resample_bilinear())
        if alpha < 255:
            mask = overlay.getchannel("A").point(lambda value: int(value * (alpha / 255)))
            overlay.putalpha(mask)
        canvas.alpha_composite(overlay)

    def _terrain_overlay_key(self, value: str) -> str:
        return TERRAIN_ALIASES.get(species_key(value), "")

    def _weather_overlay_key(self, value: str) -> str:
        return WEATHER_ALIASES.get(species_key(value), "")

    def _room_overlay_key(self, value: str) -> str:
        return ROOM_ALIASES.get(species_key(value), "")

    def _build_sprite_index(self, folder: Path) -> dict[str, Path]:
        if not folder.exists():
            return {}
        index: dict[str, Path] = {}
        for path in folder.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() != ".png":
                continue
            key = artwork_key(path.stem)
            if key and key not in index:
                index[key] = path
        return index

    def _build_effect_index(self, folder: Path) -> dict[str, Path]:
        if not folder.exists():
            return {}
        index: dict[str, Path] = {}
        for path in folder.iterdir():
            if not path.is_file():
                continue
            key = species_key(path.stem)
            if key and key not in index:
                index[key] = path
        return index

    def _stable_index(self, seed: str, total: int) -> int:
        if total <= 1:
            return 0
        return int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % total

    def _resample_nearest(self):
        assert Image is not None
        return Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST

    def _resample_bilinear(self):
        assert Image is not None
        return Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR
