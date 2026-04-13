from __future__ import annotations

import re
import asyncio
import html
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import Button
from telethon.errors import MessageNotModifiedError, RPCError
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.types import User
from telethon.utils import get_display_name

from bot.battle.gym import (
    BROCK_IMAGE_PATH,
    BROCK_TEAM_SPECS,
    GYMS_BY_REGION,
    GYM_REGIONS,
    choose_brock_action,
    gym_requirement_text,
    validate_gym_challenger_team,
)
from bot.battle.models import BattleSession, PendingChallenge, PlayerState
from bot.battle.protocol import (
    PublicBattleView,
    clean_error,
    details_name,
    fainted,
    ident_name,
    ident_side,
    parse_condition,
    protocol_parts,
)
from bot.bridge.dex_tools import run_dex_tool
from bot.battle.visuals import BattleVisualRenderer, DEFAULT_PREVIEW_OPPONENT, DEFAULT_PREVIEW_PLAYER
from bot.bridge.showdown_bridge import ShowdownBattleProcess, ShowdownBridgeError
from bot.config import BOT_DIR, PROJECT_DIR, DEFAULT_RANDOM_BATTLE_FORMAT, DEFAULT_RPG_BATTLE_FORMAT, SHOWDOWN_DIR, RUNTIME_DIR
from bot.db.repositories import TeamRepository, TrainerRepository
from bot.db.session import run_db_work_async
from bot.game.services.pokemon_data import PokemonDataService

POPUP_LIMIT = 195
ACTION_COOLDOWN_SECONDS = 1.0
PUBLIC_RENDER_MIN_INTERVAL = 1.0
CHALLENGE_EXPIRY_SECONDS = 60.0
VISUAL_CAPTION_LIMIT = 900
RECOVERABLE_BATTLE_WINDOW_SECONDS = 60
RECOVERABLE_BATTLES_PATH = RUNTIME_DIR / "recoverable_battles.json"
DEFAULT_CHALLENGE_MODE = "owned"
DEFAULT_CHALLENGE_GENERATION = 9
SUPPORTED_CHALLENGE_GENERATIONS = (9, 8, 7, 6, 5, 4, 3, 2, 1)
FULL_GIMMICK_FORMAT_ID = "gen9fullgimmicknationaldex"
FULL_GIMMICK_FORMAT_LABEL = "Gen 9 Full Gimmick National Dex"
PRIMARY_GIMMICK_ACTIONS = {"terastallize", "mega", "megax", "megay", "dynamax"}
PRIMARY_GIMMICK_LABELS = {
    "terastallize": "Tera",
    "mega": "Mega",
    "megax": "Mega X",
    "megay": "Mega Y",
    "dynamax": "Dynamax",
}
STAT_LINE_LABELS = {
    "hp": "HP",
    "atk": "Atk",
    "def": "Def",
    "spa": "SpA",
    "spd": "SpD",
    "spe": "Spe",
}
ITEM_STAT_NOTES = {
    "choiceband": {"atk": "Choice Band"},
    "choicespecs": {"spa": "Choice Specs"},
    "choicescarf": {"spe": "Choice Scarf"},
    "assaultvest": {"spd": "Assault Vest"},
    "eviolite": {"def": "Eviolite", "spd": "Eviolite"},
    "thickclub": {"atk": "Thick Club"},
    "lightball": {"atk": "Light Ball", "spa": "Light Ball"},
    "deepseatooth": {"spa": "Deep Sea Tooth"},
    "deepseascale": {"spd": "Deep Sea Scale"},
    "metalpowder": {"def": "Metal Powder"},
    "quickpowder": {"spe": "Quick Powder"},
}

RANDOM_BATTLE_FORMATS: dict[int, tuple[str, str]] = {
    9: (DEFAULT_RANDOM_BATTLE_FORMAT, "Gen 9 Random Battle"),
    8: ("gen8randombattle", "Gen 8 Random Battle"),
    7: ("gen7randombattle", "Gen 7 Random Battle"),
    6: ("gen6randombattle", "Gen 6 Random Battle"),
    5: ("gen5randombattle", "Gen 5 Random Battle"),
    4: ("gen4randombattle", "Gen 4 Random Battle"),
    3: ("gen3randombattle", "Gen 3 Random Battle"),
    2: ("gen2randombattle", "Gen 2 Random Battle"),
    1: ("gen1randombattle", "Gen 1 Random Battle"),
}
GYM_COMMAND_LOCK_MESSAGE = "Gym battles are temporarily locked while we stabilize the bot."
logger = logging.getLogger("PokemonBot.battle")
SIGNATURE_ZMOVES = {
    "10,000,000 volt thunderbolt": "10000000voltthunderbolt.png",
    "catastropika": "catastropika.png",
    "clangorous soulblaze": "clangoroussoulblaze.png",
    "extreme evoboost": "extremeevoboost.png",
    "genesis supernova": "genesissupernova.png",
    "guardian of alola": "guardianofalola.png",
    "let's snuggle forever": "letssnuggleforever.png",
    "light that burns the sky": "lightthatburnsthesky.png",
    "malicious moonsault": "maliciousmoonsault.png",
    "menacing moonraze maelstrom": "menacingmoonrazemaelstrom.png",
    "oceanic operetta": "oceanicoperetta.png",
    "pulverizing pancake": "pulverizingpancake.png",
    "searing sunraze smash": "searingsunrazesmash.png",
    "sinister arrow raid": "sinisterarrowraid.png",
    "soul-stealing 7-star strike": "soulstealing7starstrike.png",
    "splintered stormshards": "splinteredstormshards.png",
    "stoked sparksurfer": "stokedsparksurfer.png",
}

def display_name(user: User | None, fallback: str = "Trainer") -> str:
    if not user:
        return fallback
    name = get_display_name(user).strip()
    return name or fallback


def mention_html(user_id: int | None, label: str) -> str:
    safe_label = html.escape(label or "Trainer")
    if not user_id:
        return safe_label
    return f'<a href="tg://user?id={user_id}">{safe_label}</a>'


def mention_markdown(user_id: int | None, label: str) -> str:
    safe_label = re.sub(r"[\[\]\(\)]", "", str(label or "Trainer")).strip() or "Trainer"
    if not user_id:
        return safe_label
    return f"[{safe_label}](tg://user?id={int(user_id)})"


def mega_caption_name(species: str) -> str:
    clean = species.strip()
    if "-Mega-" in clean:
        base, suffix = clean.split("-Mega-", 1)
        return f"Mega {base} {suffix.replace('-', ' ')}".strip()
    if clean.endswith("-Mega"):
        return f"Mega {clean[:-5]}".strip()
    return clean.replace("-", " ").strip()


def chunk_specs(specs: list[tuple[str, str]], per_row: int) -> list[list[tuple[str, str]]]:
    return [specs[index:index + per_row] for index in range(0, len(specs), per_row)]


def compact_text(text: str, limit: int = POPUP_LIMIT) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    if limit <= 3:
        return clean[:limit]
    return clean[:limit - 3].rstrip() + "..."


def hp_bar(percent: int | None, width: int = 10) -> str:
    if percent is None:
        return "?" * width
    bounded = max(0, min(100, percent))
    filled = round((bounded / 100) * width)
    return ("█" * filled) + ("░" * (width - filled))


def escape_html_text(value: Any) -> str:
    return html.escape(str(value or ""))


def hp_bar_ascii(percent: int | None, width: int = 10) -> str:
    if percent is None:
        return "?" * width
    bounded = max(0, min(100, percent))
    filled = round((bounded / 100) * width)
    return ("#" * filled) + ("-" * (width - filled))


def format_types(types: list[str] | None) -> str:
    if not types:
        return "Unknown"
    return "/".join(types)


def compact_move_line(index: int, move: dict[str, Any]) -> str:
    move_name = str(move.get("move", f"Move {index}"))
    move_type = str(move.get("displayType") or "?")
    accuracy = move.get("displayAccuracy")
    pp = f"{move.get('pp', '?')}/{move.get('maxpp', '?')}"
    suffix = " DIS" if move.get("disabled") else ""
    accuracy_text = f" {accuracy}" if accuracy else ""
    return f"{index} {move_name}/{move_type} {pp}{accuracy_text}{suffix}"


def compact_team_popup_line(index: int, pokemon: dict[str, Any]) -> str:
    prefix = "*" if pokemon.get("active") else ""
    details = str(pokemon.get("details", pokemon.get("ident", f"Pokemon {index}")))
    parsed = parse_condition(str(pokemon.get("condition", "")))
    hp = "FNT" if parsed["fainted"] else (f"{parsed['percent']}%" if parsed["percent"] is not None else parsed["hp_text"])
    status = f" {parsed['status']}" if parsed["status"] else ""
    return compact_text(f"{index}{prefix} {details_name(details)} {hp}{status}", limit=28)


def packed_team_member_count(packed_team: str | None) -> int:
    text = str(packed_team or "").strip()
    if not text:
        return 0
    return sum(1 for member in text.split("]") if str(member).strip())


def packed_team_member_labels(packed_team: str | None) -> list[str]:
    def normalized(value: str) -> str:
        return "".join(char for char in str(value or "").lower() if char.isalnum())

    labels: list[str] = []
    text = str(packed_team or "").strip()
    if not text:
        return labels
    for index, member in enumerate((part for part in text.split("]") if str(part).strip()), start=1):
        parts = member.split("|")
        name = str(parts[0] or "").strip() if len(parts) > 0 else ""
        species = str(parts[1] or "").strip() if len(parts) > 1 else ""
        label = species or name or f"Slot {index}"
        if name and species and normalized(name) != normalized(species):
            label = f"{name}/{species}"
        level = ""
        if len(parts) > 10:
            level = str(parts[10] or "").split(",", 1)[0].strip()
        if level:
            label = f"{label} Lv.{level}"
        labels.append(label)
    return labels


def short_slot(slot: str) -> str:
    return "1" if slot == "p1" else "2"


def format_stat_stage(stage: int) -> str:
    return f"{stage:+d}"


def bool_label(value: bool) -> str:
    return "On" if value else "Off"


class BattleService:
    def __init__(self, client) -> None:
        self.client = client
        self.encounter_service = None
        self.visual_renderer = BattleVisualRenderer()
        self.local_data_service = PokemonDataService()
        self.pending_by_id: dict[str, PendingChallenge] = {}
        self.battles_by_id: dict[str, BattleSession] = {}
        self.pending_by_user: dict[int, set[str]] = {}
        self.active_pvp_by_user: dict[int, str] = {}
        self.gym_team_cache: dict[str, str] = {}
        self.exit_cleanup_handlers: list[Any] = []
        self._recovery_write_lock = asyncio.Lock()

    def attach_encounter_service(self, encounter_service) -> None:
        self.encounter_service = encounter_service

    def register_exit_cleanup_handler(self, handler) -> None:
        self.exit_cleanup_handlers.append(handler)

    def normalize_challenge_mode(self, value: str | None) -> str:
        return "random" if str(value or "").strip().lower() == "random" else DEFAULT_CHALLENGE_MODE

    def normalize_challenge_generation(self, value: int | str | None) -> int:
        try:
            generation = int(value or DEFAULT_CHALLENGE_GENERATION)
        except (TypeError, ValueError):
            return DEFAULT_CHALLENGE_GENERATION
        if generation in RANDOM_BATTLE_FORMATS:
            return generation
        return DEFAULT_CHALLENGE_GENERATION

    def challenge_preferences_for_trainer(self, trainer) -> tuple[str, int, bool]:
        return (
            self.normalize_challenge_mode(getattr(trainer, "challenge_mode", DEFAULT_CHALLENGE_MODE)),
            self.normalize_challenge_generation(getattr(trainer, "challenge_generation", DEFAULT_CHALLENGE_GENERATION)),
            bool(getattr(trainer, "battle_visuals", False)),
        )

    def resolve_challenge_format(self, mode: str, generation: int) -> tuple[str, str]:
        if self.normalize_challenge_mode(mode) == "random":
            return RANDOM_BATTLE_FORMATS[self.normalize_challenge_generation(generation)]
        return FULL_GIMMICK_FORMAT_ID, FULL_GIMMICK_FORMAT_LABEL

    def challenge_mode_label(self, mode: str) -> str:
        return "Random Battle" if self.normalize_challenge_mode(mode) == "random" else "Owned Team Battle"

    def challenge_visuals_label(self, enabled: bool) -> str:
        label = bool_label(bool(enabled))
        if enabled and not self.visual_renderer.available:
            return f"{label} [renderer unavailable]"
        return label

    def challenge_status_label(self, challenge: PendingChallenge) -> str:
        return {
            "open": "waiting",
            "starting": "preparing",
            "expired": "expired",
            "declined": "declined",
            "cancelled": "cancelled",
        }.get(challenge.state, challenge.state)

    def update_challenge_format(self, challenge: PendingChallenge) -> None:
        challenge.mode = self.normalize_challenge_mode(challenge.mode)
        challenge.generation = self.normalize_challenge_generation(challenge.generation)
        challenge.format_id, challenge.format_label = self.resolve_challenge_format(challenge.mode, challenge.generation)

    def is_challenge_expired(self, challenge: PendingChallenge) -> bool:
        return challenge.state == "open" and challenge.expires_at > 0 and asyncio.get_running_loop().time() >= challenge.expires_at

    def challenge_settings_text(self, challenge: PendingChallenge, *, view: str = "root") -> str:
        current_format_id, current_format_label = self.resolve_challenge_format(challenge.mode, challenge.generation)
        lines = ["Challenge settings", ""]
        lines.append(f"Main mode: {self.challenge_mode_label(challenge.mode)}")
        lines.append(f"Generation: Gen {challenge.generation}")
        lines.append(f"Visuals: {self.challenge_visuals_label(challenge.visuals_enabled)}")
        lines.append(f"Format preview: {current_format_label}")
        if challenge.mode != "random":
            lines.append("Generation changes Random Battle only.")
        lines.append("")
        if view == "mode":
            lines.append("Choose the main mode.")
        elif view == "generation":
            lines.append("Choose the generation for Random Battle mode.")
        elif view == "visuals":
            lines.append("Choose whether battles should render a pixel battle scene.")
            if not self.visual_renderer.available:
                lines.append("The renderer will activate after Pillow is installed on this host.")
        else:
            lines.append("Use Save to make these settings permanent.")
        return "\n".join(lines)

    def challenge_settings_buttons(self, challenge: PendingChallenge, *, view: str = "root") -> list[list[Button]]:
        challenge_id = challenge.challenge_id
        if view == "mode":
            rows = [
                [
                    Button.inline(
                        ("Owned Team" + (" *" if challenge.mode == "owned" else "")),
                        data=f"challenge:{challenge_id}:settings:setmode:owned".encode("utf-8"),
                    ),
                    Button.inline(
                        ("Random Battle" + (" *" if challenge.mode == "random" else "")),
                        data=f"challenge:{challenge_id}:settings:setmode:random".encode("utf-8"),
                    ),
                ],
                [Button.inline("Back", data=f"challenge:{challenge_id}:settings".encode("utf-8"))],
            ]
            return rows

        if view == "generation":
            buttons = [
                Button.inline(
                    (f"{generation}" + (" *" if challenge.generation == generation else "")),
                    data=f"challenge:{challenge_id}:settings:setgen:{generation}".encode("utf-8"),
                )
                for generation in SUPPORTED_CHALLENGE_GENERATIONS
            ]
            return [
                buttons[0:5],
                buttons[5:9],
                [Button.inline("Back", data=f"challenge:{challenge_id}:settings".encode("utf-8"))],
            ]

        if view == "visuals":
            return [
                [
                    Button.inline(
                        ("On" + (" *" if challenge.visuals_enabled else "")),
                        data=f"challenge:{challenge_id}:settings:setvisuals:on".encode("utf-8"),
                    ),
                    Button.inline(
                        ("Off" + (" *" if not challenge.visuals_enabled else "")),
                        data=f"challenge:{challenge_id}:settings:setvisuals:off".encode("utf-8"),
                    ),
                ],
                [Button.inline("Back", data=f"challenge:{challenge_id}:settings".encode("utf-8"))],
            ]

        return [
            [
                Button.inline("Main Modes", data=f"challenge:{challenge_id}:settings:mode".encode("utf-8")),
                Button.inline("Generation", data=f"challenge:{challenge_id}:settings:generation".encode("utf-8")),
            ],
            [Button.inline("Visuals", data=f"challenge:{challenge_id}:settings:visuals".encode("utf-8"))],
            [Button.inline("Reset To Default", data=f"challenge:{challenge_id}:settings:reset".encode("utf-8"))],
            [
                Button.inline("Save", data=f"challenge:{challenge_id}:settings:save".encode("utf-8")),
                Button.inline("Back", data=f"challenge:{challenge_id}:settings:back".encode("utf-8")),
            ],
        ]

    def pvp_lock_reason(self, user_id: int) -> str | None:
        if user_id in self.active_pvp_by_user:
            return "You are already in another PvP battle."
        if self.pending_by_user.get(user_id):
            return "You already have a pending PvP challenge."
        return None

    def encounter_lock_reason(self, user_id: int) -> str | None:
        if self.encounter_service is None:
            return None
        encounter = self.encounter_service.active_by_user.get(user_id)
        if encounter is not None and encounter.battle_id:
            return "Finish your current encounter battle first."
        return None

    def active_battle_for_user(self, user_id: int) -> BattleSession | None:
        battle_id = self.active_pvp_by_user.get(user_id)
        if battle_id:
            battle = self.battles_by_id.get(battle_id)
            if battle is not None:
                return battle
        if self.encounter_service is not None:
            encounter = self.encounter_service.active_by_user.get(user_id)
            if encounter is not None and encounter.battle_id:
                battle = self.battles_by_id.get(encounter.battle_id)
                if battle is not None:
                    return battle
        return None

    def can_start_hunt(self, user_id: int) -> tuple[bool, str | None]:
        reason = self.pvp_lock_reason(user_id)
        if reason is None:
            return True, None
        return False, "Finish your pending or active PvP battle before hunting."

    def _reserve_pending_user(self, user_id: int, challenge_id: str) -> None:
        self.pending_by_user.setdefault(user_id, set()).add(challenge_id)

    def _release_pending_user(self, user_id: int, challenge_id: str) -> None:
        pending = self.pending_by_user.get(user_id)
        if not pending:
            return
        pending.discard(challenge_id)
        if not pending:
            self.pending_by_user.pop(user_id, None)

    def _register_pending_challenge(self, challenge: PendingChallenge) -> None:
        if challenge.expires_at <= 0:
            challenge.expires_at = asyncio.get_running_loop().time() + CHALLENGE_EXPIRY_SECONDS
        self.pending_by_id[challenge.challenge_id] = challenge
        self._reserve_pending_user(challenge.challenger_id, challenge.challenge_id)
        if challenge.targeted and challenge.opponent_id is not None:
            self._reserve_pending_user(challenge.opponent_id, challenge.challenge_id)
        challenge.expiry_task = asyncio.create_task(self._expire_pending_challenge_later(challenge.challenge_id))

    def _release_pending_challenge(self, challenge: PendingChallenge, *, cancel_expiry_task: bool = True) -> None:
        self.pending_by_id.pop(challenge.challenge_id, None)
        self._release_pending_user(challenge.challenger_id, challenge.challenge_id)
        if challenge.opponent_id is not None:
            self._release_pending_user(challenge.opponent_id, challenge.challenge_id)
        if cancel_expiry_task and challenge.expiry_task is not None and not challenge.expiry_task.done():
            challenge.expiry_task.cancel()
        challenge.expiry_task = None

    def _register_active_pvp_battle(self, battle: BattleSession) -> None:
        if battle.battle_mode not in {"pvp", "gym"}:
            return
        for player in battle.players.values():
            if int(player.user_id or 0) > 0:
                self.active_pvp_by_user[player.user_id] = battle.battle_id

    def _release_active_pvp_battle(self, battle: BattleSession) -> None:
        if battle.battle_mode not in {"pvp", "gym"}:
            return
        for player in battle.players.values():
            if int(player.user_id or 0) > 0 and self.active_pvp_by_user.get(player.user_id) == battle.battle_id:
                self.active_pvp_by_user.pop(player.user_id, None)

    def _clear_battle_runtime_state(self, battle: BattleSession) -> None:
        for player in battle.players.values():
            player.current_request = None
            player.locked_choice = None
            player.last_error = None
            player.primed_action = None
            player.next_action_at = 0.0
        battle.last_render_fingerprint = ""
        battle.last_visual_scene_fingerprint = ""
        battle.render_requested = False
        battle.public_render_task = None
        battle.last_public_edit_at = 0.0
        battle.visual_message_id = None

    def _battle_recovery_payload(self, battle: BattleSession) -> dict[str, Any] | None:
        if battle.finished or battle.battle_mode not in {"pvp", "gym"}:
            return None
        p1_team = str(battle.metadata.get("p1_team") or "").strip()
        p2_team = str(battle.metadata.get("p2_team") or "").strip()
        if not p1_team or not p2_team:
            return None
        return {
            "battle_id": battle.battle_id,
            "chat_id": int(battle.chat_id),
            "public_message_id": int(battle.public_message_id),
            "format_id": battle.format_id,
            "format_label": battle.format_label,
            "battle_mode": battle.battle_mode,
            "players": {
                slot: {
                    "slot": player.slot,
                    "user_id": int(player.user_id or 0),
                    "name": player.name,
                }
                for slot, player in battle.players.items()
            },
            "metadata": dict(battle.metadata),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _persist_recoverable_battles(self) -> None:
        payload = []
        for battle in self.battles_by_id.values():
            item = self._battle_recovery_payload(battle)
            if item is not None:
                payload.append(item)
        async with self._recovery_write_lock:
            await asyncio.to_thread(
                RECOVERABLE_BATTLES_PATH.write_text,
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )

    async def _schedule_recovery_persist(self) -> None:
        try:
            await self._persist_recoverable_battles()
        except Exception:
            logger.exception("Failed to persist recoverable battles.")

    def _choice_uses_primary_gimmick(self, choice: str) -> str | None:
        parts = str(choice or "").split()
        if len(parts) < 3:
            return None
        action_name = str(parts[2] or "").strip().lower()
        return action_name if action_name in PRIMARY_GIMMICK_ACTIONS else None

    async def restore_recent_battles(self) -> None:
        if not RECOVERABLE_BATTLES_PATH.exists():
            return
        try:
            raw = await asyncio.to_thread(RECOVERABLE_BATTLES_PATH.read_text, "utf-8")
            loaded = json.loads(raw or "[]")
        except Exception:
            logger.exception("Failed to load recoverable battles.")
            return
        if not isinstance(loaded, list):
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECOVERABLE_BATTLE_WINDOW_SECONDS)
        for item in loaded:
            if not isinstance(item, dict):
                continue
            try:
                updated_at = datetime.fromisoformat(str(item.get("updated_at") or ""))
            except Exception:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at < cutoff:
                continue
            if str(item.get("battle_mode") or "") not in {"pvp", "gym"}:
                continue
            players_payload = item.get("players") or {}
            p1_payload = players_payload.get("p1") or {}
            p2_payload = players_payload.get("p2") or {}
            battle = BattleSession(
                battle_id=str(item.get("battle_id") or ""),
                chat_id=int(item.get("chat_id") or 0),
                public_message_id=int(item.get("public_message_id") or 0),
                format_id=str(item.get("format_id") or FULL_GIMMICK_FORMAT_ID),
                format_label=str(item.get("format_label") or FULL_GIMMICK_FORMAT_LABEL),
                players={
                    "p1": PlayerState(slot="p1", user_id=int(p1_payload.get("user_id") or 0), name=str(p1_payload.get("name") or "Trainer")),
                    "p2": PlayerState(slot="p2", user_id=int(p2_payload.get("user_id") or 0), name=str(p2_payload.get("name") or "Trainer")),
                },
                public_view=PublicBattleView({"p1": str(p1_payload.get("name") or "Trainer"), "p2": str(p2_payload.get("name") or "Trainer")}),
                battle_mode=str(item.get("battle_mode") or "pvp"),
                metadata=dict(item.get("metadata") or {}),
            )
            battle.metadata["recovering"] = True
            battle.metadata["replay_index"] = 0
            self._register_active_pvp_battle(battle)
            try:
                await self._start_battle_session(
                    battle,
                    p1_team=str(battle.metadata.get("p1_team") or ""),
                    p2_team=str(battle.metadata.get("p2_team") or ""),
                    failure_chat_id=battle.chat_id,
                    failure_message_id=battle.public_message_id,
                )
            except Exception:
                self._release_active_pvp_battle(battle)
                logger.exception("Failed to recover battle %s", battle.battle_id)
        await self._schedule_recovery_persist()

    async def _expire_pending_challenge_later(self, challenge_id: str) -> None:
        challenge = self.pending_by_id.get(challenge_id)
        if challenge is None:
            return
        delay = max(0.0, challenge.expires_at - asyncio.get_running_loop().time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        challenge = self.pending_by_id.get(challenge_id)
        if challenge is None or challenge.state != "open":
            return
        challenge.state = "expired"
        self._release_pending_challenge(challenge, cancel_expiry_task=False)
        await self._edit_message(
            challenge.chat_id,
            challenge.public_message_id,
            self.challenge_text(challenge),
            buttons=None,
            parse_mode="html",
        )

    async def on_exit_command(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        cleared = await self.clear_user_state(event.sender_id, actor_name=display_name(sender))
        if cleared:
            await event.respond("\n".join(cleared))
            return
        await event.respond("No active battle state was found for you.")

    async def on_battle_stats_command(self, event: NewMessage.Event) -> None:
        battle = self.active_battle_for_user(event.sender_id)
        if battle is None:
            await event.respond("No active battle found. Use /battle_stats during a battle.")
            return

        player = battle.player_for_user(event.sender_id)
        if player is None:
            await event.respond("Only the active battler can use /battle_stats here.")
            return

        bridge = battle.bridge
        if bridge is None:
            await event.respond("The battle is still starting. Try /battle_stats again in a moment.")
            return

        try:
            snapshot = await bridge.active_stats(player.slot)
        except ShowdownBridgeError as exc:
            await event.respond(f"Could not load active battle stats.\n{compact_text(str(exc), 300)}")
            return

        await event.respond(self.battle_stats_text(snapshot), parse_mode="md", link_preview=False)

    def parse_test_battle_image_species(self, raw_text: str) -> tuple[str, str]:
        text = str(raw_text or "").strip()
        if not text:
            return DEFAULT_PREVIEW_PLAYER, DEFAULT_PREVIEW_OPPONENT
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return DEFAULT_PREVIEW_PLAYER, DEFAULT_PREVIEW_OPPONENT
        query = parts[1].strip()
        for separator in ("|", " vs ", ","):
            if separator in query:
                left, right = query.split(separator, 1)
                first = left.strip() or DEFAULT_PREVIEW_PLAYER
                second = right.strip() or DEFAULT_PREVIEW_OPPONENT
                return first, second
        return query, DEFAULT_PREVIEW_OPPONENT

    async def on_test_battle_image_command(self, event: NewMessage.Event) -> None:
        if not self.visual_renderer.available:
            await event.respond("Battle visuals are unavailable. Install Pillow first.")
            return

        player_species, opponent_species = self.parse_test_battle_image_species(event.raw_text)
        payload = await asyncio.to_thread(
            self.visual_renderer.render_preview,
            player_species=player_species,
            opponent_species=opponent_species,
            highlight_slot="p1",
        )
        if payload is None:
            await event.respond("Could not build a test battle image right now.")
            return

        file_obj, _ = payload
        await event.respond(
            (
                "Battle visuals test\n"
                f"Player: {player_species}\n"
                f"Opponent: {opponent_species}\n"
                "Edit bot/battle/visuals.py and rerun /trstbimage to fine-tune placement."
            ),
            file=file_obj,
        )

    def gym_region_text(self) -> str:
        return "Gym Challenge\nChoose a region."

    def gym_region_buttons(self, owner_id: int) -> list[list[Button]]:
        return [
            [Button.inline(label, data=f"gym:{owner_id}:region:{region_id}".encode("utf-8"))]
            for region_id, label in GYM_REGIONS
        ]

    def gym_picker_text(self, region_id: str) -> str:
        region_label = next((label for key, label in GYM_REGIONS if key == region_id), region_id.title())
        return f"Gym Challenge\nRegion: {region_label}\nChoose a gym."

    def gym_picker_buttons(self, owner_id: int, region_id: str) -> list[list[Button]]:
        rows = [
            [Button.inline(gym_label, data=f"gym:{owner_id}:leader:{region_id}:{gym_id}".encode("utf-8"))]
            for gym_id, gym_label, _leader in GYMS_BY_REGION.get(region_id, ())
        ]
        rows.append([Button.inline("Back", data=f"gym:{owner_id}:home".encode("utf-8"))])
        return rows

    def gym_preview_caption(self, leader_name: str, gym_label: str) -> str:
        return f"You are about to challenge {leader_name} at {gym_label}.\n\n{gym_requirement_text()}"

    def gym_validation_text(self, leader_name: str, reasons: list[str]) -> str:
        lines = [f"{leader_name} will not accept this challenge yet.", ""]
        lines.extend(f"- {reason}" for reason in reasons)
        lines.extend(["", gym_requirement_text()])
        return "\n".join(lines)

    async def build_brock_packed_team(self) -> str:
        cached = self.gym_team_cache.get("brock")
        if cached:
            return cached

        results = await asyncio.gather(
            *(
                run_dex_tool(
                    bot_dir=BOT_DIR,
                    showdown_dir=SHOWDOWN_DIR,
                    payload={
                        "type": "generate-pokemon",
                        "species": spec["species"],
                        "level": 100,
                        "friendship": 255,
                        "allowHiddenAbility": True,
                        "legalMinEvs": False,
                        "formatid": FULL_GIMMICK_FORMAT_ID,
                        "mod": "gen9",
                        "item": spec["item"],
                        "ivs": dict(spec["ivs"]),
                        "evs": dict(spec["evs"]),
                        "moves": list(spec["moves"]),
                        "nature": spec["nature"],
                        "ability": spec["ability"],
                    },
                )
                for spec in BROCK_TEAM_SPECS
            )
        )
        packed = "]".join(str(result.get("packedTeam") or "").strip() for result in results if str(result.get("packedTeam") or "").strip())
        if not packed:
            raise ShowdownBridgeError("Could not build Brock's team.")
        self.gym_team_cache["brock"] = packed
        return packed

    async def on_gym_command(self, event: NewMessage.Event) -> None:
        await event.respond(GYM_COMMAND_LOCK_MESSAGE)

    async def handle_gym_callback(self, event: CallbackQuery.Event, data: str) -> None:
        await event.answer(GYM_COMMAND_LOCK_MESSAGE, alert=True)
        return
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Invalid gym button.", alert=True)
            return
        try:
            owner_id = int(parts[1])
        except ValueError:
            await event.answer("Invalid gym owner.", alert=True)
            return
        if event.sender_id != owner_id:
            await event.answer("This gym panel belongs to another trainer.", alert=True)
            return

        action = parts[2]
        chat_id = event.chat_id
        if chat_id is None:
            await event.answer("Chat unavailable.", alert=True)
            return

        if action == "home":
            await self._edit_message(chat_id, event.message_id, self.gym_region_text(), buttons=self.gym_region_buttons(owner_id))
            await event.answer()
            return

        if action == "region" and len(parts) >= 4:
            region_id = parts[3]
            if region_id not in GYMS_BY_REGION:
                await event.answer("That region is not ready yet.", alert=True)
                return
            await self._edit_message(
                chat_id,
                event.message_id,
                self.gym_picker_text(region_id),
                buttons=self.gym_picker_buttons(owner_id, region_id),
            )
            await event.answer()
            return

        if action == "leader" and len(parts) >= 5:
            region_id = parts[3]
            gym_id = parts[4]
            gym_entry = next((entry for entry in GYMS_BY_REGION.get(region_id, ()) if entry[0] == gym_id), None)
            if gym_entry is None:
                await event.answer("That gym is not ready yet.", alert=True)
                return
            _selected_gym_id, gym_label, leader_name = gym_entry
            preview_buttons = [[Button.inline("Challenge", data=f"gym:{owner_id}:challenge:{region_id}:{gym_id}".encode("utf-8"))]]
            caption = self.gym_preview_caption(leader_name, gym_label)

            try:
                if BROCK_IMAGE_PATH.exists():
                    await self.client.send_file(
                        chat_id,
                        str(BROCK_IMAGE_PATH),
                        caption=caption,
                        buttons=preview_buttons,
                        reply_to=event.message_id,
                        force_document=False,
                    )
                else:
                    await self.client.send_message(chat_id, caption, buttons=preview_buttons, reply_to=event.message_id, link_preview=False)
            except Exception:
                await event.answer("Could not open that gym preview right now.", alert=True)
                return

            await self._edit_message(
                chat_id,
                event.message_id,
                f"{gym_label}\nChallenge panel sent below.",
                buttons=None,
            )
            await event.answer()
            return

        if action == "challenge" and len(parts) >= 5:
            await event.answer("Preparing battle...")
            await self.start_brock_gym_battle(event, region_id=parts[3], gym_id=parts[4], owner_id=owner_id)
            return

        await event.answer("Unknown gym action.", alert=True)

    async def start_brock_gym_battle(self, event: CallbackQuery.Event, *, region_id: str, gym_id: str, owner_id: int) -> None:
        gym_entry = next((entry for entry in GYMS_BY_REGION.get(region_id, ()) if entry[0] == gym_id), None)
        if gym_entry is None:
            await event.answer("That gym is not ready yet.", alert=True)
            return
        _selected_gym_id, gym_label, leader_name = gym_entry

        challenger_lock_reason = self.pvp_lock_reason(owner_id)
        if challenger_lock_reason:
            await self.client.send_message(event.chat_id, challenger_lock_reason, reply_to=event.message_id, link_preview=False)
            return
        challenger_encounter_reason = self.encounter_lock_reason(owner_id)
        if challenger_encounter_reason:
            await self.client.send_message(event.chat_id, challenger_encounter_reason, reply_to=event.message_id, link_preview=False)
            return

        sender = await event.get_sender()
        challenger_name = display_name(sender)
        data_service = self._pokemon_data_service()

        prep = await run_db_work_async(lambda session: self._prepare_gym_battle(
            session,
            owner_id=owner_id,
            username=getattr(sender, "username", None),
            challenger_name=challenger_name,
            leader_name=leader_name,
            data_service=data_service,
        ))
        challenger_name = prep["challenger_name"]
        if prep["status"] == "invalid_team":
            await self.client.send_message(
                event.chat_id,
                self.gym_validation_text(leader_name, prep["reasons"]),
                reply_to=event.message_id,
                link_preview=False,
            )
            return
        if prep["status"] == "empty_team":
            await self.client.send_message(
                event.chat_id,
                "Your active team is empty. Set it up in /myteam first.",
                reply_to=event.message_id,
                link_preview=False,
            )
            return
        p1_team = prep["p1_team"]
        visuals_enabled = bool(prep["visuals_enabled"])

        await self._edit_message(
            event.chat_id,
            event.message_id,
            f"{leader_name} is preparing the {gym_label} challenge...",
            buttons=None,
        )

        try:
            p2_team = await self.build_brock_packed_team()
        except Exception as exc:
            await self._edit_message(
                event.chat_id,
                event.message_id,
                f"Could not prepare {leader_name}'s team.\n{compact_text(str(exc), 400)}",
                buttons=None,
            )
            return
        battle = BattleSession(
            battle_id=f"gym-{owner_id}-{secrets.token_hex(4)}",
            chat_id=event.chat_id,
            public_message_id=event.message_id,
            format_id=FULL_GIMMICK_FORMAT_ID,
            format_label=FULL_GIMMICK_FORMAT_LABEL,
            players={
                "p1": PlayerState(slot="p1", user_id=owner_id, name=challenger_name),
                "p2": PlayerState(slot="p2", user_id=0, name=leader_name),
            },
            public_view=PublicBattleView({"p1": challenger_name, "p2": leader_name}),
            battle_mode="gym",
            metadata={
                "visuals_enabled": visuals_enabled,
                "gym_region": region_id,
                "gym_id": gym_id,
                "gym_label": gym_label,
                "gym_leader": leader_name,
                "gym_ai": "brock",
            },
        )
        self._register_active_pvp_battle(battle)
        try:
            await self._start_battle_session(
                battle,
                p1_team=p1_team,
                p2_team=p2_team,
                failure_chat_id=event.chat_id,
                failure_message_id=event.message_id,
            )
        except Exception:
            self._release_active_pvp_battle(battle)
            return

    async def clear_user_state(self, user_id: int, *, actor_name: str) -> list[str]:
        messages: list[str] = []

        if self.encounter_service is not None:
            encounter_message = await self.encounter_service.exit_user_state(user_id, actor_name=actor_name)
            if encounter_message:
                messages.append(encounter_message)

        related_challenges = [
            challenge
            for challenge in list(self.pending_by_id.values())
            if challenge.challenger_id == user_id or challenge.opponent_id == user_id
        ]
        for challenge in related_challenges:
            await self._edit_message(
                challenge.chat_id,
                challenge.public_message_id,
                f"Challenge cancelled because {actor_name} used /exit.",
                buttons=None,
            )
            self._release_pending_challenge(challenge)
            messages.append("Cancelled your pending PvP challenge.")

        battle_id = self.active_pvp_by_user.get(user_id)
        if battle_id is not None:
            battle = self.battles_by_id.get(battle_id)
            if battle is None:
                self.active_pvp_by_user.pop(user_id, None)
                messages.append("Cleared a stale PvP battle reservation.")
            else:
                exiting_player = battle.player_for_user(user_id)
                leaver_name = exiting_player.name if exiting_player is not None else actor_name
                await self.cancel_public_render(battle)
                async with battle.lock:
                    battle.finished = True
                    battle.metadata["forced_exit"] = True
                    await self._edit_message(
                        battle.chat_id,
                        battle.public_message_id,
                        f"Battle closed because {leaver_name} used /exit.",
                        buttons=None,
                    )
                    self.battles_by_id.pop(battle.battle_id, None)
                    self._release_active_pvp_battle(battle)
                    if battle.bridge is not None:
                        await battle.bridge.close()
                messages.append("Closed your active gym battle." if battle.battle_mode == "gym" else "Closed your active PvP battle.")

        for handler in self.exit_cleanup_handlers:
            result = await handler(user_id, actor_name=actor_name)
            if not result:
                continue
            if isinstance(result, str):
                messages.append(result)
                continue
            messages.extend(str(item) for item in result if str(item).strip())

        return messages

    def _battle_data_service(self):
        return getattr(self.encounter_service, "data", None)

    def _pokemon_data_service(self) -> PokemonDataService:
        return self._battle_data_service() or self.local_data_service

    def _prepare_gym_battle(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        challenger_name: str,
        leader_name: str,
        data_service: PokemonDataService,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=challenger_name,
        )
        resolved_name = trainer.display_name or challenger_name
        active_team = teams.get_active_team(trainer)
        team_slots = teams.team_slots(active_team)
        members = [slot.pokemon for slot in team_slots if slot.pokemon is not None]
        reasons = validate_gym_challenger_team(members, data_service)
        if reasons:
            return {
                "status": "invalid_team",
                "challenger_name": resolved_name,
                "reasons": reasons,
            }

        p1_team = teams.build_packed_team(active_team)
        if not p1_team:
            return {
                "status": "empty_team",
                "challenger_name": resolved_name,
            }
        return {
            "status": "ok",
            "challenger_name": resolved_name,
            "p1_team": p1_team,
            "visuals_enabled": bool(getattr(trainer, "battle_visuals", False)),
        }

    def _challenge_preferences_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        challenger_name: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=challenger_name,
        )
        mode, generation, visuals_enabled = self.challenge_preferences_for_trainer(trainer)
        if mode == "owned":
            active_team = teams.get_active_team(trainer)
            if not teams.build_packed_team(active_team):
                return {"status": "empty_team"}
        return {
            "status": "ok",
            "mode": mode,
            "generation": generation,
            "visuals_enabled": visuals_enabled,
        }

    def _save_challenge_preferences(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        challenge_mode: str,
        challenge_generation: int,
        battle_visuals: bool,
    ) -> None:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        trainers.set_preferences(
            trainer,
            challenge_mode=challenge_mode,
            challenge_generation=challenge_generation,
            battle_visuals=battle_visuals,
        )

    def _load_owned_challenge_teams(self, session, challenge: PendingChallenge) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        challenger = trainers.get_by_telegram_user_id(challenge.challenger_id)
        opponent = trainers.get_by_telegram_user_id(challenge.opponent_id or 0)
        if challenger is None or opponent is None:
            return {"status": "missing_trainer"}
        p1_team = teams.build_packed_team(teams.get_active_team(challenger))
        p2_team = teams.build_packed_team(teams.get_active_team(opponent))
        if not p1_team or not p2_team:
            return {"status": "empty_team"}
        return {"status": "ok", "p1_team": p1_team, "p2_team": p2_team}

    def _wild_battle_visuals_enabled(self, session, trainer_user_id: int) -> bool:
        trainers = TrainerRepository(session)
        trainer = trainers.get_by_telegram_user_id(trainer_user_id)
        return bool(getattr(trainer, "battle_visuals", False)) if trainer is not None else False

    async def auto_choose_gym_request(self, battle: BattleSession, slot: str, request: dict[str, Any]) -> None:
        if battle.battle_mode != "gym" or slot != "p2" or battle.bridge is None or request.get("wait"):
            return
        player = battle.players["p2"]
        if player.locked_choice:
            return
        choice = choose_brock_action(battle, request, self._pokemon_data_service())
        if not choice:
            return
        player.locked_choice = self.describe_choice(request, choice)
        player.last_error = None
        player.primed_action = None
        await battle.bridge.choose("p2", choice)

    def extract_mega_notifications(self, lines: list[str]) -> list[tuple[str, str, str]]:
        notifications: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in lines:
            command, args = protocol_parts(line)
            if command not in {"detailschange", "-formechange"} or len(args) < 2:
                continue
            slot = ident_side(args[0])
            base_name = ident_name(args[0])
            new_species = details_name(args[1])
            if "Mega" not in new_species:
                continue
            key = (slot, new_species)
            if key in seen:
                continue
            seen.add(key)
            notifications.append((slot, base_name, new_species))
        return notifications

    async def send_mega_notifications(self, battle: BattleSession, notifications: list[tuple[str, str, str]]) -> None:
        data_service = self._battle_data_service()
        if data_service is None:
            return
        sent_keys = battle.metadata.setdefault("mega_notice_keys", set())
        for slot, base_name, new_species in notifications:
            notice_key = f"{slot}:{new_species}"
            if notice_key in sent_keys:
                continue
            sent_keys.add(notice_key)
            caption = f"{base_name} has mega evolved into {mega_caption_name(new_species)}"
            sent = False
            for candidate in data_service.artwork_candidates(new_species):
                try:
                    await self.client.send_file(
                        battle.chat_id,
                        candidate,
                        caption=caption,
                        reply_to=battle.public_message_id,
                        force_document=False,
                    )
                    sent = True
                    break
                except Exception:
                    continue
            if not sent:
                try:
                    await self.client.send_message(
                        battle.chat_id,
                        caption,
                        reply_to=battle.public_message_id,
                        link_preview=False,
                    )
                except Exception:
                    continue

    def extract_zmove_notifications(self, lines: list[str]) -> list[tuple[str, str, str]]:
        notifications: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in lines:
            command, args = protocol_parts(line)
            # Showdown announces Z-moves like: |move|p1a: Pikachu|Catastropika|p2a: Charizard|[zeffect]
            if command == "move" and len(args) >= 2:
                slot = ident_side(args[0])
                attacker = ident_name(args[0])
                move_name = args[1]
                normalized_move = move_name.lower().strip()
                
                if normalized_move in SIGNATURE_ZMOVES:
                    key = (slot, normalized_move)
                    if key in seen:
                        continue
                    seen.add(key)
                    notifications.append((slot, attacker, move_name))
        return notifications

    async def send_zmove_notifications(self, battle: BattleSession, notifications: list[tuple[str, str, str]]) -> None:
        sent_keys = battle.metadata.setdefault("zmove_notice_keys", set())
        for slot, attacker, move_name in notifications:
            normalized_move = move_name.lower().strip()
            notice_key = f"{slot}:{normalized_move}"
            
            if notice_key in sent_keys:
                continue
            sent_keys.add(notice_key)
            
            filename = SIGNATURE_ZMOVES.get(normalized_move)
            if not filename:
                continue
                
            filepath = PROJECT_DIR / "assets" / "zmoves" / filename
            caption = f"✨ **{attacker}** unleashed its full Z-Power to use **{move_name}**!"
            
            try:
                if filepath.exists():
                    await self.client.send_file(
                        battle.chat_id,
                        str(filepath),
                        caption=caption,
                        reply_to=battle.public_message_id,
                        force_document=False,
                    )
                else:
                    await self.client.send_message(
                        battle.chat_id,
                        caption,
                        reply_to=battle.public_message_id,
                        link_preview=False,
                    )
            except Exception:
                continue

    async def on_challenge_command(self, event: NewMessage.Event) -> None:
        if event.is_private:
            await event.respond("Use /challenge in a group chat.")
            return

        if not event.is_reply:
            await event.respond("Reply to another player's message with /challenge.")
            return
        reply_message = await event.get_reply_message()
        if reply_message is None:
            await event.respond("Reply to another player's message with /challenge.")
            return

        target_user = await reply_message.get_sender()
        if not isinstance(target_user, User) or getattr(target_user, "bot", False):
            await event.respond("Battle challenges must target another user, not a bot.")
            return
        if target_user.id == event.sender_id:
            await event.respond("You cannot challenge yourself.")
            return

        sender = await event.get_sender()
        challenger_name = display_name(sender)
        chat_id = event.chat_id
        assert chat_id is not None

        challenger_lock_reason = self.pvp_lock_reason(event.sender_id)
        if challenger_lock_reason:
            await event.respond(challenger_lock_reason)
            return
        challenger_encounter_reason = self.encounter_lock_reason(event.sender_id)
        if challenger_encounter_reason:
            await event.respond(challenger_encounter_reason)
            return

        target_lock_reason = self.pvp_lock_reason(target_user.id)
        if target_lock_reason:
            await event.respond("That trainer is already busy with another PvP battle.")
            return
        target_encounter_reason = self.encounter_lock_reason(target_user.id)
        if target_encounter_reason:
            await event.respond("That trainer is already in an encounter battle.")
            return

        preferences = await run_db_work_async(lambda session: self._challenge_preferences_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            challenger_name=challenger_name,
        ))
        if preferences["status"] == "empty_team":
            await event.respond("Your active team is empty. Set it up in /myteam first.")
            return
        mode = preferences["mode"]
        generation = preferences["generation"]
        visuals_enabled = preferences["visuals_enabled"]

        challenge = PendingChallenge(
            challenge_id=secrets.token_hex(4),
            chat_id=chat_id,
            public_message_id=0,
            challenger_id=event.sender_id,
            challenger_name=challenger_name,
            challenger_username=getattr(sender, "username", None),
            mode=mode,
            generation=generation,
            visuals_enabled=visuals_enabled,
            opponent_id=target_user.id,
            opponent_name=display_name(target_user),
            opponent_username=getattr(target_user, "username", None),
            targeted=True,
        )
        self.update_challenge_format(challenge)

        message = await event.respond(
            self.challenge_text(challenge),
            buttons=self.challenge_buttons(challenge),
            parse_mode="html",
            link_preview=False,
        )
        challenge.public_message_id = message.id
        self._register_pending_challenge(challenge)

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        data = event.data.decode("utf-8")
        if data.startswith("gym:"):
            await self.handle_gym_callback(event, data)
            return True
        if data.startswith("challenge:"):
            await self.handle_challenge_callback(event, data)
            return True
        if data.startswith("bact:"):
            await self.handle_action_callback(event, data)
            return True
        return False

    def challenge_text(self, challenge: PendingChallenge) -> str:
        challenger_link = mention_html(challenge.challenger_id, challenge.challenger_name)
        opponent_name = challenge.opponent_name or "another trainer"
        opponent_link = mention_html(challenge.opponent_id, opponent_name)
        
        return (
            f"🎫 <b>BATTLE ISSUED</b>\n"
            f"<b>Challenger:</b> {challenger_link}\n"
            f"<b>Opponent:</b> {opponent_link}\n\n"
            f"<b>Match Settings:</b>\n"
            f"├ <b>Mode:</b> {html.escape(self.challenge_mode_label(challenge.mode))}\n"
            f"├ <b>Format:</b> {html.escape(challenge.format_label)}\n"
            f"└ <b>Visuals:</b> {html.escape(self.challenge_visuals_label(challenge.visuals_enabled))}\n\n"
            f"🟡 <i>Status: {html.escape(self.challenge_status_label(challenge))}</i>"
        )

    def challenge_buttons(self, challenge: PendingChallenge) -> list[list[Button]] | None:
        if challenge.state != "open":
            return None
        challenge_id = challenge.challenge_id
        return [
            [
                Button.inline("Accept", data=f"challenge:{challenge_id}:accept".encode("utf-8")),
                Button.inline("Decline", data=f"challenge:{challenge_id}:decline".encode("utf-8")),
            ],
            [Button.inline("Settings", data=f"challenge:{challenge_id}:settings".encode("utf-8"))],
        ]

    async def handle_challenge_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Invalid challenge button.", alert=True)
            return

        challenge = self.pending_by_id.get(parts[1])
        if not challenge:
            await event.answer("That challenge is no longer active.", alert=True)
            return

        if self.is_challenge_expired(challenge):
            challenge.state = "expired"
            self._release_pending_challenge(challenge)
            await self._edit_message(
                challenge.chat_id,
                challenge.public_message_id,
                self.challenge_text(challenge),
                buttons=None,
                parse_mode="html",
            )
            await event.answer("Challenge expired.", alert=True)
            return
        if challenge.state != "open":
            await event.answer(f"This challenge is already {self.challenge_status_label(challenge)}.", alert=True)
            return

        action = parts[2]
        if action == "decline":
            if event.sender_id == challenge.challenger_id:
                challenge.state = "cancelled"
                outcome = "Challenge cancelled."
                answer_text = "Cancelled."
            elif event.sender_id == challenge.opponent_id:
                challenge.state = "declined"
                outcome = self.challenge_text(challenge)
                answer_text = "Declined."
            else:
                await event.answer("Only the invited trainer can decline this challenge.", alert=True)
                return
            self._release_pending_challenge(challenge)
            await self._edit_message(
                challenge.chat_id,
                challenge.public_message_id,
                outcome if outcome == "Challenge cancelled." else self.challenge_text(challenge),
                buttons=None,
                parse_mode="html" if outcome != "Challenge cancelled." else None,
            )
            await event.answer(answer_text)
            return

        if action == "settings":
            if event.sender_id != challenge.challenger_id:
                await event.answer("Only the challenger can change settings.", alert=True)
                return
            if challenge.state != "open":
                await event.answer("That challenge is no longer editable.", alert=True)
                return
            view = "root"
            if len(parts) >= 4:
                subaction = parts[3]
                if subaction == "back":
                    await self._edit_message(
                        challenge.chat_id,
                        challenge.public_message_id,
                        self.challenge_text(challenge),
                        buttons=self.challenge_buttons(challenge),
                        parse_mode="html",
                    )
                    await event.answer()
                    return
                if subaction == "mode":
                    view = "mode"
                elif subaction == "generation":
                    view = "generation"
                elif subaction == "visuals":
                    view = "visuals"
                elif subaction == "setmode" and len(parts) == 5:
                    challenge.mode = self.normalize_challenge_mode(parts[4])
                    self.update_challenge_format(challenge)
                    view = "mode"
                elif subaction == "setgen" and len(parts) == 5:
                    challenge.generation = self.normalize_challenge_generation(parts[4])
                    self.update_challenge_format(challenge)
                    view = "generation"
                elif subaction == "setvisuals" and len(parts) == 5:
                    challenge.visuals_enabled = parts[4].strip().lower() == "on"
                    view = "visuals"
                elif subaction == "reset":
                    challenge.mode = DEFAULT_CHALLENGE_MODE
                    challenge.generation = DEFAULT_CHALLENGE_GENERATION
                    challenge.visuals_enabled = False
                    self.update_challenge_format(challenge)
                    view = "root"
                elif subaction == "save":
                    sender = await event.get_sender()
                    await run_db_work_async(lambda session: self._save_challenge_preferences(
                        session,
                        owner_id=int(event.sender_id or 0),
                        username=getattr(sender, "username", None),
                        display_name_value=display_name(sender),
                        challenge_mode=challenge.mode,
                        challenge_generation=challenge.generation,
                        battle_visuals=challenge.visuals_enabled,
                    ))
                    await self._edit_message(
                        challenge.chat_id,
                        challenge.public_message_id,
                        self.challenge_text(challenge),
                        buttons=self.challenge_buttons(challenge),
                        parse_mode="html",
                    )
                    await event.answer("Challenge settings saved.")
                    return
                else:
                    await event.answer("Unknown settings action.", alert=True)
                    return
            await self._edit_message(
                challenge.chat_id,
                challenge.public_message_id,
                self.challenge_settings_text(challenge, view=view),
                buttons=self.challenge_settings_buttons(challenge, view=view),
            )
            await event.answer()
            return

        if action != "accept":
            await event.answer("Unknown challenge action.", alert=True)
            return

        if event.sender_id == challenge.challenger_id:
            await event.answer("You cannot accept your own challenge.", alert=True)
            return
        if challenge.targeted and event.sender_id != challenge.opponent_id:
            await event.answer("This challenge is for someone else.", alert=True)
            return
        joiner_encounter_reason = self.encounter_lock_reason(event.sender_id)
        if joiner_encounter_reason:
            await event.answer(joiner_encounter_reason, alert=True)
            return

        challenge.state = "starting"
        await self._edit_message(
            challenge.chat_id,
            challenge.public_message_id,
            self.challenge_text(challenge),
            buttons=None,
            parse_mode="html",
        )
        await event.answer("Preparing battle...")
        await self.start_challenge(challenge)

    async def start_challenge(self, challenge: PendingChallenge) -> None:
        p1_team: str | None = None
        p2_team: str | None = None
        if challenge.mode == "owned":
            owned_team_payload = await run_db_work_async(
                lambda session: self._load_owned_challenge_teams(session, challenge)
            )
            if owned_team_payload["status"] == "missing_trainer":
                await self._edit_message(
                    challenge.chat_id,
                    challenge.public_message_id,
                    "Both trainers need an RPG profile before using owned-team battles.",
                    buttons=None,
                )
                self._release_pending_challenge(challenge)
                return
            if owned_team_payload["status"] == "empty_team":
                await self._edit_message(
                    challenge.chat_id,
                    challenge.public_message_id,
                    "Both trainers need a non-empty active team in /myteam.",
                    buttons=None,
                )
                self._release_pending_challenge(challenge)
                return
            p1_team = owned_team_payload["p1_team"]
            p2_team = owned_team_payload["p2_team"]

        battle = BattleSession(
            battle_id=challenge.challenge_id,
            chat_id=challenge.chat_id,
            public_message_id=challenge.public_message_id,
            format_id=challenge.format_id,
            format_label=challenge.format_label,
            players={
                "p1": PlayerState(slot="p1", user_id=challenge.challenger_id, name=challenge.challenger_name),
                "p2": PlayerState(slot="p2", user_id=challenge.opponent_id or 0, name=challenge.opponent_name or "Trainer"),
            },
            public_view=PublicBattleView({"p1": challenge.challenger_name, "p2": challenge.opponent_name or "Trainer"}),
            battle_mode="pvp",
            metadata={"challenge_mode": challenge.mode, "visuals_enabled": bool(challenge.visuals_enabled)},
        )
        self._register_active_pvp_battle(battle)
        self._release_pending_challenge(challenge)
        try:
            await self._start_battle_session(
                battle,
                p1_team=p1_team,
                p2_team=p2_team,
                failure_chat_id=challenge.chat_id,
                failure_message_id=challenge.public_message_id,
            )
        except Exception:
            self._release_active_pvp_battle(battle)
            return

    async def start_wild_encounter(self, *, encounter, packed_team: str, owned_team_ids: list[int]) -> BattleSession:
        visuals_enabled = await run_db_work_async(
            lambda session: self._wild_battle_visuals_enabled(session, encounter.trainer_user_id),
            read_only=True,
        )

        battle = BattleSession(
            battle_id=encounter.encounter_id,
            chat_id=encounter.trainer_user_id,
            public_message_id=encounter.message_id or 0,
            format_id=FULL_GIMMICK_FORMAT_ID,
            format_label=FULL_GIMMICK_FORMAT_LABEL,
            players={
                "p1": PlayerState(slot="p1", user_id=encounter.trainer_user_id, name=encounter.trainer_name),
                "p2": PlayerState(slot="p2", user_id=0, name=f"Wild {encounter.species}"),
            },
            public_view=PublicBattleView({"p1": encounter.trainer_name, "p2": f"Wild {encounter.species}"}),
            battle_mode="wild",
            metadata={
                "encounter_kind": "wild",
                "owner_user_id": encounter.trainer_user_id,
                "encounter_id": encounter.encounter_id,
                "encounter_source": encounter.source,
                "encounter_region": encounter.region,
                "encounter_note": "",
                "wild_species": encounter.species,
                "owned_team_ids": list(owned_team_ids),
                "ball_menu_open": False,
                "visuals_enabled": visuals_enabled,
            },
        )
        await self._start_battle_session(
            battle,
            p1_team=packed_team,
            p2_team=encounter.generated["packed_set"],
            failure_chat_id=encounter.trainer_user_id,
            failure_message_id=encounter.message_id or 0,
        )
        return battle

    async def _start_battle_session(
        self,
        battle: BattleSession,
        *,
        p1_team: str | None,
        p2_team: str | None,
        failure_chat_id: int,
        failure_message_id: int,
    ) -> None:
        p1_team_size = packed_team_member_count(p1_team)
        p2_team_size = packed_team_member_count(p2_team)
        logger.info(
            "Battle startup battle_id=%s mode=%s format=%s p1_team_members=%s p2_team_members=%s",
            battle.battle_id,
            battle.battle_mode,
            battle.format_id,
            p1_team_size,
            p2_team_size,
        )
        battle.metadata["p1_expected_team_size"] = p1_team_size
        battle.metadata["p2_expected_team_size"] = p2_team_size
        battle.metadata["p1_expected_team_labels"] = packed_team_member_labels(p1_team)
        battle.metadata["p2_expected_team_labels"] = packed_team_member_labels(p2_team)
        battle.metadata["p1_team"] = p1_team or ""
        battle.metadata["p2_team"] = p2_team or ""
        battle.metadata.setdefault("battle_seed", [secrets.randbelow(0x10000) for _ in range(4)])
        battle.metadata.setdefault("replay_choices", [])
        battle.metadata.setdefault("replay_index", 0)
        owned_team_ids = list(battle.metadata.get("owned_team_ids") or [])
        if battle.battle_mode == "wild" and owned_team_ids and p1_team_size != len(owned_team_ids):
            logger.warning(
                "Wild battle team mismatch battle_id=%s owned_team_ids=%s packed_team_members=%s",
                battle.battle_id,
                len(owned_team_ids),
                p1_team_size,
            )
        battle.bridge = ShowdownBattleProcess(
            battle_id=battle.battle_id,
            bot_dir=BOT_DIR,
            showdown_dir=SHOWDOWN_DIR,
            format_id=battle.format_id,
            p1_name=battle.players["p1"].name,
            p2_name=battle.players["p2"].name,
            p1_team=p1_team,
            p2_team=p2_team,
            seed=[int(value) for value in list(battle.metadata.get("battle_seed") or [])],
        )

        try:
            await battle.bridge.start()
        except Exception as exc:
            err_str = str(exc)
            is_wild = battle.battle_mode == "wild"
            if is_wild and (
                "unknown species" in err_str.lower()
                or "invalid set" in err_str.lower()
                or "unknown move" in err_str.lower()
                or "unknown ability" in err_str.lower()
            ):
                wild_species = battle.metadata.get("wild_species", "The wild Pokémon")
                await self._edit_message(
                    failure_chat_id,
                    failure_message_id,
                    f"Oops! The wild {wild_species} just ran away!",
                    buttons=None,
                )
            else:
                await self._edit_message(
                    failure_chat_id,
                    failure_message_id,
                    f"Battle startup failed.\n{compact_text(err_str, 600)}",
                    buttons=None,
                )
            raise

        self.battles_by_id[battle.battle_id] = battle
        await self._schedule_recovery_persist()
        battle.runner_task = asyncio.create_task(self.run_battle_loop(battle))

    async def run_battle_loop(self, battle: BattleSession) -> None:
        assert battle.bridge is not None
        try:
            while True:
                event = await battle.bridge.next_event()
                should_render = False
                wait_for_render = False
                mega_notifications: list[tuple[str, str, str]] = []
                zmove_notifications: list[tuple[str, str, str]] = []
                async with battle.lock:
                    event_type = event["type"]
                    if event_type == "ready":
                        pass
                    elif event_type == "started":
                        # REMOVED the render call here. 
                        # We wait for the 'request' event to render the UI.
                        pass
                    elif event_type == "public":
                        if battle.battle_mode == "wild":
                            battle.metadata.pop("encounter_recent", None)
                        battle.public_view.apply_lines(event["lines"])
                        mega_notifications = self.extract_mega_notifications(event["lines"])
                        zmove_notifications = self.extract_zmove_notifications(event["lines"])
                        # REMOVED the render call here.
                        # We batch the text log with the new buttons to save API calls.
                    elif event_type == "request":
                        player = battle.players[event["slot"]]
                        request = event["request"]
                        player.current_request = request
                        battle.public_view.apply_request(player.slot, request)
                        player.request_token += 1
                        auto_locked_team_preview = False
                        if not request.get("wait"):
                            player.locked_choice = None
                            player.primed_action = None
                            if not request.get("update"):
                                player.last_error = None
                        if request.get("teamPreview") and not request.get("wait"):
                            choice = self.team_preview_choice(request, lead_index=1)
                            self.log_team_preview_choice(battle, player, request, choice, automatic=True)
                            player.locked_choice = self.describe_choice(request, choice)
                            player.last_error = None
                            player.primed_action = None
                            await battle.bridge.choose(player.slot, choice)
                            auto_locked_team_preview = True
                        replay_choices = list(battle.metadata.get("replay_choices") or [])
                        replay_index = int(battle.metadata.get("replay_index") or 0)
                        if battle.metadata.get("recovering") and replay_index < len(replay_choices) and not request.get("wait"):
                            replay_entry = replay_choices[replay_index]
                            if str(replay_entry.get("slot") or "") == player.slot:
                                replay_choice = str(replay_entry.get("choice") or "").strip()
                                if replay_choice:
                                    player.locked_choice = self.describe_choice(request, replay_choice)
                                    player.last_error = None
                                    player.primed_action = None
                                    await battle.bridge.choose(player.slot, replay_choice)
                                    used_primary = self._choice_uses_primary_gimmick(replay_choice)
                                    if used_primary in PRIMARY_GIMMICK_ACTIONS:
                                        player.used_primary_gimmick = used_primary
                                    battle.metadata["replay_index"] = replay_index + 1
                                    auto_locked_team_preview = True
                                    if int(battle.metadata.get("replay_index") or 0) >= len(replay_choices):
                                        battle.metadata["recovering"] = False
                        if self.encounter_service is not None:
                            await self.encounter_service.on_battle_request(battle, player.slot, request)
                        await self.auto_choose_gym_request(battle, player.slot, request)
                        should_render = (not auto_locked_team_preview) and (not self.should_defer_request_render(battle))
                        await self._schedule_recovery_persist()
                    elif event_type == "error":
                        player = battle.players[event["slot"]]
                        player.last_error = clean_error(event["message"])
                        player.locked_choice = None
                        player.primed_action = None
                        should_render = True
                    elif event_type == "bridge_error":
                        raise ShowdownBridgeError(event["message"])
                    elif event_type == "ended":
                        battle.finished = True
                        battle.public_view.winner = event.get("winner")
                        battle.public_view.tie = bool(event.get("tie"))
                        if self.encounter_service is not None:
                            await self.encounter_service.on_battle_end(battle)
                            battle.metadata["encounter_end_handled"] = True
                        should_render = True
                        wait_for_render = True
                        await self._schedule_recovery_persist()
                if should_render:
                    await self.request_public_render(battle)
                if mega_notifications:
                    await self.send_mega_notifications(battle, mega_notifications)
                if zmove_notifications:
                    await self.send_zmove_notifications(battle, zmove_notifications)
                if wait_for_render:
                    await self.wait_for_public_render(battle)
                    break
        except EOFError:
            if not battle.finished:
                await self.cancel_public_render(battle)
                await self._edit_message(
                    battle.chat_id,
                    battle.public_message_id,
                    "The local Showdown worker closed unexpectedly.",
                    buttons=None,
                )
        except Exception as exc:
            err_str = str(exc)
            is_wild = battle.battle_mode == "wild"
            # Friendly handling for "Unknown species" and similar Showdown validation
            # errors that happen because a Pokémon form name isn't in the dex.
            if is_wild and (
                "unknown species" in err_str.lower()
                or "invalid set" in err_str.lower()
                or "unknown move" in err_str.lower()
                or "unknown ability" in err_str.lower()
            ):
                # Mark battle as finished so cleanup runs correctly
                battle.finished = True
                owner_id = int(battle.metadata.get("owner_user_id", 0))
                wild_species = battle.metadata.get("wild_species", "The wild Pokémon")
                # Clean up encounter state
                if self.encounter_service is not None:
                    self.encounter_service.active_by_user.pop(owner_id, None)
                await self.cancel_public_render(battle)
                await self._edit_message(
                    battle.chat_id,
                    battle.public_message_id,
                    f"Oops! The wild {wild_species} just ran away!",
                    buttons=None,
                )
            else:
                await self.cancel_public_render(battle)
                await self._edit_message(
                    battle.chat_id,
                    battle.public_message_id,
                    f"The simulator bridge crashed.\n{compact_text(err_str, 600)}",
                    buttons=None,
                )
        finally:
            await self.wait_for_public_render(battle)
            if (
                battle.finished
                and self.encounter_service is not None
                and not battle.metadata.get("encounter_end_handled")
            ):
                await self.encounter_service.on_battle_end(battle)
            self.battles_by_id.pop(battle.battle_id, None)
            self._release_active_pvp_battle(battle)
            if battle.bridge:
                await battle.bridge.close()
            self._clear_battle_runtime_state(battle)
            await self._schedule_recovery_persist()

    async def handle_action_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) != 5:
            await event.answer("Invalid battle action.", alert=True)
            return
        battle = self.battles_by_id.get(parts[1])
        if not battle:
            await event.answer("That battle no longer exists.", alert=True)
            return

        response_text = ""
        alert = False
        should_render = False
        async with battle.lock:
            if battle.finished:
                await event.answer("That battle already ended.", alert=True)
                return
            player = battle.player_for_user(event.sender_id)
            if not player:
                await event.answer("That button belongs to one of the battlers.", alert=True)
                return
            if short_slot(player.slot) != parts[2]:
                await event.answer("That button belongs to the other side.", alert=True)
                return
            try:
                token = int(parts[3])
            except ValueError:
                await event.answer("Invalid action token.", alert=True)
                return
            if token != player.request_token:
                await event.answer("That battle panel is stale. Use the current one.", alert=True)
                return
            request = player.current_request
            if not request:
                await event.answer("No current request is available yet.", alert=True)
                return
            now = asyncio.get_running_loop().time()
            if now < player.next_action_at:
                remaining = max(0.1, player.next_action_at - now)
                await event.answer(f"Wait {remaining:.1f}s before the next action.", alert=False)
                return
            if player.locked_choice and parts[4] not in {"vm", "vt"}:
                await event.answer(f"Already locked in: {player.locked_choice}", alert=True)
                return

            try:
                response_text, should_render, alert = await self.apply_player_action(battle, player, request, parts[4])
            except ValueError as exc:
                await event.answer(str(exc), alert=True)
                return
            except ShowdownBridgeError as exc:
                await event.answer(str(exc), alert=True)
                return
            player.next_action_at = asyncio.get_running_loop().time() + ACTION_COOLDOWN_SECONDS

        if should_render:
            await self.request_public_render(battle)
        await event.answer(response_text, alert=alert)

    async def apply_player_action(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
        action_code: str,
    ) -> tuple[str, bool, bool]:
        if battle.bridge is None:
            raise ShowdownBridgeError("No bridge is attached to this battle.")

        if action_code == "vm":
            if request.get("teamPreview") or request.get("forceSwitch"):
                raise ValueError("Move details are only available on move turns.")
            return self.view_moves_text(battle, request, player), False, True

        if action_code == "vt":
            return self.view_team_text(battle, player, request), False, True

        if self.encounter_service is not None:
            special = await self.encounter_service.handle_battle_special_action(battle, player, request, action_code)
            if special is not None:
                return special

        if action_code in {"tt", "mg", "mx", "my", "dy", "zm", "ub"}:
            if request.get("teamPreview") or request.get("forceSwitch"):
                raise ValueError("Battle mechanics can only be toggled on a move turn.")
            active = request["active"][0]
            mechanics = {
                "tt": ("terastallize", "canTerastallize", "Tera"),
                "mg": ("mega", "canMegaEvo", "Mega"),
                "mx": ("megax", "canMegaEvoX", "Mega X"),
                "my": ("megay", "canMegaEvoY", "Mega Y"),
                "dy": ("dynamax", "canDynamax", "Dynamax"),
                "zm": ("zmove", "canZMove", "Z-Move"),
                "ub": ("ultra", "canUltraBurst", "Ultra Burst"),
            }
            action_name, active_key, label = mechanics[action_code]
            if not active.get(active_key):
                raise ValueError(f"{label} is not available right now.")
            if (
                action_name in PRIMARY_GIMMICK_ACTIONS
                and player.used_primary_gimmick in PRIMARY_GIMMICK_ACTIONS
                and player.used_primary_gimmick != action_name
            ):
                used_label = PRIMARY_GIMMICK_LABELS.get(
                    str(player.used_primary_gimmick),
                    str(player.used_primary_gimmick).title(),
                )
                raise ValueError(
                    f"You already used {used_label}. Only one of Mega, Tera, or Dynamax can be used per battle."
                )
            if (
                action_name in PRIMARY_GIMMICK_ACTIONS
                and player.primed_action in PRIMARY_GIMMICK_ACTIONS
                and player.primed_action != action_name
            ):
                active_label = PRIMARY_GIMMICK_LABELS.get(player.primed_action, player.primed_action.title())
                raise ValueError(f"{active_label} is already active. Deactivate it first.")
            if player.primed_action == action_name:
                player.primed_action = None
                return f"{label} deactivated.", True, False
            player.primed_action = action_name
            return f"{label} activated. Pick a move.", True, False

        if action_code == "f":
            player.locked_choice = "Forfeit"
            player.primed_action = None
            await battle.bridge.forfeit(player.slot)
            return "Forfeit submitted.", True, False

        if request.get("wait"):
            raise ValueError("The simulator is not waiting for a choice from you right now.")

        chosen_primary_gimmick: str | None = None
        if request.get("teamPreview"):
            if not action_code.startswith("t"):
                raise ValueError("Pick a lead Pokemon from the current panel.")
            index = int(action_code[1:])
            if index < 1 or index > len(request["side"]["pokemon"]):
                raise ValueError("That lead slot is not valid.")
            choice = self.team_preview_choice(request, lead_index=index)
            self.log_team_preview_choice(battle, player, request, choice, automatic=False)
        elif request.get("forceSwitch"):
            if not action_code.startswith("s"):
                raise ValueError("You must pick a switch-in right now.")
            index = int(action_code[1:])
            if index not in self.valid_switch_indices(request, forced=True):
                raise ValueError("That switch slot is not valid right now.")
            choice = f"switch {index}"
        else:
            if action_code.startswith("m"):
                index = int(action_code[1:])
                active = request["active"][0]
                moves = active["moves"]
                if index < 1 or index > len(moves):
                    raise ValueError("That move slot is not valid.")
                if player.primed_action in {"dynamax", "zmove"}:
                    suffix = self.special_move_suffix(request, player.primed_action, index)
                else:
                    move = moves[index - 1]
                    if move.get("disabled"):
                        raise ValueError(f"Move {index} is disabled.")
                    suffix = self.special_move_suffix(request, player.primed_action, index)
                if player.primed_action in PRIMARY_GIMMICK_ACTIONS and suffix:
                    chosen_primary_gimmick = player.primed_action
                choice = f"move {index}{suffix}"
            elif action_code.startswith("s"):
                index = int(action_code[1:])
                if index not in self.valid_switch_indices(request):
                    raise ValueError("That switch slot is not valid right now.")
                choice = f"switch {index}"
            else:
                raise ValueError("That action is not valid for the current request.")

        player.locked_choice = self.describe_choice(request, choice)
        player.last_error = None
        replay_choices = list(battle.metadata.get("replay_choices") or [])
        replay_choices.append({"slot": player.slot, "choice": choice})
        battle.metadata["replay_choices"] = replay_choices
        await battle.bridge.choose(player.slot, choice)
        if chosen_primary_gimmick in PRIMARY_GIMMICK_ACTIONS:
            player.used_primary_gimmick = chosen_primary_gimmick
        player.primed_action = None
        if self.encounter_service is not None:
            await self.encounter_service.after_player_choice(battle, player, request, choice)
        await self._schedule_recovery_persist()
        should_render = self.current_actor_slot(battle) is not None
        return f"Locked in: {player.locked_choice}", should_render, False

    def should_defer_request_render(self, battle: BattleSession) -> bool:
        if self.current_actor_slot(battle):
            return False
        return any(player.locked_choice for player in battle.players.values())

    def special_move_suffix(self, request: dict[str, Any], primed_action: str | None, index: int) -> str:
        if not primed_action:
            return ""

        active = request["active"][0]
        if primed_action == "terastallize":
            if not active.get("canTerastallize"):
                raise ValueError("Tera is not available right now.")
            return " terastallize"
        if primed_action == "mega":
            if not active.get("canMegaEvo"):
                raise ValueError("Mega Evolution is not available right now.")
            return " mega"
        if primed_action == "megax":
            if not active.get("canMegaEvoX"):
                raise ValueError("Mega X is not available right now.")
            return " megax"
        if primed_action == "megay":
            if not active.get("canMegaEvoY"):
                raise ValueError("Mega Y is not available right now.")
            return " megay"
        if primed_action == "dynamax":
            max_moves = ((active.get("maxMoves") or {}).get("maxMoves") or [])
            if index < 1 or index > len(max_moves):
                raise ValueError("That Max Move slot is not valid.")
            if max_moves[index - 1].get("disabled"):
                raise ValueError(f"Max Move {index} is disabled.")
            return " dynamax"
        if primed_action == "zmove":
            z_moves = active.get("canZMove") or []
            if index < 1 or index > len(z_moves):
                raise ValueError("That Z-Move slot is not valid.")
            if not z_moves[index - 1]:
                raise ValueError("That move cannot be used as a Z-Move.")
            return " zmove"
        if primed_action == "ultra":
            if not active.get("canUltraBurst"):
                raise ValueError("Ultra Burst is not available right now.")
            return " ultra"
        return ""

    def describe_choice(self, request: dict[str, Any], choice: str) -> str:
        if choice == "Forfeit":
            return "Forfeit"
        if choice.startswith("team "):
            positions = self.parse_team_preview_positions(choice)
            if not positions:
                return "Lead selected"
            pokemon = request["side"]["pokemon"][positions[0] - 1]
            return f"Lead: {details_name(pokemon['details'])}"
        if choice.startswith("switch "):
            index = int(choice.split()[1])
            pokemon = request["side"]["pokemon"][index - 1]
            return f"Switch to {details_name(pokemon['details'])}"
        if choice.startswith("move "):
            parts = choice.split()
            index = int(parts[1])
            move_name = request["active"][0]["moves"][index - 1]["move"]
            if len(parts) >= 3:
                action_name = {
                    "terastallize": "Tera",
                    "mega": "Mega",
                    "megax": "Mega X",
                    "megay": "Mega Y",
                    "dynamax": "Dynamax",
                    "zmove": "Z-Move",
                    "ultra": "Ultra Burst",
                }.get(parts[2], parts[2].title())
                return f"{action_name} + {move_name}"
            return move_name
        return choice

    async def request_public_render(self, battle: BattleSession) -> None:
        battle.render_requested = True
        if battle.public_render_task is None or battle.public_render_task.done():
            battle.public_render_task = asyncio.create_task(self._run_public_render_queue(battle))

    async def wait_for_public_render(self, battle: BattleSession) -> None:
        while True:
            task = battle.public_render_task
            if task is None:
                return
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                return

    async def cancel_public_render(self, battle: BattleSession) -> None:
        battle.render_requested = False
        task = battle.public_render_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run_public_render_queue(self, battle: BattleSession) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                battle.render_requested = False
                delay = (battle.last_public_edit_at + PUBLIC_RENDER_MIN_INTERVAL) - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if await self.render_public_message(battle):
                    battle.last_public_edit_at = loop.time()
                if not battle.render_requested:
                    break
        finally:
            battle.public_render_task = None
            if battle.render_requested:
                battle.public_render_task = asyncio.create_task(self._run_public_render_queue(battle))

    async def render_public_message(self, battle: BattleSession) -> bool:
        async with battle.lock:
            payload = await self.prepare_public_render(battle)
        if payload is None:
            return False
        text, buttons, parse_mode, visual_payload = payload
        if visual_payload is not None:
            await self._upsert_visual_message(
                battle,
                text=text,
                buttons=buttons,
                parse_mode=parse_mode,
                visual_payload=visual_payload,
            )
            return True
        await self._edit_message(
            battle.chat_id,
            battle.public_message_id,
            text,
            buttons=buttons,
            parse_mode=parse_mode,
        )
        return True

    async def prepare_public_render(
        self,
        battle: BattleSession,
    ) -> tuple[str, list[list[Button]] | None, str | None, tuple[Any, str] | None] | None:
        text, button_specs = self.render_public_text_and_buttons(battle)
        
        parse_mode = "html"
        
        visual_payload: tuple[Any, str] | None = None
        scene_fingerprint: str | None = None

        if self.battle_uses_visuals(battle):
            text = self.visual_caption_text(text)
            visual_payload = await asyncio.to_thread(
                self.visual_renderer.render,
                battle,
                highlight_slot=self.current_actor_slot(battle),
            )
            if visual_payload is not None:
                scene_fingerprint = visual_payload[1]

        fingerprint = json.dumps({"text": text, "buttons": button_specs, "scene": scene_fingerprint}, ensure_ascii=False)
        if fingerprint == battle.last_render_fingerprint:
            return None
        battle.last_render_fingerprint = fingerprint

        return text, self.build_buttons(button_specs), parse_mode, visual_payload

    def battle_uses_visuals(self, battle: BattleSession) -> bool:
        return bool(battle.metadata.get("visuals_enabled")) and self.visual_renderer.available

    def visual_caption_text(self, text: str) -> str:
        return compact_text(text.replace("**", "").strip(), limit=VISUAL_CAPTION_LIMIT)

    async def _upsert_visual_message(
        self,
        battle: BattleSession,
        *,
        text: str,
        buttons: list[list[Button]] | None,
        parse_mode: str | None,
        visual_payload: tuple[Any, str],
    ) -> None:
        file_obj, scene_fingerprint = visual_payload
        send_media = battle.visual_message_id is None or battle.last_visual_scene_fingerprint != scene_fingerprint

        if battle.visual_message_id is None:
            previous_message_id = battle.public_message_id
            sent = await self.client.send_file(
                battle.chat_id,
                file_obj,
                caption=text,
                buttons=buttons,
                parse_mode=parse_mode,
                force_document=False,
                reply_to=previous_message_id or None,
            )
            battle.visual_message_id = sent.id
            battle.public_message_id = sent.id
            battle.last_visual_scene_fingerprint = scene_fingerprint
            if previous_message_id and previous_message_id != sent.id:
                await self._edit_message(
                    battle.chat_id,
                    previous_message_id,
                    "Battle started below.",
                    buttons=None,
                    parse_mode=None,
                )
            return

        try:
            await self.client.edit_message(
                battle.chat_id,
                battle.visual_message_id,
                text,
                file=file_obj if send_media else None,
                buttons=buttons,
                parse_mode=parse_mode,
                link_preview=False,
            )
        except MessageNotModifiedError:
            return
        except RPCError:
            await self._edit_message(
                battle.chat_id,
                battle.visual_message_id,
                text,
                buttons=buttons,
                parse_mode=parse_mode,
            )
            return

        if send_media:
            battle.last_visual_scene_fingerprint = scene_fingerprint

    def render_public_text_and_buttons(self, battle: BattleSession) -> tuple[str, list[list[tuple[str, str]]] | None]:
        if battle.finished and battle.battle_mode == "wild" and battle.metadata.get("encounter_outcome") == "ran":
            return "<b>You ran away safely.</b>", None
        if (
            battle.finished
            and battle.battle_mode == "wild"
            and battle.metadata.get("encounter_outcome") == "caught"
            and self.encounter_service is not None
        ):
            caught_render = self.encounter_service.caught_battle_render(battle)
            if caught_render is not None:
                return caught_render

        recent = list(battle.public_view.display_recent())
        if self.encounter_service is not None:
            recent.extend(self.encounter_service.extra_recent_lines(battle))
        recent = self.trim_opening_recent_lines(battle, recent)
        recent = [escape_html_text(entry) for entry in recent]
        current_slot = self.current_actor_slot(battle)
        if battle.battle_mode == "wild":
            return self.render_wild_public_text_and_buttons(battle, recent, current_slot)

        lines: list[str] = []
        if recent:
            recent_turn = battle.public_view.turn
            if not battle.public_view.recent and battle.public_view.last_turn_recent and recent_turn > 1:
                recent_turn -= 1
            if recent_turn > 0:
                lines.append(f"<b>Turn {recent_turn} Recap</b>")
            else:
                lines.append("<b>Battle Recap</b>")
            lines.extend(f"• {entry}" for entry in recent)
            lines.append("")

        lines.extend(self.render_active_block(battle, "p1", highlight=current_slot == "p1"))
        lines.append("")
        lines.extend(self.render_active_block(battle, "p2", highlight=current_slot == "p2"))

        button_specs: list[list[tuple[str, str]]] | None = None
        if battle.finished:
            lines.append("")
            if battle.battle_mode == "wild" and battle.metadata.get("encounter_note"):
                lines.append(escape_html_text(battle.metadata["encounter_note"]))
            elif battle.battle_mode == "gym":
                leader_name = escape_html_text(battle.metadata.get("gym_leader") or battle.players["p2"].name)
                gym_label = escape_html_text(battle.metadata.get("gym_label") or "the gym")
                if battle.public_view.tie:
                    lines.append(f"The {gym_label} battle ended in a tie.")
                elif battle.public_view.winner == battle.players["p1"].name:
                    lines.append(f"You defeated {leader_name} at {gym_label}.")
                else:
                    lines.append(f"{leader_name} defended {gym_label}.")
            elif battle.public_view.tie:
                lines.append("Battle over: tie.")
            else:
                winner = escape_html_text(battle.public_view.winner or "Unknown winner")
                lines.append(f"Battle over: winner is {winner}.")
        elif current_slot:
            current_player = battle.players[current_slot]
            request = current_player.current_request or {}
            if self.encounter_service is not None:
                override = self.encounter_service.override_battle_render(battle, current_player, request)
                if override is not None:
                    return override
            lines.append("")
            if current_player.last_error:
                lines.append(
                    f"{escape_html_text(current_player.name)}, your last choice was rejected: "
                    f"{escape_html_text(current_player.last_error)}"
                )
            if self.encounter_service is not None:
                lines.extend(self.encounter_service.extra_status_lines(battle))
            if request.get("teamPreview"):
                lines.append(f"{mention_html(current_player.user_id, current_player.name)}: choose your lead.")
                button_specs = self.team_preview_button_specs(battle, current_player, request)
            elif request.get("forceSwitch"):
                lines.append(f"{mention_html(current_player.user_id, current_player.name)}: choose your switch-in.")
                button_specs = self.forced_switch_button_specs(battle, current_player, request)
            else:
                lines.append(f"{mention_html(current_player.user_id, current_player.name)}: choose your move or switch.")
                button_specs = self.move_request_button_specs(battle, current_player, request)
        else:
            status_lines: list[str] = []
            if not any(player.locked_choice for player in battle.players.values()):
                status_lines.append("Generating teams and waiting for the simulator...")
            if self.encounter_service is not None:
                status_lines.extend(self.encounter_service.extra_status_lines(battle))
            if status_lines:
                lines.append("")
                lines.extend(status_lines)

        text = "\n".join(lines)
        text = text.replace("â€¢ ", "- ").replace("â–ˆ", "#").replace("â–‘", "-")
        return text, button_specs

    def trim_opening_recent_lines(self, battle: BattleSession, recent: list[str]) -> list[str]:
        recent_lines = list(recent)
        if (
            not battle.finished
            and battle.public_view.turn <= 1
            and recent_lines
            and all(
                (" sent out " in entry or " revealed " in entry or " dragged in " in entry)
                for entry in recent_lines
            )
        ):
            return []
        return recent_lines

    def render_wild_public_text_and_buttons(
        self,
        battle: BattleSession,
        recent: list[str],
        current_slot: str | None,
    ) -> tuple[str, list[list[tuple[str, str]]] | None]:
        lines: list[str] = []
        button_specs: list[list[tuple[str, str]]] | None = None

        recent_lines = self.trim_opening_recent_lines(battle, recent)

        if recent_lines:
            lines.extend(recent_lines)

        if battle.finished:
            note = str(battle.metadata.get("encounter_note", "")).strip()
            if note:
                if lines:
                    lines.append("")
                lines.append(escape_html_text(note))
            elif battle.public_view.tie:
                lines.append("The encounter ended in a tie.")
            elif battle.public_view.winner:
                lines.append(f"{escape_html_text(battle.public_view.winner)} won the battle.")
            text = "\n".join(lines).strip()
            text = text.replace("â€¢ ", "- ").replace("â–ˆ", "#").replace("â–‘", "-")
            return text, None

        if current_slot:
            current_player = battle.players[current_slot]
            request = current_player.current_request or {}
            if self.encounter_service is not None:
                override = self.encounter_service.override_battle_render(battle, current_player, request)
                if override is not None:
                    return override

        if lines:
            lines.append("")
        lines.extend(self.render_active_block(battle, "p1", highlight=current_slot == "p1"))
        lines.append("")
        lines.extend(self.render_active_block(battle, "p2", highlight=current_slot == "p2"))

        if current_slot:
            current_player = battle.players[current_slot]
            request = current_player.current_request or {}
            lines.append("")
            if current_player.last_error:
                lines.append(
                    f"{escape_html_text(current_player.name)}, your last choice was rejected: "
                    f"{escape_html_text(current_player.last_error)}"
                )
            if self.encounter_service is not None:
                lines.extend(self.encounter_service.extra_status_lines(battle))
            if request.get("forceSwitch"):
                lines.append(f"{mention_html(current_player.user_id, current_player.name)}: choose your switch-in.")
                button_specs = self.forced_switch_button_specs(battle, current_player, request)
            elif not request.get("teamPreview"):
                lines.append(f"{mention_html(current_player.user_id, current_player.name)}: choose your move or switch.")
                button_specs = self.move_request_button_specs(battle, current_player, request)
        elif self.encounter_service is not None:
            status_lines = self.encounter_service.extra_status_lines(battle)
            if status_lines:
                lines.append("")
                lines.extend(status_lines)

        text = "\n".join(lines)
        text = text.replace("â€¢ ", "- ").replace("â–ˆ", "#").replace("â–‘", "-")
        return text, button_specs

    def render_active_block(self, battle: BattleSession, slot: str, *, highlight: bool) -> list[str]:
        player_name = battle.players[slot].name
        active = battle.public_view.active.get(slot)
        if not active:
            suffix = " (TURN)" if highlight else ""
            return [f"{escape_html_text(player_name)}{suffix}: waiting for lead"]

        info_parts = [
            f"Level: {active.get('level', '?')}",
            f"Type: {format_types(active.get('types'))}",
        ]
        status = active.get("status")
        if status:
            info_parts.append(f"Status: {status}")
        if battle.battle_mode == "wild" and slot == "p2":
            header = player_name
        else:
            header = f"{player_name}: {active['name']}"
        if highlight:
            header += " (TURN)"
        return [
            escape_html_text(header),
            escape_html_text(" | ".join(info_parts)),
            escape_html_text(f"HP: {hp_bar_ascii(active.get('percent'))} ({active.get('hp_text') or 'unknown'})"),
        ]

    def current_actor_slot(self, battle: BattleSession) -> str | None:
        actionable = self.actionable_slots(battle)
        return actionable[0] if actionable else None

    def actionable_slots(self, battle: BattleSession) -> list[str]:
        slots = [
            slot
            for slot in ("p1", "p2")
            if battle.players[slot].current_request
            and not battle.players[slot].current_request.get("wait")
            and not battle.players[slot].locked_choice
        ]
        forced = [slot for slot in slots if battle.players[slot].current_request.get("forceSwitch")]
        if forced:
            remaining = [slot for slot in slots if slot not in forced]
            return forced + remaining
        return slots

    def action_data(self, battle: BattleSession, player: PlayerState, action_code: str) -> str:
        return f"bact:{battle.battle_id}:{short_slot(player.slot)}:{player.request_token}:{action_code}"

    def parse_team_preview_positions(self, choice: str) -> list[int]:
        payload = choice.removeprefix("team").strip()
        if not payload or payload.lower() == "default":
            return []
        chunks = payload.split(",") if "," in payload else list(payload)
        positions: list[int] = []
        for chunk in chunks:
            token = str(chunk).strip()
            if not token:
                continue
            if not token.isdigit():
                return []
            positions.append(int(token))
        return positions

    def team_preview_choice(self, request: dict[str, Any], *, lead_index: int = 1) -> str:
        pokemon = list((request.get("side") or {}).get("pokemon") or [])
        if not pokemon:
            raise ValueError("No team preview data is available.")
        if lead_index < 1 or lead_index > len(pokemon):
            raise ValueError("That lead slot is not valid.")
        # Lead-only choice keeps the previous stable behavior and lets the
        # simulator auto-fill the rest of the roster.
        return f"team {lead_index}"

    def log_team_preview_choice(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
        choice: str,
        *,
        automatic: bool,
    ) -> None:
        team_size = len(list((request.get("side") or {}).get("pokemon") or []))
        expected_team_size = int(battle.metadata.get(f"{player.slot}_expected_team_size") or 0)
        max_chosen_raw = request.get("maxChosenTeamSize")
        try:
            max_chosen = int(max_chosen_raw) if max_chosen_raw is not None else None
        except (TypeError, ValueError):
            max_chosen = None
        if expected_team_size > 0 and team_size < expected_team_size:
            preview_members = [
                details_name(str(pokemon.get("details", pokemon.get("ident", f"Pokemon {index}"))))
                for index, pokemon in enumerate(list((request.get("side") or {}).get("pokemon") or []), start=1)
            ]
            logger.warning(
                "Preview roster shorter than packed team battle_id=%s slot=%s user_id=%s automatic=%s expected_team_size=%s preview_team_size=%s choice=%s expected_members=%s preview_members=%s",
                battle.battle_id,
                player.slot,
                player.user_id,
                automatic,
                expected_team_size,
                team_size,
                choice,
                battle.metadata.get(f"{player.slot}_expected_team_labels") or [],
                preview_members,
            )
        if battle.format_id == FULL_GIMMICK_FORMAT_ID and max_chosen is not None and max_chosen < team_size:
            logger.warning(
                "Full gimmick preview limit detected battle_id=%s slot=%s user_id=%s automatic=%s roster_size=%s maxChosenTeamSize=%s choice=%s",
                battle.battle_id,
                player.slot,
                player.user_id,
                automatic,
                team_size,
                max_chosen,
                choice,
            )

    def build_buttons(self, specs: list[list[tuple[str, str]]] | None) -> list[list[Button]] | None:
        if not specs:
            return None
        return [[Button.inline(label, data=data.encode("utf-8")) for label, data in row] for row in specs]

    def _request_pokemon_token(self, pokemon: dict[str, Any], *, fallback_index: int) -> str:
        details = str(pokemon.get("details", "") or "").strip()
        ident = str(pokemon.get("ident", "") or "").strip()
        return details or ident or f"pokemon-{fallback_index}"

    def _request_slot_number_map(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
    ) -> dict[int, int]:
        side_pokemon = list((request.get("side") or {}).get("pokemon") or [])
        if not side_pokemon:
            return {}

        metadata_key = f"{player.slot}_stable_slot_tokens"
        original_tokens = list(battle.metadata.get(metadata_key) or [])
        if not original_tokens:
            original_tokens = [
                self._request_pokemon_token(pokemon, fallback_index=index)
                for index, pokemon in enumerate(side_pokemon, start=1)
            ]
            battle.metadata[metadata_key] = list(original_tokens)

        token_positions: dict[str, list[int]] = {}
        for position, token in enumerate(original_tokens, start=1):
            token_positions.setdefault(token, []).append(position)

        current_counts: dict[str, int] = {}
        mapping: dict[int, int] = {}
        for current_index, pokemon in enumerate(side_pokemon, start=1):
            token = self._request_pokemon_token(pokemon, fallback_index=current_index)
            current_counts[token] = current_counts.get(token, 0) + 1
            matches = token_positions.get(token) or []
            match_index = current_counts[token] - 1
            mapping[current_index] = matches[match_index] if match_index < len(matches) else current_index
        return mapping

    def team_preview_button_specs(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
    ) -> list[list[tuple[str, str]]]:
        specs = [(str(index), self.action_data(battle, player, f"t{index}")) for index, _ in enumerate(request["side"]["pokemon"], start=1)]
        rows = chunk_specs(specs, per_row=4)
        rows.append([("TEAM", self.action_data(battle, player, "vt"))])
        return rows

    def move_request_button_specs(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
    ) -> list[list[tuple[str, str]]]:
        active = request["active"][0]
        rows: list[list[tuple[str, str]]] = []
        move_specs = [(str(index), self.action_data(battle, player, f"m{index}")) for index, _ in enumerate(active["moves"], start=1)]
        rows.extend(chunk_specs(move_specs, per_row=4))
        rows.append(
            [
                ("MOVES", self.action_data(battle, player, "vm")),
                ("TEAM", self.action_data(battle, player, "vt")),
            ]
        )

        if self.can_offer_switch(request):
            rows.extend(self.switch_button_specs(battle, player, request))

        mechanics = self.mechanic_button_specs(battle, player, request)
        if mechanics:
            rows.extend(chunk_specs(mechanics, per_row=2))
        if self.encounter_service is not None:
            rows.extend(self.encounter_service.extra_action_rows(battle, player, request))
        if not (battle.battle_mode == "wild" and player.slot == "p1"):
            rows.append([("FORFIT", self.action_data(battle, player, "f"))])
        return rows

    def switch_button_specs(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
    ) -> list[list[tuple[str, str]]]:
        slot_map = self._request_slot_number_map(battle, player, request)
        specs = [
            (str(slot_map.get(index, index)), self.action_data(battle, player, f"s{index}"))
            for index in self.valid_switch_indices(request, forced=bool(request.get("forceSwitch")))
        ]
        return chunk_specs(specs, per_row=5)

    def forced_switch_button_specs(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
    ) -> list[list[tuple[str, str]]]:
        rows = self.switch_button_specs(battle, player, request)
        rows.append([("TEAM", self.action_data(battle, player, "vt"))])
        return rows

    def mechanic_button_specs(
        self,
        battle: BattleSession,
        player: PlayerState,
        request: dict[str, Any],
    ) -> list[tuple[str, str]]:
        active = request["active"][0]
        specs: list[tuple[str, str]] = []
        locked_primary = player.primed_action if player.primed_action in PRIMARY_GIMMICK_ACTIONS else None
        used_primary = player.used_primary_gimmick if player.used_primary_gimmick in PRIMARY_GIMMICK_ACTIONS else None
        allow_primary = lambda action_name: locked_primary in {None, action_name} and used_primary in {None, action_name}

        if active.get("canMegaEvo") and allow_primary("mega"):
            label = "MEGA" + (" *" if player.primed_action == "mega" else "")
            specs.append((label, self.action_data(battle, player, "mg")))
        elif active.get("canMegaEvoX") and allow_primary("megax"):
            label = "MEGA X" + (" *" if player.primed_action == "megax" else "")
            specs.append((label, self.action_data(battle, player, "mx")))
        elif active.get("canMegaEvoY") and allow_primary("megay"):
            label = "MEGA Y" + (" *" if player.primed_action == "megay" else "")
            specs.append((label, self.action_data(battle, player, "my")))
        elif active.get("canUltraBurst"):
            label = "ULTRA" + (" *" if player.primed_action == "ultra" else "")
            specs.append((label, self.action_data(battle, player, "ub")))
        if active.get("canZMove"):
            label = "Z" + (" *" if player.primed_action == "zmove" else "")
            specs.append((label, self.action_data(battle, player, "zm")))
        if active.get("canTerastallize") and allow_primary("terastallize"):
            label = "TERA" + (" *" if player.primed_action == "terastallize" else "")
            specs.append((label, self.action_data(battle, player, "tt")))
        if active.get("canDynamax") and allow_primary("dynamax"):
            label = "DYNA" + (" *" if player.primed_action == "dynamax" else "")
            specs.append((label, self.action_data(battle, player, "dy")))
        return specs

    def can_offer_switch(self, request: dict[str, Any]) -> bool:
        return bool(self.valid_switch_indices(request))

    def valid_switch_indices(self, request: dict[str, Any], *, forced: bool = False) -> list[int]:
        if not forced:
            active = request["active"][0]
            if active.get("trapped"):
                return []
        return [
            index
            for index, pokemon in enumerate(request["side"]["pokemon"], start=1)
            if not pokemon.get("active") and not fainted(str(pokemon.get("condition", "")))
        ]

    def view_moves_text(self, battle: BattleSession, request: dict[str, Any], player: PlayerState) -> str:
        active = request.get("active") or []
        if not active:
            return "No move details available."
        current = active[0]
        active_state = battle.public_view.active.get(player.slot) or {}
        is_dynamaxed = bool(active_state.get("dynamaxed"))
        
        if (player.primed_action == "dynamax" or is_dynamaxed) and current.get("maxMoves"):
            lines = [
                compact_text(f"{i} {m.get('move', f'Max Move {i}')}{' DIS' if m.get('disabled') else ''}", limit=48)
                for i, m in enumerate((current.get("maxMoves") or {}).get("maxMoves", []), start=1)
            ]
        elif player.primed_action == "zmove" and current.get("canZMove"):
            lines = []
            for index, option in enumerate(current.get("canZMove") or [], start=1):
                lines.append(compact_text(f"{index} {option.get('move', 'Z-Move')}" if option else f"{index} unavailable", limit=48))
        else:
            lines = []
            for index, move in enumerate(current["moves"], start=1):
                move_name = str(move.get("move", f"Move {index}"))
                move_id = move.get("id") or re.sub(r"[^a-z0-9]+", "", move_name.lower())
                info = self.local_data_service.move_info.get(move_id, {})
                
                # Abbreviate type to 3 letters to save space (e.g., Ele, Fir, Nor)
                move_type = str(move.get("displayType") or info.get("type", "?")).title()[:3]
                pwr = info.get("power", "-")
                acc = move.get("displayAccuracy") or info.get("accuracy", "-")
                pp = f"{move.get('pp', '?')}/{move.get('maxpp', '?')}"
                suffix = " [X]" if move.get("disabled") else ""

                # Dense Format: 1.Thunderbolt[Ele] P:90 A:100 15/15
                lines.append(f"{index}.{move_name}[{move_type}]{suffix} P:{pwr} A:{acc} {pp}")
                
        return compact_text("\n".join(lines), limit=195)

    def view_team_text(self, battle: BattleSession, player: PlayerState, request: dict[str, Any]) -> str:
        slot_map = self._request_slot_number_map(battle, player, request)
        lines = []
        for index, pokemon in enumerate(request["side"]["pokemon"], start=1):
            prefix = "*" if pokemon.get("active") else ""
            details = str(pokemon.get("details", pokemon.get("ident", f"Pokemon {index}")))
            name = details_name(details)
            
            # Abbreviate typing to save space
            species_key_str = re.sub(r"[^a-z0-9]+", "", name.lower())
            info = self.local_data_service.species_reference.get(species_key_str, {})
            types = "/".join(t[:3].title() for t in info.get("types", ["?"])) if info.get("types") else "?"
            
            parsed = parse_condition(str(pokemon.get("condition", "")))
            # Prioritize % over raw HP to save characters
            hp = "FNT" if parsed["fainted"] else (f"{parsed['percent']}%" if parsed["percent"] is not None else parsed["hp_text"])
            
            item = str(pokemon.get("item") or "")
            item = "None" if not item or item.lower() == "none" else item
            # Truncate extremely long item names (e.g., Heavy-Duty Boots -> Heavy-Duty)
            if len(item) > 10:
                item = item[:10]

            # Dense Format: 1*Charizard[Fir/Fly] 100% Item
            stable_index = slot_map.get(index, index)
            lines.append(f"{stable_index}{prefix}.{name}[{types}] {hp} {item}")
            
        return compact_text("\n".join(lines), limit=195)

    def battle_stats_text(self, snapshot: dict[str, Any]) -> str:
        name = str(snapshot.get("name") or "Pokemon")
        level = int(snapshot.get("level") or 0)
        base_types = [str(item) for item in (snapshot.get("baseTypes") or []) if str(item).strip()]
        current_types = [str(item) for item in (snapshot.get("currentTypes") or []) if str(item).strip()]
        item_name = str(snapshot.get("item") or "None").strip() or "None"
        item_id = str(snapshot.get("itemId") or "").strip()
        status = str(snapshot.get("status") or "").strip()
        hp = snapshot.get("hp") or {}
        lines = [f"**{name}** [Lv. {level}]"]

        base_type_text = format_types(base_types)
        current_type_text = format_types(current_types)
        if base_types and current_types and base_types != current_types:
            lines.append(f"Type: {base_type_text} -> {current_type_text} [changed]")
        else:
            lines.append(f"Type: {current_type_text if current_types else base_type_text}")
        lines.append(f"Held Item: {item_name}")
        if status:
            lines.append(f"Status: {status}")
        lines.append(f"HP: {int(hp.get('current') or 0)}/{int(hp.get('max') or 0)}")
        if snapshot.get("bestEffort"):
            lines.append("_Best-effort snapshot: some live modifiers could not be read safely between turns._")

        stats = snapshot.get("stats") or {}
        for stat in ("atk", "def", "spa", "spd", "spe"):
            stat_payload = stats.get(stat) or {}
            current = int(stat_payload.get("current") or 0)
            base = int(stat_payload.get("base") or 0)
            unboosted = int(stat_payload.get("unboosted") or 0)
            stage = int(stat_payload.get("stage") or 0)
            modified = current != base
            value = f"**{current}**" if modified else str(current)
            notes: list[str] = []
            if stage:
                notes.append(format_stat_stage(stage))
            item_note = ITEM_STAT_NOTES.get(item_id, {}).get(stat)
            if item_note and unboosted != base:
                notes.append(item_note)
            elif unboosted != base:
                notes.append("modifier active")
            note_text = f" ({', '.join(notes)})" if notes else ""
            lines.append(f"{STAT_LINE_LABELS[stat]}: {value}{note_text}")
        return "\n".join(lines)

    async def _edit_message(self, chat_id: int, message_id: int, text: str, buttons: list[list[Button]] | None, parse_mode: str | None = None) -> None:
        try:
            await self.client.edit_message(chat_id, message_id, text, buttons=buttons, parse_mode=parse_mode, link_preview=False)
        except MessageNotModifiedError:
            return
        except RPCError:
            return
