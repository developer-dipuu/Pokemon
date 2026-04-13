from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import html
import json
import logging
import re
from pathlib import Path
import random
import secrets
from typing import Any, Sequence
from urllib.parse import quote

from telethon import Button
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.tl.types import User
from telethon.utils import get_display_name

from bot.config import ADMIN_USER_ID_SET, EVOLUTION_CHAINS_PATH, POKECHAIN_PATH, REGIONS_PATH, STARTERS_PATH, STONES_PATH
from bot.db.repositories import (
    AdminRepository,
    CommandLockRepository,
    DISPLAY_OPTIONS,
    SORT_CAUGHT,
    SORT_OPTIONS,
    InventoryRepository,
    PokemonRepository,
    RedeemCodeRepository,
    TeamRepository,
    TrainerRepository,
    display_mode_label,
    normalize_display_mode,
    normalize_lookup,
    normalize_sort_mode,
    pokemon_display_label,
    pokemon_total_ev,
    sort_mode_label,
)
from bot.db.session import clear_database, db_session, run_db_work_async
from bot.game.balls import BALL_ORDER, ball_label, normalize_ball_kind
from bot.game.fusion import (
    FORM_CHANGE_ITEM_COST_VP,
    FORM_CHANGE_ITEM_ORDER,
    KEY_ITEM_DNA_SPLICERS,
    KEY_ITEM_METEORITE,
    KEY_ITEM_N_LUNARIZER,
    KEY_ITEM_N_SOLARIZER,
    KEY_ITEM_PRISON_BOTTLE,
    KEY_ITEM_REINS_OF_UNITY,
    active_item_key,
    compatible_host,
    compatible_partner,
    effective_species,
    fusion_result_species,
    has_form_state,
    item_form_targets,
    item_key,
    item_name_from_key,
    item_requires_partner,
    load_form_state,
    restore_payload_from_snapshot,
    set_form_state,
    snapshot_owned_pokemon,
    toggle_result_species,
)
from bot.game.move_history import load_move_history, record_move_history
from bot.game.services.encounters import EncounterService, REGIONAL_FORM_OVERRIDES
from bot.game.services.factions import FactionService
from bot.game.services.daycare import DaycareService
from bot.game.services.encounter_loot import MEGA_STONE_LOCATIONS, SHINY_CHARM_ITEM, TERA_SHARDS, TM_DROPS, Z_CRYSTALS
from bot.game.services.generator import PokemonGeneratorService
from bot.game.services.medicine import (
    CANDY_KEYS,
    EXP_CANDY_DROP_KEYS,
    FEATHER_KEYS,
    MEDICINE_DEFINITIONS,
    MOCHI_KEYS,
    SHOP_MEDICINE_KEYS,
    medicine_name,
    medicine_shop_price,
    normalize_medicine_key,
)
from bot.game.services.pokemon_data import NATURES, PokemonDataService, species_key
from bot.game.services.pokemon_stats import PokemonStatsService
from bot.game.services.team_manager import TeamManagerService
from bot.game.services.weekend_boost import set_weekend_boost_enabled, weekend_boost_status_text
from bot.telegram_helpers import resolve_event_user, safe_client_edit, safe_event_edit
from bot.bridge.showdown_bridge import ShowdownBridgeError

logger = logging.getLogger("PokemonBot.game.trainer")


def display_name(user: User | None, fallback: str = "Trainer") -> str:
    if not user:
        return fallback
    value = get_display_name(user).strip()
    return value or fallback

HELP_CATEGORIES = {
    "profile": {
        "title": "Trainer & Profile",
        "short_label": "Profile",
        "commands": [
            "/start - Open your trainer profile or start your journey",
            "/starter - Reopen the starter selection menu",
            "/mycard - View your generated Trainer Card",
            "/forcecomplete - Instantly finish active timers (Admin/Debug)",
        ]
    },
    "pokemon": {
        "title": "Pokémon & Team",
        "short_label": "Pokémon",
        "commands": [
            "/mypokemons - View your Pokémon collection",
            "/myteam - Manage your active battle team",
            "/display - Change how Pokémon are displayed in lists",
            "/sort - Change your collection sorting order",
            "/stats <pokemon> - Open a Pokémon's detailed stat card",
        ]
    },
    "moves": {
        "title": "Moves & Forms",
        "short_label": "Moves",
        "commands": [
            "/listmoveid - Search for move IDs",
            "/formchange - Use fusion and form-change key items",
            "/maxsoup <pokemon> - Give an eligible Pokémon its Gmax form",
        ]
    },
    "items": {
        "title": "Items & Shop",
        "short_label": "Bag & Shop",
        "commands": [
            "/mybag - View your inventory",
            "/box - Open Trainer Box rewards",
            "/equip_item - Equip held items to your Pokémon (DM only)",
            "/shop - Open the PokéMart",
            "/buy <item> [qty] - Buy items from the shop",
            "/sell <item> [qty] - Sell Pokéballs",
            "/mochi <pokemon> - Use Mochi for EV training",
            "/candy <pokemon> - Use EXP Candies",
            "/feather <pokemon> - Use Feathers for EV training",
            "/mint <pokemon> [nature] - Use a Nature Mint from your bag",
            "/abilitypatch <pokemon> - Use Ability Patch on a Pokemon (DM only)",
            "/abilitycapsule <pokemon> - Use Ability Capsule on a Pokemon (DM only)",
            "/bottlecap <pokemon> - Max one IV with a Bottle Cap",
            "/goldbottlecap <pokemon> - Max all IVs with a Gold Bottle Cap",
        ]
    },
    "explore": {
        "title": "Exploration",
        "short_label": "Explore",
        "commands": [
            "/travel - Travel to a region and pick an area",
            "/dexnav <pokemon> - Find where specific Pokémon spawn",
            "/hunt - Search for wild Pokémon (DM only)",
            "/autohunt <count> - Simulate hunt drop/shiny report",
            "/open - Open the quick-hunt keyboard",
            "/close - Close the quick-hunt keyboard",
        ]
    },
    "safari": {
        "title": "Safari Zone",
        "short_label": "Safari",
        "commands": [
            "/safari - Open the Safari entry screen",
            "/exit - Leave the Safari or close your current battle state",
        ]
    },
    "train": {
        "title": "Training & Breeding",
        "short_label": "Train",
        "commands": [
            "/train - Enter the Training Spot to level up or alter EVs/IVs (DM only)",
            "/breed - Leave Pokémon at the Daycare (DM only)",
            "/breeddata - View egg timers and daycare status (DM only)",
            "/incubate - Instantly hatch an egg (DM only)",
            "/incubator - Open the Egg Incubator menu (DM only)",
            "/relearner <pokemon> - Relearn forgotten level-up, evolution, TM, egg, and tutor moves (DM only)",
        ]
    },
    "social": {
        "title": "Multiplayer & Social",
        "short_label": "Social",
        "commands": [
            "/challenge - Reply to a message to battle another trainer",
            "/trade - Reply to a message to offer a trade",
            "/pokechain - Create a Pokechain lobby in group",
            "/joinpc - Join the current Pokechain lobby",
            "/myfac - View your faction profile",
            "/join - Join a faction in DM",
            "/leave - Leave your faction in DM",
            "/fac_deposit <amount> - Deposit VP into faction bank",
            "/send <amount> - Reply to send Victory Points to someone",
            "/exit - Close your current PvP challenge or battle",
        ]
    }
}
POKEMON_LIST_PAGE_SIZE = 20
EQUIP_ITEM_PAGE_SIZE = 20
MAX_EV_PER_STAT = 252
MAX_TOTAL_EVS = 510
ITEM_USE_QUANTITIES = (1, 5, 10)
EQUIP_CATEGORY_ORDER = (
    ("mega", "Mega Stones"),
    ("choice", "Choice Items"),
    ("status", "Status Items"),
    ("berries", "Berries"),
    ("type", "Type Items"),
    ("other", "Other"),
)
EQUIP_CATEGORY_LABELS = {key: label for key, label in EQUIP_CATEGORY_ORDER}
EQUIP_CHOICE_ITEM_KEYS = {"choiceband", "choicescarf", "choicespecs"}
EQUIP_STATUS_ITEM_KEYS = {
    "assaultvest",
    "blacksludge",
    "clearamulet",
    "covertcloak",
    "damprock",
    "flameorb",
    "gripclaw",
    "heatrock",
    "icyrock",
    "leftovers",
    "mentalherb",
    "powerherb",
    "safetygoggles",
    "shedshell",
    "smoothrock",
    "terrainextender",
    "toxicorb",
    "utilityumbrella",
    "whiteherb",
}
EQUIP_TYPE_ITEM_KEYS = {
    "blackbelt",
    "blackglasses",
    "charcoal",
    "dragonfang",
    "hardstone",
    "magnet",
    "metalcoat",
    "miracleseed",
    "mysticwater",
    "nevermeltice",
    "poisonbarb",
    "sharpbeak",
    "silkscarf",
    "silverpowder",
    "softsand",
    "spelltag",
    "twistedspoon",
}
EQUIP_TYPE_ITEM_SUFFIXES = ("plate", "memory", "drive", "gem", "mask", "seed")
EV_STAT_ORDER = ("hp", "atk", "def", "spa", "spd", "spe")
EV_STAT_LABELS = {
    "hp": "HP",
    "atk": "Attack",
    "def": "Defense",
    "spa": "Sp. Atk",
    "spd": "Sp. Def",
    "spe": "Speed",
}
TRAINING_DURATION_OPTIONS = (
    ("2pk", "2 Pokemon", 60, 5000, 2),
    ("5pk", "5 Pokemon", 60, 10000, 5),
    ("10pk", "10 Pokemon", 60, 20000, 10),
    ("30pk", "30 Pokemon", 60, 50000, 30),
)
TRAINING_DURATION_LABELS = {key: label for key, label, _, _, _ in TRAINING_DURATION_OPTIONS}
TRAINING_DURATION_MINUTES = {key: minutes for key, _, minutes, _, _ in TRAINING_DURATION_OPTIONS}
TRAINING_DURATION_COSTS = {key: cost for key, _, _, cost, _ in TRAINING_DURATION_OPTIONS}
TRAINING_POKEMON_LIMITS = {key: limit for key, _, _, _, limit in TRAINING_DURATION_OPTIONS}
TRAINING_LEVEL_STEPS = (1, 5, 10)
TRAINING_EV_STEPS = (-10, -5, -1, 1, 5, 10)
TRAINING_MOVE_PAGE_SIZE = 12
TRAINING_NATURE_PAGE_SIZE = 12
SHOP_HELD_PAGE_SIZE = 18
FORM_CHANGE_PAGE_SIZE = 20
TM_COMPAT_PAGE_SIZE = 20
ITEM_USE_PICKER_PAGE_SIZE = 20
COMMAND_USE_SESSION_MINUTES = 10
RELEARNER_SESSION_MINUTES = 10
RELEARNER_MOVE_PAGE_SIZE = 20
ABILITY_CAPSULE_ITEM = "Ability Capsule"
ABILITY_PATCH_ITEM = "Ability Patch"
BOTTLE_CAP_ITEM = "Bottle Cap"
GOLD_BOTTLE_CAP_ITEM = "Gold Bottle Cap"
KEY_ITEM_EGG_INCUBATOR = "Egg Incubator"
KEY_ITEM_MAX_SOUP = "Max Soup"
KEY_ITEM_TRAINER_BOX = "Trainer Box"
KEY_ITEM_HOLOWEAR_TICKET = "Holowear Ticket"
TERA_TYPE_CHANGE_SHARD_COST = 50
MAX_SOUP_COST_VP = 30000
RANKUP_ADMIN_IDS = frozenset(ADMIN_USER_ID_SET)
TRAINING_NATURE_MINTS = (
    ("Lonely", "Lonely Mint"),
    ("Adamant", "Adamant Mint"),
    ("Naughty", "Naughty Mint"),
    ("Brave", "Brave Mint"),
    ("Bold", "Bold Mint"),
    ("Impish", "Impish Mint"),
    ("Lax", "Lax Mint"),
    ("Relaxed", "Relaxed Mint"),
    ("Modest", "Modest Mint"),
    ("Mild", "Mild Mint"),
    ("Rash", "Rash Mint"),
    ("Quiet", "Quiet Mint"),
    ("Calm", "Calm Mint"),
    ("Gentle", "Gentle Mint"),
    ("Careful", "Careful Mint"),
    ("Sassy", "Sassy Mint"),
    ("Timid", "Timid Mint"),
    ("Hasty", "Hasty Mint"),
    ("Jolly", "Jolly Mint"),
    ("Naive", "Naive Mint"),
    ("Serious", "Serious Mint"),
)
KEY_ITEM_DAYCARE_CANDY = "Daycare Candy"
KEY_ITEM_DYNAMAX_CANDY = "Dynamax Candy"
KEY_ITEM_OMNI_RING = "Omni Ring"
MINT_NATURE_LOOKUP = {
    normalize_lookup(nature_name): nature_name
    for nature_name, _item_name in TRAINING_NATURE_MINTS
}
DAYCARE_LOOP_SECONDS = 5
HUNT_EGG_ODDS = 30000
EGG_INCUBATOR_COST = 20000
HATCH_ACCELERATOR_ABILITIES = {
    "flamebody",
    "magmaarmor",
    "steamengine",
}
DAYCARE_SPECIAL_OFFSPRING = {
    ("manaphy", "ditto"): "Phione",
    ("ditto", "manaphy"): "Phione",
    ("phione", "ditto"): "Phione",
    ("ditto", "phione"): "Phione",
}
DAYCARE_COUNTERPARTS = {
    "nidoran-f": ("Nidoran-F", "Nidoran-M"),
    "nidoran-m": ("Nidoran-F", "Nidoran-M"),
    "nidorina": ("Nidoran-F", "Nidoran-M"),
    "nidorino": ("Nidoran-F", "Nidoran-M"),
    "nidoqueen": ("Nidoran-F", "Nidoran-M"),
    "nidoking": ("Nidoran-F", "Nidoran-M"),
    "illumise": ("Volbeat", "Illumise"),
    "volbeat": ("Volbeat", "Illumise"),
}
POKECHAIN_LOBBY_TIMEOUT_SECONDS = 120
POKECHAIN_INITIAL_TURN_SECONDS = 45
POKECHAIN_MIN_TURN_SECONDS = 20
POKECHAIN_TURN_REDUCTION_STEP = 5
POKECHAIN_TURN_REDUCTION_GUESSES = 5
POKECHAIN_WIN_VP = 1000
POKECHAIN_WIN_LP = 100
POKECHAIN_DAILY_REWARD_LIMIT = 5
NICKNAME_PAGE_SIZE = 25
ADMIN_COMMAND_LOCK_MESSAGE = "This admin command is temporarily locked while we stabilize the bot."
GYM_COMMAND_LOCK_MESSAGE = "Gym battles are temporarily locked while we stabilize the bot."
USER_COMMAND_LOCK_MESSAGE = "This command is locked by admins."
DM_LAUNCH_COMMAND_LABELS = {
    "breed": "Breed",
    "breeddata": "Breeddata",
    "incubate": "Incubate",
    "train": "Train",
    "equip_item": "Equip Item",
    "top": "Top",
}


def chunk_buttons(buttons: list[Button], *, per_row: int) -> list[list[Button]]:
    return [buttons[index:index + per_row] for index in range(0, len(buttons), per_row)]


def paginate_items(items: list, *, page: int, per_page: int) -> tuple[list, int, int]:
    total = len(items)
    if total <= 0:
        return [], 0, 0
    max_page = (total - 1) // per_page
    current_page = min(max(page, 0), max_page)
    start = current_page * per_page
    end = start + per_page
    return items[start:end], total, current_page


async def respond_locked(event, message: str) -> None:
    await event.respond(message)


def equip_item_category(item_name: str) -> str:
    lowered = str(item_name).strip().lower()
    item_key = normalize_lookup(item_name)
    if (
        item_key in {"redorb", "blueorb"}
        or (
            lowered.endswith("ite")
            or lowered.endswith("ite x")
            or lowered.endswith("ite y")
            or lowered.endswith("ite z")
        )
    ) and item_key != "eviolite":
        return "mega"
    if item_key in EQUIP_CHOICE_ITEM_KEYS:
        return "choice"
    if lowered.endswith("berry"):
        return "berries"
    if item_key in EQUIP_STATUS_ITEM_KEYS:
        return "status"
    if item_key in EQUIP_TYPE_ITEM_KEYS or any(item_key.endswith(suffix) for suffix in EQUIP_TYPE_ITEM_SUFFIXES):
        return "type"
    return "other"


@dataclass
class TradeSession:
    trade_id: str
    chat_id: int
    public_message_id: int
    requester_id: int
    requester_name: str
    target_id: int
    target_name: str
    state: str = "pending"
    selected_by: dict[int, int | None] = field(default_factory=dict)
    accepted_by: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class TrainingSession:
    session_id: str
    user_id: int
    duration_key: str
    duration_label: str
    cost_vp: int
    allowed_pokemon: int
    started_at: datetime
    expires_at: datetime
    trained_pokemon_ids: set[int] = field(default_factory=set)


@dataclass
class CommandUseSession:
    session_id: str
    owner_id: int
    action: str
    query: str
    requested_nature: str | None
    expires_at: datetime


@dataclass
class RelearnerSession:
    session_id: str
    owner_id: int
    query: str
    pokemon_ids: list[int]
    expires_at: datetime


@dataclass
class NicknameSession:
    owner_id: int
    pokemon_id: int
    page: int
    expires_at: datetime


@dataclass
class PokechainSession:
    chat_id: int
    host_id: int
    status: str = "lobby"
    players: list[int] = field(default_factory=list)
    player_names: dict[int, str] = field(default_factory=dict)
    turn_index: int = 0
    used_names: set[str] = field(default_factory=set)
    used_lines: set[str] = field(default_factory=set)
    guess_count: int = 0
    time_per_turn: int = POKECHAIN_INITIAL_TURN_SECONDS
    deadline_ts: float = 0.0
    lobby_task: asyncio.Task | None = None
    turn_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DirectMessageProxyEvent:
    def __init__(self, *, client, sender: User | None, sender_id: int, raw_text: str) -> None:
        self._client = client
        self._sender = sender
        self.sender_id = int(sender_id)
        self.raw_text = str(raw_text)
        self.is_private = True
        self.chat_id = int(sender_id)

    async def get_sender(self) -> User | None:
        return self._sender

    async def respond(self, text: str, *, buttons=None, parse_mode=None, link_preview: bool | None = None) -> None:
        await self._client.send_message(
            self.chat_id,
            text,
            buttons=buttons,
            parse_mode=parse_mode,
            link_preview=bool(link_preview),
        )

    async def reply(self, text: str, *, buttons=None, parse_mode=None, link_preview: bool | None = None) -> None:
        await self.respond(text, buttons=buttons, parse_mode=parse_mode, link_preview=link_preview)


class TrainerGameService:
    def __init__(self, generator: PokemonGeneratorService, battle_service) -> None:
        self.battle_service = battle_service
        self.generator = generator
        self.pokemon_data = PokemonDataService()
        self.regions = json.loads(Path(REGIONS_PATH).read_text(encoding="utf-8"))["regions"]
        self.starter_regions = json.loads(Path(STARTERS_PATH).read_text(encoding="utf-8"))["regions"]
        self.team_manager = TeamManagerService(self.pokemon_data)
        self.encounters = EncounterService(generator, battle_service, self.pokemon_data)
        self.stats = PokemonStatsService(self.pokemon_data, generator, battle_service)
        self.stats.attach_encounter_service(self.encounters)
        self.daycare = DaycareService(generator, battle_service, self.pokemon_data, self.encounters)
        self.factions = FactionService(battle_service.client)
        self._held_item_catalog: list[str] | None = None
        self.trade_sessions: dict[str, TradeSession] = {}
        self.trade_by_user: dict[int, str] = {}
        self.training_sessions: dict[int, TrainingSession] = {}
        self.command_use_sessions: dict[str, CommandUseSession] = {}
        self.relearner_sessions: dict[str, RelearnerSession] = {}
        self.nickname_sessions: dict[int, NicknameSession] = {}
        self._training_move_catalog: dict[str, list[dict[str, object]]] = {}
        self._training_levelup_move_catalog: dict[str, list[dict[str, object]]] = {}
        self._training_ability_catalog: dict[str, list[dict[str, object]]] = {}
        self._breeding_profile_cache: dict[str, dict[str, Any]] = {}
        self._bot_dm_url: str | None = None
        self._service_started_at = datetime.utcnow()
        self.box_selection_by_user: dict[int, int] = {}
        self.pokechain_games: dict[int, PokechainSession] = {}
        self._pokechain_allowed_names, self._pokechain_display_names = self._load_pokechain_names()
        self._pokechain_line_map = self._build_pokechain_line_map()
        self._tm_drop_map = self._load_tm_drop_map()
        self._stone_data = self._load_stone_data()
        if hasattr(self.battle_service, "register_exit_cleanup_handler"):
            self.battle_service.register_exit_cleanup_handler(self._clear_trade_state_on_exit)

    def _is_admin_user(self, user_id: int | None) -> bool:
        return int(user_id or 0) in RANKUP_ADMIN_IDS

    async def _require_admin(self, event: NewMessage.Event) -> bool:
        if self._is_admin_user(event.sender_id):
            return True
        await event.respond("Access denied.")
        return False

    def _telegram_command_specs(self) -> list[tuple[str, str]]:
        return [
            ("start", "Open your trainer profile"),
            ("help", "Show command help"),
            ("starter", "Choose your starter again"),
            ("mycard", "View your trainer card"),
            ("mypokemons", "View your Pokemon collection"),
            ("nickname", "Give a nickname to a Pokemon"),
            ("mynicknames", "List your nicknamed Pokemon"),
            ("myteam", "Manage your active team"),
            ("mybag", "Open your inventory"),
            ("box", "Open Trainer Box rewards"),
            ("stats", "View a Pokemon stat card"),
            ("travel", "Travel to another region"),
            ("dexnav", "Find where a Pokemon spawns"),
            ("hunt", "Search for wild Pokemon"),
            ("autohunt", "Run multiple hunts quickly"),
            ("open", "Open the quick-hunt keyboard"),
            ("close", "Close the quick-hunt keyboard"),
            ("safari", "Open the Safari menu"),
            ("exit", "Leave Safari or close active battle"),
            ("train", "Open the training spot"),
            ("breed", "Use the daycare"),
            ("breeddata", "View daycare timers"),
            ("incubate", "Hatch an egg instantly"),
            ("incubator", "Open the egg incubator"),
            ("shop", "Open the Pokemart"),
            ("buy", "Buy items from the shop"),
            ("sell", "Sell Pokeballs"),
            ("challenge", "Challenge another trainer"),
            ("trade", "Trade with another trainer"),
            ("pokechain", "Start a Pokechain lobby"),
            ("joinpc", "Join the current Pokechain lobby"),
            ("myfac", "View your faction profile"),
        ]

    async def _handle_admin_sync_commands(self, event: NewMessage.Event) -> None:
        commands = [BotCommand(command=name, description=description) for name, description in self._telegram_command_specs()]
        await self.battle_service.client(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=commands,
            )
        )
        await event.respond(
            f"Updated Telegram bot commands successfully.\nCommands synced: `{len(commands)}`",
            parse_mode="md",
        )

    async def _track_group_chat(self, chat_id: int | None, *, title: str | None = None) -> None:
        if chat_id is None:
            return
        value = int(chat_id)
        if value >= 0:
            return
        await run_db_work_async(
            lambda session: AdminRepository(session).track_group_chat(value, title=title),
            read_only=False,
        )

    async def _reply_target_user(self, event: NewMessage.Event) -> User | None:
        if not event.is_reply:
            return None
        reply_message = await event.get_reply_message()
        if reply_message is None:
            return None
        target_user = reply_message.sender if isinstance(reply_message.sender, User) else await reply_message.get_sender()
        if not isinstance(target_user, User) or getattr(target_user, "bot", False):
            return None
        return target_user

    def _extract_integer_arguments(self, text: str) -> list[int]:
        return [int(value) for value in re.findall(r"-?\d+", str(text or ""))]

    def _admin_runtime_lock_reason(self, user_id: int) -> str | None:
        reason = self.battle_service.pvp_lock_reason(user_id)
        if reason:
            return reason
        reason = self.battle_service.encounter_lock_reason(user_id)
        if reason:
            return reason
        trade_id = self.trade_by_user.get(user_id)
        if trade_id and trade_id in self.trade_sessions:
            return "The target user is in an active trade."
        training = self.training_sessions.get(user_id)
        if training is not None and training.expires_at > datetime.utcnow():
            return "The target user has an active training session."
        return None

    async def _is_banned_user_id(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        value = int(user_id)
        if value in ADMIN_USER_ID_SET:
            return False
        return await run_db_work_async(
            lambda session: AdminRepository(session).is_banned_user(value),
            read_only=True,
        )

    def start_background_tasks(self) -> None:
        self.encounters.start_background_tasks()
        self.daycare.start_background_tasks()

    def _pokechain_turn_seconds(self, guess_count: int) -> int:
        step = max(0, int(guess_count) // POKECHAIN_TURN_REDUCTION_GUESSES)
        return max(POKECHAIN_MIN_TURN_SECONDS, POKECHAIN_INITIAL_TURN_SECONDS - (step * POKECHAIN_TURN_REDUCTION_STEP))

    def _load_pokechain_names(self) -> tuple[set[str], dict[str, str]]:
        names: list[str] = []
        if Path(POKECHAIN_PATH).exists():
            try:
                raw = json.loads(Path(POKECHAIN_PATH).read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    names = [str(name) for name in raw]
                elif isinstance(raw, dict) and isinstance(raw.get("names"), list):
                    names = [str(name) for name in raw["names"]]
            except Exception:
                names = []

        if not names:
            names = [str(payload.get("name") or key) for key, payload in self.pokemon_data.species_reference.items()]

        allowed: set[str] = set()
        display: dict[str, str] = {}
        for raw_name in names:
            key = species_key(raw_name)
            if not key:
                continue
            allowed.add(key)
            display.setdefault(key, str(raw_name).strip() or key.title())
        return allowed, display

    def _build_pokechain_line_map(self) -> dict[str, str]:
        if not Path(EVOLUTION_CHAINS_PATH).exists():
            return {}
        try:
            payload = json.loads(Path(EVOLUTION_CHAINS_PATH).read_text(encoding="utf-8"))
        except Exception:
            return {}
        rows = payload.get("evolution_chains", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return {}

        graph: dict[str, set[str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            left = species_key(str(row.get("current_pokemon") or ""))
            right = species_key(str(row.get("evolved_pokemon") or ""))
            if not left or not right:
                continue
            graph.setdefault(left, set()).add(right)
            graph.setdefault(right, set()).add(left)

        line_map: dict[str, str] = {}
        line_index = 1
        for node in graph:
            if node in line_map:
                continue
            line_id = f"line-{line_index}"
            line_index += 1
            stack = [node]
            line_map[node] = line_id
            while stack:
                current = stack.pop()
                for nxt in graph.get(current, set()):
                    if nxt in line_map:
                        continue
                    line_map[nxt] = line_id
                    stack.append(nxt)
        return line_map

    def _pokechain_line_id(self, name_key: str) -> str:
        return self._pokechain_line_map.get(name_key, name_key)

    def _pokechain_cancel_task(self, task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    async def _pokechain_send(self, chat_id: int, text: str, *, parse_mode: str = "html") -> None:
        await self.battle_service.client.send_message(chat_id, text, parse_mode=parse_mode, link_preview=False)

    def _pokechain_player_name(self, game: PokechainSession, user_id: int) -> str:
        return game.player_names.get(user_id, f"Player {user_id}")

    def _pokechain_mention(self, game: PokechainSession, user_id: int) -> str:
        label = html.escape(self._pokechain_player_name(game, user_id))
        return f'<a href="tg://user?id={int(user_id)}"><b>{label}</b></a>'

    async def _pokechain_finish_and_cleanup(self, game: PokechainSession) -> None:
        self._pokechain_cancel_task(game.turn_task)
        self._pokechain_cancel_task(game.lobby_task)
        self.pokechain_games.pop(game.chat_id, None)

    async def _pokechain_reward_winner(self, winner_id: int) -> tuple[bool, int]:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        rewarded = False
        reward_count = 0
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.get_by_telegram_user_id(winner_id)
            if trainer is None or trainer.inventory is None:
                return False, 0
            state = self._shop_state(trainer)
            raw_data = state.get("pokechain_rewards", {})
            if not isinstance(raw_data, dict):
                raw_data = {}
            if str(raw_data.get("date") or "") != today:
                raw_data = {"date": today, "count": 0}

            reward_count = max(0, int(raw_data.get("count") or 0))
            if reward_count < POKECHAIN_DAILY_REWARD_LIMIT:
                inventories.add_victory_points(trainer, POKECHAIN_WIN_VP)
                inventories.add_league_points(trainer, POKECHAIN_WIN_LP)
                reward_count += 1
                rewarded = True

            state["pokechain_rewards"] = {"date": today, "count": reward_count}
            self._store_shop_state(trainer, state)
        return rewarded, reward_count

    async def _pokechain_set_lobby_timer(self, game: PokechainSession) -> None:
        async def _runner() -> None:
            try:
                await asyncio.sleep(POKECHAIN_LOBBY_TIMEOUT_SECONDS)
                current = self.pokechain_games.get(game.chat_id)
                if current is None or current is not game:
                    return
                async with game.lock:
                    if game.status != "lobby":
                        return
                    if len(game.players) < 2:
                        await self._pokechain_send(
                            game.chat_id,
                            "• <b>Pokechain</b>\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            "Lobby closed. Need at least 2 players.",
                        )
                        await self._pokechain_finish_and_cleanup(game)
                        return
                    await self._pokechain_send(
                        game.chat_id,
                        "• <b>Pokechain</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "Lobby auto-started after 120 seconds.",
                    )
                    await self._pokechain_start_game_locked(game)
            except asyncio.CancelledError:
                return

        self._pokechain_cancel_task(game.lobby_task)
        game.lobby_task = asyncio.create_task(_runner())

    async def _pokechain_set_turn_timer(self, game: PokechainSession) -> None:
        async def _runner() -> None:
            try:
                await asyncio.sleep(max(1, int(game.time_per_turn)))
                current = self.pokechain_games.get(game.chat_id)
                if current is None or current is not game:
                    return
                async with game.lock:
                    if game.status != "active" or not game.players:
                        return
                    current_player = game.players[game.turn_index]
                    await self._pokechain_eliminate_player_locked(game, current_player, "Time is up.")
            except asyncio.CancelledError:
                return

        self._pokechain_cancel_task(game.turn_task)
        game.deadline_ts = asyncio.get_running_loop().time() + float(max(1, int(game.time_per_turn)))
        game.turn_task = asyncio.create_task(_runner())

    async def _pokechain_announce_turn_locked(self, game: PokechainSession) -> None:
        if not game.players:
            return
        player_id = game.players[game.turn_index]
        name = self._pokechain_mention(game, player_id)
        await self._pokechain_send(
            game.chat_id,
            "• <b>Pokechain - Turn</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Player: {name}\n"
            f"Time: <b>{game.time_per_turn}s</b>\n"
            "Send a Pokemon name.",
        )

    async def _pokechain_start_game_locked(self, game: PokechainSession) -> bool:
        if len(game.players) < 2:
            await self._pokechain_send(game.chat_id, "Need at least 2 players to start.")
            return False
        self._pokechain_cancel_task(game.lobby_task)
        game.lobby_task = None
        game.status = "active"
        game.turn_index = 0
        game.used_names.clear()
        game.used_lines.clear()
        game.guess_count = 0
        game.time_per_turn = self._pokechain_turn_seconds(0)
        first_id = game.players[game.turn_index]
        first_name = self._pokechain_mention(game, first_id)
        await self._pokechain_send(
            game.chat_id,
            "• <b>Pokechain Started</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Rules: valid Pokemon names only.\n"
            f"First turn: {first_name}\n"
            f"Time: <b>{game.time_per_turn}s</b>",
        )
        await self._pokechain_set_turn_timer(game)
        return True

    async def _pokechain_eliminate_player_locked(self, game: PokechainSession, user_id: int, reason: str) -> None:
        if user_id not in game.players:
            return
        index = game.players.index(user_id)
        name = self._pokechain_mention(game, user_id)
        game.players.pop(index)

        if index < game.turn_index:
            game.turn_index -= 1
        if game.turn_index >= len(game.players):
            game.turn_index = 0

        await self._pokechain_send(
            game.chat_id,
            "• <b>Pokechain</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{name} is out. {html.escape(reason)}",
        )

        if not game.players:
            await self._pokechain_send(game.chat_id, "No winner this time.")
            await self._pokechain_finish_and_cleanup(game)
            return

        if len(game.players) == 1:
            winner_id = game.players[0]
            winner_name = self._pokechain_mention(game, winner_id)
            rewarded, count = await self._pokechain_reward_winner(winner_id)
            if rewarded:
                reward_line = (
                    f"Rewards: +{POKECHAIN_WIN_VP:,} PokeCoins, +{POKECHAIN_WIN_LP:,} League Points "
                    f"({count}/{POKECHAIN_DAILY_REWARD_LIMIT} today)"
                )
            else:
                reward_line = f"Reward limit reached for today ({POKECHAIN_DAILY_REWARD_LIMIT}/{POKECHAIN_DAILY_REWARD_LIMIT})."
            await self._pokechain_send(
                game.chat_id,
                "• <b>Pokechain Winner</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{winner_name}\n{html.escape(reward_line)}",
            )
            await self._pokechain_finish_and_cleanup(game)
            return

        await self._pokechain_set_turn_timer(game)
        await self._pokechain_announce_turn_locked(game)

    async def _bot_dm_link(self, start: str | None = None) -> str | None:
        if self._bot_dm_url is None:
            try:
                me = await self.battle_service.client.get_me()
            except Exception:
                return None
            username = str(getattr(me, "username", "") or "").strip()
            if not username:
                return None
            self._bot_dm_url = f"https://t.me/{username}"
        if start:
            return f"{self._bot_dm_url}?start={quote(start, safe='')}"
        return self._bot_dm_url

    async def _reply_dm_only(self, event: NewMessage.Event) -> None:
        url = await self._bot_dm_link()
        buttons = [[Button.url("Open Bot", url)]] if url else None
        await event.reply("This command is for dm only", buttons=buttons)

    def _normalize_lockable_command_name(self, command_name: str) -> str | None:
        aliases = {
            "abilitypatch": "abilitypatch",
            "ability_patch": "abilitypatch",
            "abilitycapsule": "abilitycapsule",
            "ability_capsule": "abilitycapsule",
            "breed": "breed",
            "breeddata": "breeddata",
            "incubate": "incubate",
            "incubator": "incubate",
            "train": "train",
            "equipitem": "equip_item",
            "equipitems": "equip_item",
            "equip_item": "equip_item",
            "equip_items": "equip_item",
            "relearn": "relearner",
            "relearner": "relearner",
            "top": "top",
            "redeem": "redeem",
        }
        return aliases.get(normalize_lookup(command_name))

    async def _is_command_locked(self, command_name: str) -> bool:
        normalized = self._normalize_lockable_command_name(command_name)
        if not normalized:
            return False
        return await run_db_work_async(
            lambda session: CommandLockRepository(session).is_locked(normalized),
            read_only=True,
        )

    async def _ensure_command_unlocked(self, event: NewMessage.Event, command_name: str) -> bool:
        if self._is_admin_user(getattr(event, "sender_id", None)):
            return True
        if await self._is_command_locked(command_name):
            await event.respond(USER_COMMAND_LOCK_MESSAGE)
            return False
        return True

    def _dm_launch_button(self, command_name: str) -> list[list[Button]]:
        return [[Button.inline(command_name, data=f"dmcmd:open:{command_name}".encode("utf-8"))]]

    async def _reply_dm_command_button(self, event: NewMessage.Event, command_name: str) -> None:
        label = DM_LAUNCH_COMMAND_LABELS.get(command_name, command_name.replace("_", " ").title())
        await event.respond(
            f"Use {label} In Bot DM",
            buttons=self._dm_launch_button(command_name),
        )

    def _dm_command_start_payload(self, command_name: str) -> str:
        normalized = self._normalize_lockable_command_name(command_name)
        if normalized is None:
            raise ValueError("unknown_dm_command")
        return f"dmcmd_{normalized}"

    async def _resolve_dm_command_name_for_sender(self, sender: User | None, command_name: str) -> str:
        normalized = self._normalize_lockable_command_name(command_name)
        if normalized is None:
            raise ValueError("unknown_dm_command")
        sender_id = int(getattr(sender, "id", 0) or 0)
        if not self._is_admin_user(sender_id) and await self._is_command_locked(normalized):
            raise ValueError("locked_dm_command")
        return normalized

    async def _dispatch_dm_command(self, sender: User | None, *, command_name: str) -> None:
        normalized = await self._resolve_dm_command_name_for_sender(sender, command_name)
        sender_id = int(getattr(sender, "id", 0) or 0)

        proxy = DirectMessageProxyEvent(
            client=self.battle_service.client,
            sender=sender,
            sender_id=sender_id,
            raw_text=f"/{normalized}",
        )
        dispatch_map = {
            "breed": self.on_breed,
            "breeddata": self.on_breeddata,
            "incubate": self.on_incubate,
            "train": self.on_train,
            "equip_item": self.on_equip_items,
            "top": self.on_top,
        }
        handler = dispatch_map.get(normalized)
        if handler is None:
            raise ValueError("unknown_dm_command")
        await handler(proxy)

    async def _handle_dm_command_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) != 3 or parts[1] != "open":
            await event.answer("Unknown action.", alert=True)
            return
        sender = await event.get_sender()
        if not isinstance(sender, User):
            await event.answer("Unable to open the bot DM right now.", alert=True)
            return
        try:
            normalized = await self._resolve_dm_command_name_for_sender(sender, parts[2])
        except ValueError as exc:
            if str(exc) == "locked_dm_command":
                await event.answer(USER_COMMAND_LOCK_MESSAGE, alert=True)
                return
            await event.answer("Unknown action.", alert=True)
            return
        url = await self._bot_dm_link(start=self._dm_command_start_payload(normalized))
        if url:
            await event.answer(url=url)
            return
        try:
            await self._dispatch_dm_command(sender, command_name=normalized)
        except Exception:
            fallback_url = await self._bot_dm_link()
            if fallback_url:
                await self.battle_service.client.send_message(
                    event.chat_id,
                    "Open the bot DM and try again there.",
                    buttons=[[Button.url("Open Bot", fallback_url)]],
                )
            await event.answer("Could not open the bot DM.", alert=True)
            return
        await event.answer("Opened in bot DM.")

    def bag_buttons(self, current_category: str = "overview", page: int = 0, max_page: int = 0) -> list[list[Button]]:
        options = [
            ("balls", "Poké Balls"),
            ("held", "Held Items"),
            ("tms", "TMs"),
            ("medicine", "Medicine"),
            ("key", "Key Items"),
            ("overview", "Overview"),
        ]
        
        category_buttons = [
            Button.inline(label, data=f"bag:{category}:0".encode("utf-8"))
            for category, label in options
            if category != current_category
        ]
        
        rows = chunk_buttons(category_buttons, per_row=2)
        
        # Add Pagination Arrows if needed
        if max_page > 0:
            nav_row = []
            if page > 0:
                nav_row.append(Button.inline("<-", data=f"bag:{current_category}:{page - 1}".encode("utf-8")))
            nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="bag:noop".encode("utf-8")))
            if page < max_page:
                nav_row.append(Button.inline("->", data=f"bag:{current_category}:{page + 1}".encode("utf-8")))
            rows.append(nav_row)
            
        return rows if rows else None

    def shop_buttons(
        self,
        current_category: str = "balls",
        *,
        page: int = 0,
        max_page: int = 0,
        trainer=None,
    ) -> list[list[Button]] | None:
        # Removed emojis for a cleaner, text-based look
        options = [
            ("balls", "Balls"),
            ("medicine", "Medicine"),
            ("held", "Held Items"),
            ("battle", "Battle Items"),
            ("key", "Key Items"),
        ]
        rows: list[list[Button]] = []
        
        if current_category == "battle" and trainer is not None:
            rotation = self._battle_item_rotation(int(trainer.telegram_user_id))
            purchased = self._battle_shop_purchased_keys(trainer)
            
            battle_buttons: list[Button] = []
            special_labels = {
                "mega": "Mega Stone",
                "zcrystal": "Z-Crystal",
                "terashard": "Tera Shard",
            }
            
            for entry in rotation:
                purchase_key = self._battle_shop_purchase_key(entry)
                bought = purchase_key in purchased
                kind = str(entry.get("kind") or "")
                
                if kind == "tm":
                    base_label = str(entry.get("name") or "").split(" - ", 1)[0] or "TM"
                else:
                    base_label = special_labels.get(kind, str(entry.get("display_name") or kind.title()))
                    
                # Clean text-only indicator for sold items
                button_label = f"{base_label} [Sold]" if bought else base_label
                
                battle_buttons.append(
                    Button.inline(
                        button_label,
                        data=("shop:noop" if bought else f"shop:buy:battle:{purchase_key}").encode("utf-8"),
                    )
                )
                
            # Chunk the battle items to exactly 2 per row so they don't break on mobile
            if battle_buttons:
                rows.extend(chunk_buttons(battle_buttons, per_row=2))
                
        # Split categories into 2 per row
        category_rows = chunk_buttons(
            [
                Button.inline(label, data=f"shop:page:{category}:0".encode("utf-8"))
                for category, label in options
                if category != current_category
            ],
            per_row=2, 
        )
        rows.extend(category_rows)
        
        # Pagination
        if max_page > 0:
            nav_row: list[Button] = []
            if page > 0:
                nav_row.append(Button.inline("Prev", data=f"shop:page:{current_category}:{page - 1}".encode("utf-8")))
            nav_row.append(Button.inline(f"Page {page + 1}/{max_page + 1}", data="shop:noop".encode("utf-8")))
            if page < max_page:
                nav_row.append(Button.inline("Next", data=f"shop:page:{current_category}:{page + 1}".encode("utf-8")))
            rows.append(nav_row)
            
        return rows or None

    def _shop_move_label(self, move_text: str) -> str:
        parts = [part for part in str(move_text).replace("-", " ").split() if part]
        return " ".join(part.upper() if len(part) == 1 else part.capitalize() for part in parts)

    def _weekly_shop_rng(self, user_id: int) -> random.Random:
        return random.Random(f"shop:{user_id}:{self._weekly_shop_key()}")

    def _weekly_shop_reset_at(self) -> datetime:
        current = datetime.utcnow()
        start_of_day = datetime(current.year, current.month, current.day)
        days_until_reset = 8 - current.isoweekday()
        return start_of_day + timedelta(days=days_until_reset)

    def _weekly_shop_key(self) -> str:
        current = datetime.utcnow()
        iso_year, iso_week, _iso_weekday = current.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    def _shop_state(self, trainer) -> dict[str, Any]:
        raw = getattr(trainer, "shop_state_json", None)
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _store_shop_state(self, trainer, state: dict[str, Any]) -> None:
        cleaned = {key: value for key, value in state.items() if value not in (None, [], {}, "")}
        trainer.shop_state_json = json.dumps(cleaned, sort_keys=True) if cleaned else None

    def _battle_shop_purchase_key(self, entry: dict[str, Any]) -> str:
        return f"{normalize_lookup(str(entry.get('kind') or 'item'))}-{normalize_lookup(str(entry.get('name') or ''))}"

    def _battle_shop_purchased_keys(self, trainer) -> set[str]:
        state = self._shop_state(trainer)
        if str(state.get("battle_shop_week") or "") != self._weekly_shop_key():
            return set()
        return {
            str(value)
            for value in (state.get("battle_shop_purchased") or [])
            if str(value).strip()
        }

    def _battle_shop_is_purchased(self, trainer, entry: dict[str, Any]) -> bool:
        return self._battle_shop_purchase_key(entry) in self._battle_shop_purchased_keys(trainer)

    def _mark_battle_shop_purchased(self, trainer, entry: dict[str, Any]) -> None:
        state = self._shop_state(trainer)
        week_key = self._weekly_shop_key()
        purchased = self._battle_shop_purchased_keys(trainer)
        purchased.add(self._battle_shop_purchase_key(entry))
        state["battle_shop_week"] = week_key
        state["battle_shop_purchased"] = sorted(purchased)
        self._store_shop_state(trainer, state)

    def _battle_shop_offer_by_purchase_key(self, user_id: int, purchase_key: str) -> dict[str, Any] | None:
        for entry in self._battle_item_rotation(user_id):
            if self._battle_shop_purchase_key(entry) == purchase_key:
                return dict(entry)
        return None

    def _battle_tm_price(self, tm_name: str) -> int:
        move_text = str(tm_name).split(" - ", 1)[1] if " - " in str(tm_name) else str(tm_name)
        move_info = self.pokemon_data.move_info.get(normalize_lookup(move_text), {})
        power_value = move_info.get("power")
        category = str(move_info.get("category") or "").lower()
        move_key = normalize_lookup(move_text)
        top_tier = {
            "earthquake",
            "closecombat",
            "stealthrock",
            "spikes",
            "protect",
            "substitute",
            "swordsdance",
            "nastyplot",
            "dragondance",
            "trickroom",
            "thunderbolt",
            "icebeam",
            "surf",
            "uturn",
            "voltswitch",
        }
        high_tier = {
            "calmmind",
            "bulkup",
            "roost",
            "taunt",
            "willowisp",
            "thunderwave",
            "shadowball",
            "outrage",
            "drainpunch",
            "aurasphere",
            "hydropump",
            "fireblast",
            "thunder",
            "blizzard",
        }
        if move_key in top_tier:
            return 5000
        if move_key in high_tier:
            return 4500
        try:
            power = int(power_value)
        except (TypeError, ValueError):
            power = 0
        if category == "status":
            if move_key in {"toxic", "defog", "allyswitch", "reflect", "lightscreen", "haze"}:
                return 4000
            return 3000
        if power >= 110:
            return 5000
        if power >= 95:
            return 4500
        if power >= 75:
            return 3500
        return 2500

    def _held_item_shop_price(self, item_name: str) -> int:
        item_key = normalize_lookup(item_name)
        lowered = str(item_name).strip().lower()
        top_tier = {
            "assaultvest",
            "choiceband",
            "choicescarf",
            "choicespecs",
            "clearamulet",
            "covertcloak",
            "eviolite",
            "focussash",
            "heavydutyboots",
            "leftovers",
            "lifeorb",
            "lightclay",
            "rockyhelmet",
            "scopelens",
            "throatspray",
            "weaknesspolicy",
        }
        high_tier = {
            "blacksludge",
            "boosterenergy",
            "expertbelt",
            "flameorb",
            "gripclaw",
            "mentalherb",
            "mirrorherb",
            "muscleband",
            "powerherb",
            "safetygoggles",
            "terrainextender",
            "toxicorb",
            "whiteherb",
            "wiseglasses",
        }
        if item_key in top_tier:
            return 4000
        if item_key in high_tier:
            return 3500
        if lowered.endswith("berry"):
            return 1500
        if any(item_key.endswith(suffix) for suffix in ("plate", "memory", "drive", "gem", "seed")):
            return 2000
        if item_key.endswith("band") or item_key.endswith("glasses") or item_key.endswith("lens"):
            return 3000
        if item_key.endswith("orb") or item_key.endswith("herb"):
            return 2500
        return 1000

    async def _held_item_shop_catalog(self) -> list[str]:
        excluded = {
            normalize_lookup(item_name)
            for item_name in (
                set(MEGA_STONE_LOCATIONS)
                | set(Z_CRYSTALS)
                | set(TERA_SHARDS)
                | {
                    KEY_ITEM_EGG_INCUBATOR,
                    KEY_ITEM_MAX_SOUP,
                    BOTTLE_CAP_ITEM,
                    GOLD_BOTTLE_CAP_ITEM,
                    *FORM_CHANGE_ITEM_ORDER,
                }
            )
        }
        entries = [
            item_name
            for item_name in await self._held_item_catalog_entries()
            if normalize_lookup(item_name) not in excluded
            and normalize_lookup(item_name) not in {"blueorb", "redorb"}
            and not (
                str(item_name).strip().lower().endswith(("ite", "ite x", "ite y", "ite z"))
                and normalize_lookup(item_name) != "eviolite"
            )
            and not str(item_name).strip().lower().endswith(" z")
        ]
        return sorted(entries, key=str.lower)

    def _battle_item_rotation(self, user_id: int) -> list[dict[str, Any]]:
        rng = self._weekly_shop_rng(user_id)
        tm_stock = rng.sample(list(TM_DROPS), k=min(4, len(TM_DROPS)))
        mega_stock = rng.choice(sorted(MEGA_STONE_LOCATIONS))
        z_stock = rng.choice(list(Z_CRYSTALS))
        tera_stock = rng.choice(list(TERA_SHARDS))
        return [
            *[
                {
                    "kind": "tm",
                    "name": tm_name,
                    "display_name": f"{tm_name.split(' - ', 1)[0]} - {self._shop_move_label(tm_name.split(' - ', 1)[1])}",
                    "price": self._battle_tm_price(tm_name),
                    "currency": "lp",
                    "amount": 1,
                }
                for tm_name in tm_stock
            ],
            {
                "kind": "mega",
                "name": mega_stock,
                "display_name": mega_stock,
                "price": 120000,
                "currency": "lp",
                "amount": 1,
            },
            {
                "kind": "zcrystal",
                "name": z_stock,
                "display_name": z_stock,
                "price": 100000,
                "currency": "lp",
                "amount": 1,
            },
            {
                "kind": "terashard",
                "name": tera_stock,
                "display_name": f"{tera_stock} Bundle (15x)",
                "price": 85000,
                "currency": "lp",
                "amount": 15,
            },
        ]

    async def shop_text(
        self,
        trainer,
        inventories: InventoryRepository,
        *,
        category: str,
        page: int = 0,
    ) -> tuple[str, int, int]:
        vp = int(trainer.inventory.victory_points if trainer.inventory else 0)
        lp = int(getattr(trainer.inventory, "league_points", 0) or 0)

        # Helper to generate the standard footer
        def _footer(current_page: int = 0, total_pages: int = 0) -> list[str]:
            footer_lines = ["", "━━━━━━━━━━━━━━━━━━━━━"]
            if total_pages > 0:
                footer_lines.append(f"📄 **Page:** `{current_page + 1} / {total_pages + 1}`")
            footer_lines.extend([
                f"💳 **Victory Points:** `{vp:,} VP`",
                f"🏆 **League Points:** `{lp:,} LP`"
            ])
            return footer_lines

        if category == "medicine":
            lines = [
                "🏪 **PokéMart — Medicine**",
                "━━━━━━━━━━━━━━━━━━━━━",
                "🛒 **Commands:** `/buy [item] [qty]`",
                "__(Use /mochi, /candy, or /feather after buying)__",
                "",
                "📦 **Stock:**",
            ]
            for key in SHOP_MEDICINE_KEYS:
                lines.append(f"• **{medicine_name(key)}** — `{medicine_shop_price(key):,} VP`")
            lines.extend(_footer())
            return "\n".join(lines), 0, 0

        if category == "held":
            entries = await self._held_item_shop_catalog()
            items, total, current_page = paginate_items(entries, page=page, per_page=SHOP_HELD_PAGE_SIZE)
            max_page = max(0, (max(total, 1) - 1) // SHOP_HELD_PAGE_SIZE)
            lines = [
                "🏪 **PokéMart — Held Items**",
                "━━━━━━━━━━━━━━━━━━━━━",
                "🛒 **Commands:** `/buy [item] [qty]`",
                "__(Held items are purchased using League Points)__",
                "",
                "📦 **Stock:**",
            ]
            for index, item_name in enumerate(items, start=current_page * SHOP_HELD_PAGE_SIZE + 1):
                lines.append(f"`[{index:>2}]` **{item_name}** — `{self._held_item_shop_price(item_name):,} LP`")
            lines.extend(_footer(current_page, max_page))
            return "\n".join(lines), current_page, max_page

        if category == "battle":
            rotation = self._battle_item_rotation(trainer.telegram_user_id)
            purchased_keys = self._battle_shop_purchased_keys(trainer)
            reset_at = self._weekly_shop_reset_at().strftime("%Y-%m-%d %H:%M UTC")
            lines = [
                "🏪 **PokéMart — Battle Items**",
                "━━━━━━━━━━━━━━━━━━━━━",
                "🛒 **How to buy:** Use the inline buttons below!",
                "__(Limit 1 per item. Stock refreshes weekly.)__",
                f"⏱️ **Next Refresh:** `{reset_at}`",
                "",
                "📦 **This Week's Stock:**",
            ]
            for entry in rotation:
                is_purchased = self._battle_shop_purchase_key(entry) in purchased_keys
                status = " `[SOLD OUT]`" if is_purchased else ""
                lines.append(f"• **{entry['display_name']}** — `{entry['price']:,} LP`{status}")
            lines.extend(_footer())
            return "\n".join(lines), 0, 0

        if category == "key":
            incubator_owned = inventories.key_item_count(trainer, KEY_ITEM_EGG_INCUBATOR)
            max_soup_owned = inventories.key_item_count(trainer, KEY_ITEM_MAX_SOUP)
            lines = [
                "🏪 **PokéMart — Key Items**",
                "━━━━━━━━━━━━━━━━━━━━━",
                "🛒 **Commands:** `/buy [item]`",
                "",
                "📦 **Stock:**",
                f"• **{KEY_ITEM_EGG_INCUBATOR}** — `{EGG_INCUBATOR_COST:,} VP` {'__(Owned)__' if incubator_owned > 0 else ''}".rstrip(),
                f"• **{KEY_ITEM_MAX_SOUP}** — `{MAX_SOUP_COST_VP:,} VP` __**(Owned: {max_soup_owned})**__",
            ]
            for item_name in FORM_CHANGE_ITEM_ORDER:
                owned = inventories.key_item_count(trainer, item_name)
                suffix = "_(Owned)_" if owned > 0 else ""
                lines.append(f"• **{item_name}** — `{FORM_CHANGE_ITEM_COST_VP:,} VP` {suffix}".rstrip())
            lines.extend(_footer())
            return "\n".join(lines), 0, 0

        # Fallback to Balls
        lines = [
            "🏪 **PokéMart — Poké Balls**",
            "━━━━━━━━━━━━━━━━━━━━━",
            "🛒 **Commands:**",
            "`Buy  →` `/buy [item] [qty]`",
            "`Sell →` `/sell [item] [qty]`",
            "",
            "📦 **Stock:**",
            "• **Poké Ball** — `5 VP`",
            "• **Great Ball** — `7 VP`",
            "• **Ultra Ball** — `12 VP`",
            "• **Repeat Ball** — `15 VP`",
            "• **Nest Ball** — `20 VP`",
        ]
        lines.extend(_footer())
        return "\n".join(lines), 0, 0

    async def _held_shop_offer(self, item_arg: str) -> dict[str, Any] | None:
        target = normalize_lookup(item_arg)
        for item_name in await self._held_item_shop_catalog():
            if normalize_lookup(item_name) == target:
                return {
                    "kind": "held",
                    "name": item_name,
                    "display_name": item_name,
                    "price": self._held_item_shop_price(item_name),
                    "currency": "lp",
                    "amount": 1,
                }
        return None

    def _battle_shop_offer(self, user_id: int, item_arg: str) -> dict[str, Any] | None:
        target = normalize_lookup(item_arg)
        for entry in self._battle_item_rotation(user_id):
            candidates = {
                normalize_lookup(str(entry.get("name") or "")),
                normalize_lookup(str(entry.get("display_name") or "")),
            }
            if entry.get("kind") == "tm":
                candidates.add(normalize_lookup(str(entry.get("name") or "").split(" - ", 1)[0]))
            if target in candidates:
                return dict(entry)
        return None

    def _key_item_shop_offer(self, item_arg: str) -> dict[str, Any] | None:
        target = normalize_lookup(item_arg)
        offers = [
            {
                "kind": "key",
                "name": KEY_ITEM_EGG_INCUBATOR,
                "display_name": KEY_ITEM_EGG_INCUBATOR,
                "price": EGG_INCUBATOR_COST,
                "currency": "vp",
                "amount": 1,
                "unique": True,
            },
            {
                "kind": "key",
                "name": KEY_ITEM_MAX_SOUP,
                "display_name": KEY_ITEM_MAX_SOUP,
                "price": MAX_SOUP_COST_VP,
                "currency": "vp",
                "amount": 1,
                "unique": False,
            },
            *[
                {
                    "kind": "key",
                    "name": item_name,
                    "display_name": item_name,
                    "price": FORM_CHANGE_ITEM_COST_VP,
                    "currency": "vp",
                    "amount": 1,
                    "unique": True,
                }
                for item_name in FORM_CHANGE_ITEM_ORDER
            ],
        ]
        return next((offer for offer in offers if normalize_lookup(offer["name"]) == target), None)

    def _apply_shop_add_action(
        self,
        inventories: InventoryRepository,
        trainer,
        add_action: tuple[str, str, int],
        qty: int,
    ) -> int:
        amount_per_purchase = int(add_action[2])
        total_amount = int(qty) * amount_per_purchase
        if add_action[0] == "ball":
            inventories.add_ball(trainer, add_action[1], int(qty))
        elif add_action[0] == "medicine":
            inventories.add_medicine(trainer, add_action[1], int(qty))
        elif add_action[0] == "tm":
            inventories.add_tm(trainer, add_action[1], total_amount)
        elif add_action[0] == "key":
            inventories.add_key_item(trainer, add_action[1], total_amount)
        else:
            inventories.add_item(trainer, add_action[1], total_amount)
        return int(qty) if amount_per_purchase == 1 else total_amount

    def parse_shop_item(self, raw_text: str) -> tuple[str, int] | None:
        parts = raw_text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return None
        remainder = parts[1].strip()
        qty = 1
        if " " in remainder:
            item_text, maybe_qty = remainder.rsplit(" ", 1)
            if maybe_qty.isdigit():
                if int(maybe_qty) <= 0:
                    return None
                remainder = item_text.strip()
                qty = int(maybe_qty)
        if not remainder:
            return None
        return remainder, qty

    def _region_label(self, region_id: str) -> str:
        for region in self.regions:
            if region["id"] == region_id:
                return region["label"]
        return region_id.title()

    def region_buttons(self) -> list[list[Button]]:
        buttons = [Button.inline(region["label"], data=f"starter:region:{region['id']}".encode("utf-8")) for region in self.starter_regions]
        return [buttons[index:index + 3] for index in range(0, len(buttons), 3)]

    def starter_buttons(self, region_id: str) -> list[list[Button]]:
        for region in self.starter_regions:
            if region["id"] == region_id:
                buttons = [
                    Button.inline(species, data=f"starter:pick:{region_id}:{species}".encode("utf-8"))
                    for species in region["starters"]
                ]
                return [
                    buttons,
                    [Button.inline("Back", data="starter:regions".encode("utf-8"))],
                ]
        return [[Button.inline("Back", data="starter:regions".encode("utf-8"))]]

    def starter_confirm_buttons(self, region_id: str, species: str) -> list[list[Button]]:
        return [
            [Button.inline("Confirm Starter", data=f"starter:confirm:{region_id}:{species}".encode("utf-8"))],
            [Button.inline("Back", data=f"starter:region:{region_id}".encode("utf-8"))],
        ]

    def _load_start_profile(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return {
            "starter_species": bool(trainer.starter_species),
            "display_name": trainer.display_name,
            "region_name": self._region_label(trainer.current_region),
            "location_text": trainer.current_location or "Unknown Area",
        }

    def _pokemon_list_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, Any]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        items, total, page = self.pokemon_page(trainer, pokemons, page=0)
        return (
            self.pokemon_list_text(trainer, items=items, total=total, page=page),
            self.pokemon_list_buttons(owner_id=owner_id, page=page, total=total),
        )

    def _display_menu_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        mode: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        if mode:
            normalized = normalize_display_mode(mode)
            if normalized is None:
                return {"status": "invalid"}
            trainers.set_preferences(trainer, display_mode=normalized)
            return {"status": "updated", "text": f"Pokemon display mode set to {display_mode_label(normalized)}."}
        return {
            "status": "menu",
            "text": self.display_menu_text(trainer),
            "buttons": self.display_menu_buttons(trainer.display_mode),
        }

    def _sort_menu_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        mode: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        if mode:
            normalized = normalize_sort_mode(mode)
            if normalized is None:
                return {"status": "invalid"}
            trainers.set_preferences(trainer, sort_mode=normalized)
            return {"status": "updated", "text": f"Pokemon sort mode set to {sort_mode_label(normalized)}."}
        return {
            "status": "menu",
            "text": self.sort_menu_text(trainer),
            "buttons": self.sort_menu_buttons(trainer.sort_mode, trainer.sort_descending),
        }

    def _toggle_sort_order_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        trainers.set_preferences(trainer, sort_descending=not trainer.sort_descending)
        return {
            "text": self.sort_menu_text(trainer),
            "buttons": self.sort_menu_buttons(trainer.sort_mode, trainer.sort_descending),
            "answer": f"Order set to {'Descending' if trainer.sort_descending else 'Ascending'}.",
        }

    def _pending_move_prompt_payload(self, session, *, owner_id: int) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        trainer = trainers.get_by_telegram_user_id(owner_id)
        if not trainer:
            return {"status": "empty"}
        entries = self.encounters._load_pending_move_entries(trainer)
        if not entries:
            return {"status": "empty"}
        now_ts = int(datetime.utcnow().timestamp())
        active_entries = [entry for entry in entries if int(entry.get("expires_at") or 0) > now_ts]
        self.encounters._store_pending_move_entries(trainer, active_entries)
        if not active_entries:
            return {"status": "expired"}
        prompt = active_entries[0]
        suffix = f"\n\nMore pending moves: {len(active_entries) - 1}" if len(active_entries) > 1 else ""
        return {
            "status": "ok",
            "text": self.encounters._pending_move_prompt_text(prompt) + suffix,
            "buttons": self.encounters._pending_move_buttons(str(prompt["id"]), len(list(prompt.get("moves") or []))),
        }

    def _bag_overview_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, int, int]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return inventories.render_bag(trainer, category="overview", page=0)

    def _bag_category_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        category: str,
        page: int,
    ) -> tuple[str, int, int]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return inventories.render_bag(trainer, category=category, page=page)

    def _pokemon_list_page_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        page: int,
    ) -> tuple[str, Any]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        items, total, current_page = self.pokemon_page(trainer, pokemons, page=page)
        return (
            self.pokemon_list_text(trainer, items=items, total=total, page=current_page),
            self.pokemon_list_buttons(owner_id=owner_id, page=current_page, total=total),
        )

    def _trainer_card_snapshot(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        if not trainer.gender:
            return {"status": "missing_gender"}
        total_caught = trainer.total_caught or 0
        all_pokemon = pokemons.list_owned_pokemon(trainer)
        starter_entry = next(
            (pokemon for pokemon in all_pokemon if str(pokemon.source_kind or "").strip().lower() == "starter"),
            None,
        )
        trainer_level = max(1, int(trainer.trainer_level or 1))
        trainer_exp = max(0, int(trainer.trainer_exp or 0))
        next_level_total = TrainerRepository.exp_for_level(min(200, trainer_level + 1))
        return {
            "status": "ok",
            "trainer_id": trainer.telegram_user_id,
            "display_name": trainer.display_name,
            "gender": trainer.gender,
            "total_caught": total_caught,
            "dex_entries": len({p.species for p in all_pokemon}),
            "joined": trainer.started_at.strftime("%Y-%m-%d") if trainer.started_at else "Unknown",
            "starter_pokemon": effective_species(starter_entry) if starter_entry is not None else (trainer.starter_species or "None"),
            "trainer_level": trainer_level,
            "trainer_exp": trainer_exp,
            "exp_to_next": 0 if trainer_level >= 200 else max(0, next_level_total - trainer_exp),
        }

    def _battlepass_snapshot(self, session, *, trainer_id: int) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        trainer = trainers.get_by_telegram_user_id(trainer_id)
        if not trainer or not trainer.inventory:
            return {"status": "missing"}
        return {"status": "ok", "season_points": int(trainer.inventory.season_points or 0)}

    def _nickname_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        query: str,
        nickname: str | None,
    ) -> tuple[bool, str]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        matches = pokemons.find_by_query(trainer, query)
        if not matches:
            return False, f"No Pokemon matched `{query}`."
        if len(matches) > 1:
            preview = ", ".join(f"`{pokemon.id}` {pokemon.nickname or effective_species(pokemon)}" for pokemon in matches[:6])
            if len(matches) > 6:
                preview += ", ..."
            return False, f"Multiple Pokemon matched `{query}`.\nUse a more specific id or name.\n{preview}"

        pokemon = matches[0]
        cleaned = str(nickname or "").strip()
        if cleaned.lower() in {"clear", "remove", "none", "-"}:
            cleaned = ""
        if len(cleaned) > 64:
            return False, "Nickname must be 64 characters or fewer."

        old_name = str(pokemon.nickname or "").strip()
        pokemons.set_nickname(pokemon, cleaned or None)
        base_species = effective_species(pokemon)
        if cleaned:
            return True, f"`{base_species}` is now nicknamed `{cleaned}`."
        if old_name:
            return True, f"Removed nickname from `{base_species}`."
        return True, f"`{base_species}` does not have a nickname right now."

    def _my_nicknames_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> str:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        nicknamed = pokemons.list_nicknamed_pokemon(trainer)
        lines = ["**Your Nicknamed Pokemon**", ""]
        if not nicknamed:
            lines.append("You have no nicknamed Pokemon yet.")
            lines.append("")
            lines.append("Use `/nickname <pokemon> <nickname>` to set one.")
            return "\n".join(lines)
        for pokemon in nicknamed:
            lines.append(f"`{pokemon.id}` {pokemon.nickname} ({effective_species(pokemon)}) Lv.{pokemon.level}")
        lines.append("")
        lines.append(f"Total nicknamed Pokemon: `{len(nicknamed)}`")
        return "\n".join(lines)

    def _purge_nickname_sessions(self) -> None:
        now = datetime.utcnow()
        stale_ids = [
            owner_id
            for owner_id, session in self.nickname_sessions.items()
            if session.expires_at <= now
        ]
        for owner_id in stale_ids:
            self.nickname_sessions.pop(owner_id, None)

    def _set_nickname_session(self, *, owner_id: int, pokemon_id: int, page: int) -> NicknameSession:
        self._purge_nickname_sessions()
        session = NicknameSession(
            owner_id=int(owner_id),
            pokemon_id=int(pokemon_id),
            page=max(int(page), 0),
            expires_at=datetime.utcnow() + timedelta(minutes=COMMAND_USE_SESSION_MINUTES),
        )
        self.nickname_sessions[int(owner_id)] = session
        return session

    def _get_nickname_session(self, owner_id: int) -> NicknameSession | None:
        self._purge_nickname_sessions()
        session = self.nickname_sessions.get(int(owner_id))
        if session is None:
            return None
        if session.expires_at <= datetime.utcnow():
            self.nickname_sessions.pop(int(owner_id), None)
            return None
        return session

    def _clear_nickname_session(self, owner_id: int) -> None:
        self.nickname_sessions.pop(int(owner_id), None)

    def _nickname_picker_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        page: int,
    ) -> tuple[str, list[list[Button]] | None]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        pokemon_list = self.sorted_owned_pokemon(trainer, pokemons)
        total = len(pokemon_list)
        if total <= 0:
            return "You do not own any Pokemon yet.", None
        max_page = (total - 1) // NICKNAME_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * NICKNAME_PAGE_SIZE
        items = pokemon_list[start:start + NICKNAME_PAGE_SIZE]
        lines = ["**Choose a Pokemon to nickname**", ""]
        button_specs: list[tuple[str, int]] = []
        for index, pokemon in enumerate(items, start=start + 1):
            nickname_suffix = f' "{pokemon.nickname}"' if pokemon.nickname else ""
            lines.append(f"{index}. {effective_species(pokemon)}{nickname_suffix}")
            button_specs.append((str(index), int(pokemon.id)))
        lines.append("")
        lines.append(f"Page `{current_page + 1}` / `{max_page + 1}`")
        lines.append(f"Total Pokemon: `{total}`")

        rows = chunk_buttons(
            [Button.inline(label, data=f"nick:pick:{owner_id}:{current_page}:{pokemon_id}".encode("utf-8")) for label, pokemon_id in button_specs],
            per_row=5,
        )
        nav: list[Button] = []
        if current_page > 0:
            nav.append(Button.inline("<", data=f"nick:page:{owner_id}:{current_page - 1}".encode("utf-8")))
        if current_page < max_page:
            nav.append(Button.inline(">", data=f"nick:page:{owner_id}:{current_page + 1}".encode("utf-8")))
        if nav:
            rows.append(nav)
        rows.append([Button.inline("Cancel", data=f"nick:cancel:{owner_id}".encode("utf-8"))])
        return "\n".join(lines), rows

    def _nickname_prompt_text(self, pokemon, *, page: int) -> str:
        current_nickname = str(getattr(pokemon, "nickname", "") or "").strip()
        species = effective_species(pokemon)
        lines = [
            f"**New nickname for {species}**",
            "",
            f"Current nickname: `{current_nickname or 'None'}`",
            "",
            "Write the new nickname for this Pokemon.",
            "Send `-` to clear the nickname.",
        ]
        return "\n".join(lines)

    def _nickname_prompt_buttons(self, *, owner_id: int, page: int) -> list[list[Button]]:
        return [
            [Button.inline("Back", data=f"nick:page:{owner_id}:{page}".encode("utf-8"))],
            [Button.inline("Cancel", data=f"nick:cancel:{owner_id}".encode("utf-8"))],
        ]

    def _apply_nickname_from_session(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        nickname_session: NicknameSession,
        nickname: str,
    ) -> tuple[bool, str]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        pokemon = pokemons.get_owned_pokemon(trainer, int(nickname_session.pokemon_id))
        if pokemon is None:
            return False, "That Pokemon is no longer available. Use /nickname again."
        cleaned = str(nickname or "").strip()
        if len(cleaned) > 64:
            return False, "Nickname must be 64 characters or fewer."
        if cleaned.lower() in {"clear", "remove", "none", "-"}:
            cleaned = ""
        old_name = str(pokemon.nickname or "").strip()
        pokemons.set_nickname(pokemon, cleaned or None)
        species = effective_species(pokemon)
        if cleaned:
            return True, f"`{species}` is now nicknamed `{cleaned}`."
        if old_name:
            return True, f"Removed nickname from `{species}`."
        return True, f"`{species}` does not have a nickname right now."

    def _autohunt_context(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, str | None, bool]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return (
            str(trainer.current_region or "kanto").strip().lower(),
            str(trainer.current_location or "").strip() or None,
            inventories.has_item(trainer, SHINY_CHARM_ITEM),
        )

    def _reset_safari_cooldown(self, session, *, target_id: int) -> bool:
        trainers = TrainerRepository(session)
        trainer = trainers.get_by_telegram_user_id(target_id)
        if trainer is None or trainer.last_safari_entered_at is None:
            return False
        trainers.reset_safari_cooldown(trainer)
        return True

    async def on_start(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        start_payload = ""
        if event.raw_text:
            parts = event.raw_text.strip().split(None, 1)
            if len(parts) == 2:
                start_payload = parts[1].strip()

        if start_payload.lower() == "hunt":
            await self.encounters.on_hunt(event)
            return

        dm_command_name: str | None = None
        if start_payload.lower().startswith("dmcmd_"):
            dm_command_name = self._normalize_lockable_command_name(start_payload[6:])

        profile = await run_db_work_async(lambda session: self._load_start_profile(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))

        if profile["starter_species"]:
            if event.is_private and dm_command_name:
                try:
                    await self._dispatch_dm_command(sender if isinstance(sender, User) else None, command_name=dm_command_name)
                except ValueError as exc:
                    if str(exc) == "locked_dm_command":
                        await event.respond(USER_COMMAND_LOCK_MESSAGE)
                    else:
                        await event.respond("That command could not be opened from the DM button.")
                return
            response_text = (
                f"**Trainer Profile: {profile['display_name']}**\n"
                "---------------------\n"
                f"**Current Region:** `{profile['region_name']}`\n"
                f"**Current Area:** `{profile['location_text']}`"
            )

            if event.is_private:
                response_text += "\n\nUse `/hunt` to explore the area."
                await event.respond(response_text, parse_mode="md")
            else:
                me = await event.client.get_me()
                bot_username = me.username
                dm_url = f"https://t.me/{bot_username}?start=hunt"
                await event.respond(response_text, buttons=[[Button.url("Hunt", dm_url)]], parse_mode="md")
            return

        await event.respond(
            "**Welcome to the World of Pokemon.**\n"
            "---------------------\n"
            "Your very own Pokemon legend is about to unfold. Before you begin your journey, "
            "you must choose a partner to accompany you.\n\n"
            "Select a region below to view its available starter Pokemon.",
            buttons=self.region_buttons(),
            parse_mode="md",
        )

    async def on_help(self, event: NewMessage.Event) -> None:
        # Default to the first category when the user types /help
        await self._show_help_menu(event, category_key="profile", edit=False)

    async def _show_help_menu(self, event, category_key: str, edit: bool = False) -> None:
        category_data = HELP_CATEGORIES.get(category_key, HELP_CATEGORIES["profile"])
        
        # Build the text
        lines = [
            f"📘 **Help Menu: {category_data['title']}**",
            "━━━━━━━━━━━━━━━━━━━━━",
            ""
        ]
        
        for cmd in category_data["commands"]:
            command_part, desc_part = cmd.split(" - ", 1)
            lines.append(f"• `{command_part}` - {desc_part}")
            
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━",
            "Select a category below to view more commands:"
        ])

        # Build the buttons using the NEW short_label
        buttons = []
        for key, data in HELP_CATEGORIES.items():
            # Use a compact symbol for the active tab to save horizontal space
            label = f"✦ {data['short_label']}" if key == category_key else data['short_label']
            buttons.append(Button.inline(label, data=f"help:cat:{key}".encode("utf-8")))

        # Since the words are much shorter now, 2-per-row will fit perfectly even in groups.
        # You could even try chunk_buttons(buttons, per_row=3) if you want them smaller!
        rows = chunk_buttons(buttons, per_row=2)

        text = "\n".join(lines)
        if edit and hasattr(event, "edit"):
            await safe_event_edit(event, text, buttons=rows, parse_mode="md")
        else:
            await event.respond(text, buttons=rows, parse_mode="md")

    async def on_starter(self, event: NewMessage.Event) -> None:
        await self.on_start(event)

    async def on_my_pokemons(self, event: NewMessage.Event) -> None:
        sender = await resolve_event_user(event)
        response_text, response_buttons = await run_db_work_async(lambda session: self._pokemon_list_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(response_text, buttons=response_buttons)

    async def on_nickname(self, event: NewMessage.Event) -> None:
        self._clear_nickname_session(int(event.sender_id or 0))
        sender = await resolve_event_user(event)
        text, buttons = await run_db_work_async(lambda session: self._nickname_picker_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            page=0,
        ))
        await event.respond(text, buttons=buttons, parse_mode="md")

    async def on_my_nicknames(self, event: NewMessage.Event) -> None:
        sender = await resolve_event_user(event)
        text = await run_db_work_async(lambda session: self._my_nicknames_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(text, parse_mode="md")

    async def on_display(self, event: NewMessage.Event) -> None:
        mode = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""

        sender = await resolve_event_user(event)
        payload = await run_db_work_async(lambda session: self._display_menu_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            mode=mode,
        ))
        if payload["status"] == "invalid":
            await event.respond("Unknown display mode. Use /display to open the menu.")
            return
        await event.respond(payload["text"], buttons=payload.get("buttons"))

    async def on_sort(self, event: NewMessage.Event) -> None:
        mode = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""

        sender = await resolve_event_user(event)
        payload = await run_db_work_async(lambda session: self._sort_menu_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            mode=mode,
        ))
        if payload["status"] == "invalid":
            await event.respond("Unknown sort mode. Use /sort to open the menu.")
            return
        await event.respond(payload["text"], buttons=payload.get("buttons"))

    async def on_learn(self, event: NewMessage.Event) -> None:
        payload = await run_db_work_async(
            lambda session: self._pending_move_prompt_payload(session, owner_id=int(event.sender_id or 0))
        )
        if payload["status"] == "empty":
            await event.respond("You have no pending moves to learn.")
            return
        if payload["status"] == "expired":
            await event.respond("The time to learn this move has expired.")
            return
        await event.respond(payload["text"], buttons=payload["buttons"])

    async def on_forget(self, event: NewMessage.Event) -> None:
        await event.respond("Use the move-learning buttons shown in the pending move prompt.")
        return

        """Legacy helper for move-slot replacement."""
        if not event.is_private:
            await event.respond("Use move-learning prompts in DM.")
            return
            
        args = event.raw_text.split()
        if len(args) < 2 or not args[1].isdigit():
            await event.respond("Select a move slot from the prompt buttons.")
            return
            
        slot = int(args[1])
        if slot < 1 or slot > 4:
            await event.respond("Invalid slot. Use 1-4.")
            return
            
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(event.sender_id)
            if not trainer or not trainer.pending_move_learning:
                await event.respond("You have no pending moves to learn.")
                return
                
            data = json.loads(trainer.pending_move_learning)
            if datetime.utcnow().timestamp() > data["expires_at"]:
                trainer.pending_move_learning = None
                await event.respond("The time to learn this move has expired.")
                return
                
            pokemon = pokemons.get_owned_pokemon(trainer, data["pokemon_id"])
            if not pokemon:
                trainer.pending_move_learning = None
                await event.respond("Error: Pokémon not found.")
                return
                
            new_move = data["move"]
            moves = json.loads(pokemon.moves_json)
            
            if len(moves) < slot:
                await event.respond("Invalid slot.")
                return
                
            old_move = moves[slot-1]
            moves[slot-1] = new_move
            pokemon.moves_json = json.dumps(moves)
            pokemons.sync_packed_set(pokemon, self.data)
            trainer.pending_move_learning = None
            
            await event.respond(f"✅ 1, 2, and... Ta-da! {pokemon.species} forgot {old_move} and learned {new_move}!", buttons=Button.clear())

    async def on_mybag(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        bag_text, current_page, max_page = await run_db_work_async(lambda session: self._bag_overview_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(
            bag_text,
            buttons=self.bag_buttons("overview", page=current_page, max_page=max_page),
            parse_mode="md"
        )

    def _box_menu_text(self, *, owned: int, selected: int) -> str:
        return (
            "📦 **Trainer Box**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Owned: `{owned}`\n"
            f"Selected to open: `{selected}`\n\n"
            "Use the buttons to change amount, then open."
        )

    def _box_menu_buttons(self, *, owner_id: int, selected: int, owned: int) -> list[list[Button]]:
        if owned <= 0:
            return []
        return [
            [
                Button.inline("+1", data=f"box:set:{owner_id}:{selected + 1}".encode("utf-8")),
                Button.inline("+5", data=f"box:set:{owner_id}:{selected + 5}".encode("utf-8")),
                Button.inline("+10", data=f"box:set:{owner_id}:{selected + 10}".encode("utf-8")),
            ],
            [
                Button.inline("Open Selected", data=f"box:open:{owner_id}:{selected}".encode("utf-8")),
                Button.inline("Open All", data=f"box:open:{owner_id}:{owned}".encode("utf-8")),
            ],
            [Button.inline("Cancel", data=f"box:cancel:{owner_id}".encode("utf-8"))],
        ]

    async def _random_box_held_item(self) -> str:
        excluded = {
            normalize_lookup(item_name)
            for item_name in (
                set(MEGA_STONE_LOCATIONS)
                | set(Z_CRYSTALS)
                | set(TERA_SHARDS)
                | {KEY_ITEM_EGG_INCUBATOR, KEY_ITEM_MAX_SOUP, *FORM_CHANGE_ITEM_ORDER}
            )
        }
        candidates = [
            item_name
            for item_name in await self._held_item_catalog_entries()
            if normalize_lookup(item_name) not in excluded
            and normalize_lookup(item_name) not in {"blueorb", "redorb"}
            and not (
                str(item_name).strip().lower().endswith(("ite", "ite x", "ite y", "ite z"))
                and normalize_lookup(item_name) != "eviolite"
            )
            and not str(item_name).strip().lower().endswith(" z")
        ]
        if not candidates:
            return random.choice(list(TM_DROPS))
        return random.choice(candidates)

    async def _open_trainer_boxes(self, trainer, inventories: InventoryRepository, qty: int) -> tuple[int, dict[str, int]]:
        opened = 0
        rewards: dict[str, int] = {}

        nature_mints = [mint_name for _nature, mint_name in TRAINING_NATURE_MINTS]
        weighted_rewards = [
            ("common30", 40.0),
            ("ability_capsule", 5.0),
            ("ability_patch", 3.89),
            ("nature_mint", 5.0),
            ("bottle_cap", 0.01),
            ("gold_bottle_cap", 0.0001),
            ("egg_energy", 6.0),
            ("held_item", 27.0),
            ("tm_bundle", 16.0),
            ("tera_shard", 1.9),
            ("z_crystal", 1.0),
            ("mega_stone", 3.0),
        ]
        reward_names = [name for name, _weight in weighted_rewards]
        reward_weights = [weight for _name, weight in weighted_rewards]

        for _ in range(max(0, int(qty))):
            if not inventories.consume_key_item(trainer, KEY_ITEM_TRAINER_BOX, 1):
                break
            opened += 1
            roll = random.choices(reward_names, weights=reward_weights, k=1)[0]

            if roll == "common30":
                common_reward = random.choice(("lp_1000", "vp_3000", "tm_2", "tm_4"))
                if common_reward == "lp_1000":
                    inventories.add_league_points(trainer, 1000)
                    rewards["League Points x1000"] = rewards.get("League Points x1000", 0) + 1
                elif common_reward == "vp_3000":
                    inventories.add_victory_points(trainer, 3000)
                    rewards["Victory Points x3000"] = rewards.get("Victory Points x3000", 0) + 1
                else:
                    tm_pool = list(TM_DROPS)
                    bundle_size = 2 if common_reward == "tm_2" else 4
                    picked_tms = random.sample(tm_pool, k=min(bundle_size, len(tm_pool))) if tm_pool else []
                    for tm_name in picked_tms:
                        inventories.add_tm(trainer, tm_name, 1)
                        rewards[tm_name] = rewards.get(tm_name, 0) + 1
            elif roll == "ability_capsule":
                inventories.add_item(trainer, ABILITY_CAPSULE_ITEM, 1)
                rewards[ABILITY_CAPSULE_ITEM] = rewards.get(ABILITY_CAPSULE_ITEM, 0) + 1
            elif roll == "ability_patch":
                inventories.add_item(trainer, ABILITY_PATCH_ITEM, 1)
                rewards[ABILITY_PATCH_ITEM] = rewards.get(ABILITY_PATCH_ITEM, 0) + 1
            elif roll == "nature_mint":
                mint = random.choice(nature_mints)
                inventories.add_item(trainer, mint, 1)
                rewards[mint] = rewards.get(mint, 0) + 1
            elif roll == "bottle_cap":
                inventories.add_item(trainer, BOTTLE_CAP_ITEM, 1)
                rewards[BOTTLE_CAP_ITEM] = rewards.get(BOTTLE_CAP_ITEM, 0) + 1
            elif roll == "gold_bottle_cap":
                inventories.add_item(trainer, GOLD_BOTTLE_CAP_ITEM, 1)
                rewards[GOLD_BOTTLE_CAP_ITEM] = rewards.get(GOLD_BOTTLE_CAP_ITEM, 0) + 1
            elif roll == "egg_energy":
                inventories.add_egg_energy(trainer, 10)
                rewards["Egg Energy x10"] = rewards.get("Egg Energy x10", 0) + 1
            elif roll == "held_item":
                item_name = await self._random_box_held_item()
                inventories.add_item(trainer, item_name, 1)
                rewards[item_name] = rewards.get(item_name, 0) + 1
            elif roll == "tm_bundle":
                tm_pool = list(TM_DROPS)
                if tm_pool:
                    picked_tms = random.sample(tm_pool, k=min(6, len(tm_pool)))
                    for tm_name in picked_tms:
                        inventories.add_tm(trainer, tm_name, 1)
                        rewards[tm_name] = rewards.get(tm_name, 0) + 1
            elif roll == "tera_shard":
                shard_name = random.choice(list(TERA_SHARDS))
                inventories.add_item(trainer, shard_name, 20)
                rewards[f"{shard_name} x20"] = rewards.get(f"{shard_name} x20", 0) + 1
            elif roll == "z_crystal":
                crystal_name = random.choice(list(Z_CRYSTALS))
                inventories.add_item(trainer, crystal_name, 1)
                rewards[crystal_name] = rewards.get(crystal_name, 0) + 1
            else:
                stone_name = random.choice(list(MEGA_STONE_LOCATIONS))
                inventories.add_item(trainer, stone_name, 1)
                rewards[stone_name] = rewards.get(stone_name, 0) + 1

        return opened, rewards

    async def on_box(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        requested_qty: int | None = None
        parts = event.raw_text.split()
        if len(parts) > 1 and parts[1].isdigit():
            requested_qty = max(1, int(parts[1]))

        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            owned = inventories.key_item_count(trainer, KEY_ITEM_TRAINER_BOX)
            if owned <= 0:
                await event.respond("You don't have any Trainer Box right now.")
                return

            if requested_qty is not None:
                qty = min(requested_qty, owned)
                opened, rewards = await self._open_trainer_boxes(trainer, inventories, qty)
                remaining = inventories.key_item_count(trainer, KEY_ITEM_TRAINER_BOX)
            else:
                self.box_selection_by_user[event.sender_id] = 1
                selected = 1
                await event.respond(
                    self._box_menu_text(owned=owned, selected=selected),
                    buttons=self._box_menu_buttons(owner_id=event.sender_id, selected=selected, owned=owned),
                    parse_mode="md",
                )
                return

        if opened <= 0:
            await event.respond("No Trainer Box were opened.")
            return
        lines = [f"📦 Opened `{opened}` Trainer Box", "━━━━━━━━━━━━━━━━━━━━━━", "**Rewards**"]
        for item_name in sorted(rewards.keys()):
            amount = rewards[item_name]
            if amount == 1:
                lines.append(f"• {item_name}")
            else:
                lines.append(f"• {item_name} x{amount}")
        lines.append("")
        lines.append(f"Remaining Trainer Box: `{remaining}`")
        await event.respond("\n".join(lines), parse_mode="md")

    async def on_mycard(self, event: NewMessage.Event) -> None:
        loading_message = await event.respond("Getting data...")
        sender = await event.get_sender()
        try:
            snapshot = await run_db_work_async(lambda session: self._trainer_card_snapshot(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            ))
            if snapshot["status"] == "missing_gender":
                try:
                    await loading_message.delete()
                except Exception:
                    pass
                from telethon import Button
                await event.reply(
                    "oops, we need additional data for trainer details",
                    buttons=[[Button.inline("Next", b"cardsetup:start")]]
                )
                return

            import io
            from PIL import Image, ImageDraw, ImageFont
            from bot.config import PROJECT_DIR

            card_file = PROJECT_DIR / "assets" / "card.png"
            if not card_file.exists():
                try:
                    await loading_message.delete()
                except Exception:
                    pass
                await event.respond("Trainer card template not found.")
                return

            font_candidates = [
                PROJECT_DIR / "assets" / "Poppins-Bold.ttf",
                PROJECT_DIR / "assets" / "tahoma.ttf",
                PROJECT_DIR / "assets" / "tahomabd.ttf",
                PROJECT_DIR / "assets" / "font.ttf",
            ]
            img = Image.open(card_file).convert("RGBA")
            draw = ImageDraw.Draw(img)

            def load_font(size: int) -> ImageFont.ImageFont:
                for font_path in font_candidates:
                    try:
                        return ImageFont.truetype(str(font_path), size)
                    except IOError:
                        continue
                return ImageFont.load_default()

            def fit_font(text: str, max_width: int, preferred_size: int, min_size: int, *, stroke_width: int = 0) -> ImageFont.ImageFont:
                if max_width <= 0:
                    return load_font(min_size)
                for size in range(preferred_size, min_size - 1, -4):
                    font = load_font(size)
                    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
                    if (bbox[2] - bbox[0]) <= max_width:
                        return font
                return load_font(min_size)

            def draw_right_text(text: str, *, right_x: int, top_y: int, font: ImageFont.ImageFont, fill: tuple[int, int, int, int], shadow_offset: tuple[int, int] = (0, 0), shadow_fill: tuple[int, int, int, int] = (0, 0, 0, 0)) -> int:
                bbox = draw.textbbox((0, 0), text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
                text_x = right_x - text_width
                if shadow_offset != (0, 0):
                    draw.text((text_x + shadow_offset[0], top_y + shadow_offset[1]), text, font=font, fill=shadow_fill)
                draw.text((text_x, top_y), text, font=font, fill=fill)
                return text_height

            def draw_left_text(text: str, *, left_x: int, top_y: int, font: ImageFont.ImageFont, fill: tuple[int, int, int, int], shadow_offset: tuple[int, int] = (0, 0), shadow_fill: tuple[int, int, int, int] = (0, 0, 0, 0)) -> int:
                bbox = draw.textbbox((0, 0), text, font=font)
                text_height = bbox[3] - bbox[1]
                if shadow_offset != (0, 0):
                    draw.text((left_x + shadow_offset[0], top_y + shadow_offset[1]), text, font=font, fill=shadow_fill)
                draw.text((left_x, top_y), text, font=font, fill=fill)
                return text_height

            content_left = int(img.width * 0.585)
            content_right = img.width - 60
            content_width = content_right - content_left
            id_right = content_right
            id_top = int(img.height * 0.025)
            panel_top = int(img.height * 0.19)
            panel_bottom = img.height - int(img.height * 0.15)

            id_text = f"Id : {snapshot['trainer_id']}"
            info_lines = [
                f"Dex entries : {snapshot['dex_entries']}",
                f"Pokemon caught : {snapshot['total_caught']}",
                f"Trainer level : {snapshot['trainer_level']}",
                f"Trainer exp : {snapshot['trainer_exp']:,}",
                f"Exp to next level : {snapshot['exp_to_next']:,}",
                f"Adventure started : {snapshot['joined']}",
            ]

            id_font = fit_font(id_text, content_width, 70, 40)
            info_font = fit_font(max(info_lines, key=len), content_width, 140, 70)
            text_fill = (0, 0, 0, 255)
            id_fill = (0, 0, 0, 255)

            draw_right_text(id_text, right_x=id_right, top_y=id_top, font=id_font, fill=id_fill)

            block_line_height = draw.textbbox((0, 0), "Ay", font=info_font)[3] - draw.textbbox((0, 0), "Ay", font=info_font)[1]
            available_h = panel_bottom - panel_top
            n = len(info_lines)
            line_gap = max(10, (available_h - n * block_line_height) // max(1, n - 1))

            y = panel_top
            for line in info_lines:
                draw_left_text(line, left_x=content_left, top_y=y, font=info_font, fill=text_fill)
                y += block_line_height + line_gap

            sprite_path = PROJECT_DIR / "assets" / f"{snapshot['gender']}.png"
            if sprite_path.exists():
                sprite = Image.open(sprite_path).convert("RGBA")
                alpha = sprite.split()[-1]
                bbox = alpha.getbbox()
                if bbox:
                    sprite = sprite.crop(bbox)
                max_w = content_left - 100
                max_h = int(img.height * 0.88)
                ratio = min(max_w / sprite.width, max_h / sprite.height)
                new_w = int(sprite.width * ratio)
                new_h = int(sprite.height * ratio)
                sprite = sprite.resize((new_w, new_h), Image.Resampling.LANCZOS)
                sx = max(0, ((content_left - sprite.width) // 2) - 100)
                sy = max(0, img.height - sprite.height - int(img.height * 0.08))
                img.alpha_composite(sprite, dest=(sx, sy))

            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)
            img_bytes.name = "mycard.png"
        except Exception:
            try:
                await loading_message.delete()
            except Exception:
                pass
            raise

        try:
            await loading_message.delete()
        except Exception:
            pass
        await event.reply(file=img_bytes)

    async def on_battlepass(self, event: NewMessage.Event) -> None:
        await self._send_battlepass(event, event.sender_id, page=1)

    async def handle_battlepass_cb(self, event: CallbackQuery.Event, data: str) -> None:
        _, action, val_str = data.split(":")
        
        if action == "info":
            if val_str == "premium":
                await getattr(event, "answer")("Premium Pass is locked.", alert=True)
            elif val_str == "nocost":
                await getattr(event, "answer")("No Cost Pass - Contains 25 progression tiers.", alert=True)
            elif val_str == "locked":
                await getattr(event, "answer")("You haven't unlocked the next page yet!", alert=True)
            return

        elif action == "page":
            page = int(val_str)
            await self._send_battlepass(event, event.sender_id, page=page, edit=True)

    async def _send_battlepass(self, event, trainer_id: int, page: int = 1, edit: bool = False):
        import io
        from PIL import Image, ImageDraw, ImageFont
        from telethon import Button
        from bot.config import PROJECT_DIR

        battlepass = await run_db_work_async(
            lambda session: self._battlepass_snapshot(session, trainer_id=trainer_id),
            read_only=True,
        )
        if battlepass["status"] == "missing":
            await event.respond("Trainer or inventory not found.")
            return
        current_sp = battlepass["season_points"]

        current_tier = (current_sp // 100) + 1
        progress = current_sp % 100

        bp_file = PROJECT_DIR / "assets" / "battlepass.png"
        if not bp_file.exists():
            await event.respond("Battle pass image not found.")
            return

        img = Image.open(bp_file).convert("RGBA")
        draw = ImageDraw.Draw(img)

        font_path = PROJECT_DIR / "assets" / "Poppins-Bold.ttf"
        medium_font_path = PROJECT_DIR / "assets" / "Poppins-Medium.ttf"
        try:
            tier_font = ImageFont.truetype(str(font_path), 14)
            num_font = ImageFont.truetype(str(medium_font_path), 12)
        except:
            tier_font = ImageFont.load_default()
            num_font = ImageFont.load_default()

        draw.text((352, 150), f"{min(current_tier, 25)}", font=tier_font, fill=(255, 255, 255), anchor="mm")
        if current_tier <= 25:
            pct = progress / 100.0
            bar_width = int(155 * pct)
            draw.rectangle([405, 143, 405 + bar_width, 157], fill=(130, 100, 200))

        start_tier = (page - 1) * 10
        start_x = 100
        gap_x = 79.2
        start_y = 413
        for i in range(10):
            tier_num = start_tier + i + 1
            if tier_num > 25:
                break
            bx = start_x + (gap_x * i)
            draw.text((bx, start_y), str(tier_num), font=num_font, fill=(0, 0, 0), anchor="mm")

        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        img_bytes.name = "battlepass.png"

        max_allowed_page = ((current_tier - 1) // 10) + 1
        prev_page = page - 1 if page > 1 else 1
        if page < 3 and (page + 1) <= max_allowed_page:
            next_data = f"bp:page:{page + 1}".encode("utf-8")
        elif page < 3:
            next_data = b"bp:info:locked"
        else:
            next_data = f"bp:page:{page}".encode("utf-8")

        buttons = [
            [
                Button.inline("<<", data=f"bp:page:{prev_page}".encode("utf-8")),
                Button.inline(">>", data=next_data)
            ],
            [
                Button.inline("??? No Cost Pass", data=b"bp:info:nocost"),
                Button.inline("???? Premium Pass", data=b"bp:info:premium")
            ]
        ]

        if edit and hasattr(event, 'edit'):
            await event.edit(file=img_bytes, buttons=buttons)
        else:
            await event.reply(file=img_bytes, buttons=buttons)

    async def on_myteam(self, event: NewMessage.Event) -> None:
        await self.team_manager.on_myteam(event)

    async def on_travel(self, event: NewMessage.Event) -> None:
        await self.encounters.on_travel(event)

    async def on_dexnav(self, event: NewMessage.Event) -> None:
        await self.encounters.on_dexnav(event)

    async def on_open(self, event: NewMessage.Event) -> None:
        await event.respond(
            "Quick hunt keyboard opened.",
            buttons=[
                [
                    Button.text("/hunt", resize=True),
                    Button.text("/close", resize=True),
                ]
            ],
        )

    async def on_close(self, event: NewMessage.Event) -> None:
        await event.respond("Quick hunt keyboard closed.", buttons=Button.clear())

    async def on_hunt(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        if await self.daycare.maybe_find_hunt_egg(event):
            return
        await self.encounters.on_hunt(event)

    async def on_autohunt(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return
        parts = event.raw_text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await event.respond("Usage: /autohunt <count>")
            return

        hunts = int(parts[1])
        if hunts <= 0:
            await event.respond("Count must be a positive number.")
            return
        if hunts > 500000:
            await event.respond("Max allowed is 500000 hunts per report.")
            return

        sender = await event.get_sender()
        region_id, location_id, has_shiny_charm = await run_db_work_async(lambda session: self._autohunt_context(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))

        if not location_id:
            await event.respond("Pick an area first with /travel.")
            return

        await event.respond(f"Running auto-hunt simulation for {hunts} hunts...")
        report = self.encounters.simulate_hunt_report(
            region_id=region_id,
            location_id=location_id,
            hunts=hunts,
            has_shiny_charm=has_shiny_charm,
        )

        region_label = self.encounters._region_label(region_id)
        location = self.encounters.location_by_id(region_id, location_id)
        location_name = str(location["name"]) if location else location_id
        lines = [
            f"AutoHunt Report ({report['hunts']} hunts)",
            f"Region: {region_label}",
            f"Location: {location_name}",
            f"Shiny: {report['shiny']}",
            f"TMs: {report['tms']}",
            f"Mega Stones: {report['mega_stone']}",
            f"Z-Crystals: {report['z_crystal']}",
            f"Tera Shards: {report['tera_shards']}",
            f"EXP Candy: {report['exp_candy']}",
        ]
        if report.get("failed_spawns", 0):
            lines.append(f"Failed spawns: {report['failed_spawns']}")
        await event.respond("\n".join(lines))

    async def on_safari(self, event: NewMessage.Event) -> None:
        await self.encounters.on_safari(event)

    async def on_sexit(self, event: NewMessage.Event) -> None:
        await self.encounters.on_sexit(event)

    async def on_sreset(self, event: NewMessage.Event) -> None:
        if not event.is_reply:
            await event.respond("Reply to a trainer with /sreset.")
            return
        reply_message = await event.get_reply_message()
        if reply_message is None:
            await event.respond("Reply to a trainer with /sreset.")
            return
        target_user = await reply_message.get_sender()
        if not isinstance(target_user, User) or getattr(target_user, "bot", False):
            await event.respond("Reply to a player message with /sreset.")
            return

        target_name = display_name(target_user)
        reset_done = await run_db_work_async(
            lambda session: self._reset_safari_cooldown(session, target_id=int(target_user.id))
        )

        if reset_done:
            await event.respond(f"Safari cooldown reset for {target_name}.")
            return
        await event.respond(f"{target_name} already has Safari available.")

    async def on_stats(self, event: NewMessage.Event) -> None:
        await self.stats.on_stats(event)

    def _add_usage_text(self) -> str:
        return (
            "Use /add by reply to give a Pokemon to that trainer, or use it without reply to add to yourself.\n"
            "Supported forms:\n"
            "/add <pokemon> <level>\n"
            "/add <pokemon> <-1|0> <level>\n"
            "/add <pokemon> <-1|0> <level> <nature> <ivhp> <ivatk> <ivdef> <ivspa> <ivspd> <ivspe>\n"
            "/add <pokemon> <-1|0> <level> <nature> <6 IVs> <6 EVs> [moves]"
        )

    def _resolve_add_species(self, raw_species: str) -> str | None:
        target_key = species_key(raw_species)
        if target_key in self.pokemon_data.species_reference:
            return self.pokemon_data.species_name(target_key)

        target_lookup = normalize_lookup(raw_species)
        if not target_lookup:
            return None

        for key, payload in self.pokemon_data.species_reference.items():
            display_name = str(payload.get("name") or "").strip()
            if target_lookup in {
                normalize_lookup(key),
                normalize_lookup(display_name),
            }:
                return display_name or self.pokemon_data.species_name(key)
        return None

    def _resolve_add_nature(self, raw_nature: str) -> str | None:
        target = normalize_lookup(raw_nature)
        if not target:
            return None
        for nature_name in NATURES:
            if normalize_lookup(nature_name) == target:
                return nature_name
        return None

    def _resolve_add_move(self, raw_move: str) -> str | None:
        move_text = str(raw_move or "").strip()
        if not move_text:
            return None

        by_id = self.pokemon_data.move_name_from_id(move_text)
        if by_id is not None:
            return by_id

        target = normalize_lookup(move_text)
        for move_id, payload in self.pokemon_data.move_info_by_id.items():
            raw_name = str(payload.get("name") or "").strip()
            if not raw_name:
                continue
            display_name = self.pokemon_data._display_move_name(raw_name)
            if target in {
                normalize_lookup(move_id),
                normalize_lookup(raw_name),
                normalize_lookup(display_name),
            }:
                return display_name
        return None

    def _parse_add_move_tokens(self, tokens: list[str], index: int = 0, picked: list[str] | None = None) -> list[str] | None:
        chosen = list(picked or [])
        if index >= len(tokens):
            return chosen
        if len(chosen) >= 4:
            return None

        for end in range(len(tokens), index, -1):
            candidate = " ".join(tokens[index:end]).strip()
            move_name = self._resolve_add_move(candidate)
            if move_name is None:
                continue
            result = self._parse_add_move_tokens(tokens, end, [*chosen, move_name])
            if result is not None:
                return result
        return None

    def _parse_add_moves(self, raw_moves: str) -> list[str] | None:
        text = str(raw_moves or "").strip()
        if not text:
            return []

        if "," in text:
            move_chunks = [chunk.strip() for chunk in text.split(",") if chunk.strip()]
            if len(move_chunks) > 4:
                return None
            resolved: list[str] = []
            for chunk in move_chunks:
                move_name = self._resolve_add_move(chunk)
                if move_name is None:
                    return None
                resolved.append(move_name)
            return resolved

        tokens = [token for token in text.split() if token]
        if not tokens:
            return []
        return self._parse_add_move_tokens(tokens)

    def _parse_add_remainder(self, tokens: list[str]) -> tuple[dict[str, Any] | None, str | None]:
        if not tokens:
            return None, self._add_usage_text()

        index = 0
        shiny = False
        if tokens[index] in {"-1", "0"}:
            shiny = tokens[index] == "-1"
            index += 1
        if index >= len(tokens):
            return None, self._add_usage_text()

        try:
            level = int(tokens[index])
        except (TypeError, ValueError):
            return None, "Level must be a number from 1 to 100."
        if level < 1 or level > 100:
            return None, "Level must be a number from 1 to 100."
        index += 1

        # Accept the legacy extra trailing number from commands like:
        # /add slaking -1 100 100
        if index < len(tokens) and len(tokens) - index == 1 and tokens[index].lstrip("+-").isdigit():
            index += 1

        if index >= len(tokens):
            return {
                "shiny": shiny,
                "level": level,
                "nature": None,
                "ivs": None,
                "evs": None,
                "moves": None,
                "iv_profile": "hunt",
            }, None

        nature = self._resolve_add_nature(tokens[index])
        if nature is None:
            return None, "Unknown nature."
        index += 1

        if index >= len(tokens):
            return {
                "shiny": shiny,
                "level": level,
                "nature": nature,
                "ivs": None,
                "evs": None,
                "moves": None,
                "iv_profile": "hunt",
            }, None

        stat_tokens = tokens[index:]
        if len(stat_tokens) not in {6} and len(stat_tokens) < 12:
            return None, self._add_usage_text()

        if len(stat_tokens) == 6:
            try:
                iv_values = [int(value) for value in stat_tokens]
            except (TypeError, ValueError):
                return None, "Each IV must be between 0 and 31."
            if any(value < 0 or value > 31 for value in iv_values):
                return None, "Each IV must be between 0 and 31."
            return {
                "shiny": shiny,
                "level": level,
                "nature": nature,
                "ivs": dict(zip(("hp", "atk", "def", "spa", "spd", "spe"), iv_values, strict=True)),
                "evs": {key: 0 for key in ("hp", "atk", "def", "spa", "spd", "spe")},
                "moves": None,
                "iv_profile": None,
            }, None

        try:
            stat_values = [int(value) for value in stat_tokens[:12]]
        except (TypeError, ValueError):
            return None, "IVs and EVs must be numbers."

        iv_values = stat_values[:6]
        ev_values = stat_values[6:]
        if any(value < 0 or value > 31 for value in iv_values):
            return None, "Each IV must be between 0 and 31."
        if any(value < 0 or value > 252 for value in ev_values):
            return None, "Each EV must be between 0 and 252."
        if sum(ev_values) > MAX_TOTAL_EVS:
            return None, f"Total EVs cannot exceed {MAX_TOTAL_EVS}."

        move_text = " ".join(stat_tokens[12:]).strip()
        custom_moves = self._parse_add_moves(move_text)
        if custom_moves is None:
            return None, "Could not parse the move list. Use move ids or move names, and separate multi-word move names with commas."
        if len(custom_moves) > 4:
            return None, "You can only set up to 4 moves."

        return {
            "shiny": shiny,
            "level": level,
            "nature": nature,
            "ivs": dict(zip(("hp", "atk", "def", "spa", "spd", "spe"), iv_values, strict=True)),
            "evs": dict(zip(("hp", "atk", "def", "spa", "spd", "spe"), ev_values, strict=True)),
            "moves": custom_moves or None,
            "iv_profile": None,
        }, None

    def _parse_add_request(self, tokens: list[str]) -> tuple[dict[str, Any] | None, str | None]:
        for species_end in range(len(tokens) - 1, 0, -1):
            species = self._resolve_add_species(" ".join(tokens[:species_end]))
            if species is None:
                continue
            parsed, error = self._parse_add_remainder(tokens[species_end:])
            if parsed is None:
                return None, error
            parsed["species"] = species
            return parsed, None
        return None, "Unknown Pokemon name."

    async def on_add(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return
        raw_parts = event.raw_text.split(maxsplit=1)
        if len(raw_parts) < 2 or not raw_parts[1].strip():
            await event.respond(self._add_usage_text())
            return

        tokens = raw_parts[1].split()
        parsed, error = self._parse_add_request(tokens)
        if parsed is None:
            await event.respond(error or self._add_usage_text())
            return

        target_user = await self._reply_target_user(event)
        if target_user is None:
            target_user = await event.get_sender()
        if not isinstance(target_user, User):
            await event.respond("Could not resolve the target trainer.")
            return

        created = None
        try:
            with db_session() as session:
                trainers = TrainerRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.get_by_telegram_user_id(int(target_user.id))
                if trainer is None:
                    await event.respond(f"{display_name(target_user)} hasn't started their adventure yet.")
                    return
                generated = await self.generator.generate_pokemon(
                    species=str(parsed["species"]),
                    level=int(parsed["level"]),
                    region=trainer.current_region,
                    source_kind="test",
                    shiny=bool(parsed["shiny"]),
                    allow_hidden_ability=True,
                    nature=parsed["nature"],
                    ivs=parsed["ivs"],
                    evs=parsed["evs"],
                    moves=parsed["moves"],
                    iv_profile=parsed["iv_profile"],
                )
                if generated.get("generator_problems"):
                    problem = str((generated.get("generator_problems") or ["Invalid setup."])[0])
                    await event.respond(f"Invalid setup: {problem}")
                    return
                created = pokemons.create_owned_pokemon(trainer=trainer, data=generated)
                trainers.place_in_first_party_slot(trainer, created)
                session.expunge(created)
        except ShowdownBridgeError as exc:
            await event.respond(f"Invalid setup: {str(exc)}")
            return
        except Exception:
            await event.respond("Could not create that Pokemon.")
            return

        if created is None:
            await event.respond("Could not create that Pokemon.")
            return
        await self.stats.send_stats_card(event, created, page="summary")

    async def on_listmoveid(self, event: NewMessage.Event) -> None:
        query = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""
        entries = self.pokemon_data.move_entries(query)
        if not entries:
            if query:
                await event.respond(f"No moves matched '{query}'.")
                return
            await event.respond("No moves found.")
            return

        limit = 40
        lines = ["Move ids", ""]
        for move_id, move_name in entries[:limit]:
            lines.append(f"{move_id} - {move_name}")
        if query:
            lines.extend(["", f"Showing {min(limit, len(entries))} of {len(entries)} match(es)."])
        else:
            lines.extend(["", f"Showing {min(limit, len(entries))} of {len(entries)} moves.", "Use /listmoveid <name or id> to filter."])
        await event.respond("\n".join(lines))

    def _load_tm_drop_map(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for tm_entry in TM_DROPS:
            tm_label = str(tm_entry or "").strip()
            tm_number = self._extract_tm_number(tm_label)
            if tm_number is None:
                continue
            mapping[int(tm_number)] = tm_label
        return mapping

    def _load_stone_data(self) -> dict[str, dict[str, str]]:
        if not STONES_PATH.exists():
            return {}
            
        try:
            payload = json.loads(STONES_PATH.read_text(encoding="utf-8"))
        except (TypeError, ValueError):
            return {}
            
        data: dict[str, dict[str, str]] = {}
        
        if isinstance(payload, list):
            for entry in payload:
                item_name = str(entry.get("mega_stone") or "").strip()
                if not item_name:
                    continue
                
                image_url = str(entry.get("full_image_url") or entry.get("image_url") or "").strip()
                base_species = str(entry.get("mega_evolves") or "Unknown").strip()
                
                mega_suffix = ""
                if item_name.endswith(" X"):
                    mega_suffix = " X"
                elif item_name.endswith(" Y"):
                    mega_suffix = " Y"
                elif item_name.endswith(" Z"):
                    mega_suffix = " Z"
                    
                mega_species = f"Mega {base_species}{mega_suffix}"
                
                data[normalize_lookup(item_name)] = {
                    "item_name": item_name,
                    "pokemon": base_species,
                    "mega": mega_species,
                    "image": image_url,
                }
                
        elif isinstance(payload, dict):
            for raw_name, raw_entry in payload.items():
                item_name = str(raw_name or "").strip()
                if not item_name:
                    continue
                entry = raw_entry if isinstance(raw_entry, dict) else {}
                data[normalize_lookup(item_name)] = {
                    "item_name": item_name,
                    "pokemon": str(entry.get("pokemon") or "").strip(),
                    "mega": str(entry.get("mega") or "").strip(),
                    "image": str(entry.get("image") or "").strip(),
                }
                
        return data

    def _extract_tm_number(self, value: str) -> int | None:
        match = re.search(r"TM\s*0*(\d+)", str(value), flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    def _parse_tm_number_from_command(self, raw_text: str) -> int | None:
        text = str(raw_text or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split(maxsplit=1)
        command = str(parts[0]).lstrip("/")
        command_base = command.split("@", 1)[0].strip().lower()
        if not command_base.startswith("tm"):
            return None

        inline_number = command_base[2:]
        if inline_number.isdigit():
            return int(inline_number)
        if len(parts) <= 1:
            return None

        arg_number = str(parts[1]).strip()
        if arg_number.isdigit():
            return int(arg_number)
        return None

    def _tm_count_for_number(self, inventories: InventoryRepository, trainer, tm_number: int) -> int:
        total = 0
        for tm_name, count in inventories.tm_counts(trainer).items():
            if int(count) <= 0:
                continue
            if self._extract_tm_number(str(tm_name)) == int(tm_number):
                total += int(count)
        return total

    def _consume_tm_by_number(self, inventories: InventoryRepository, trainer, tm_number: int, amount: int = 1) -> bool:
        remaining = max(int(amount), 0)
        if remaining <= 0:
            return True
        counts = inventories.tm_counts(trainer)
        tm_names = [
            str(tm_name)
            for tm_name, count in counts.items()
            if int(count) > 0 and self._extract_tm_number(str(tm_name)) == int(tm_number)
        ]
        for tm_name in tm_names:
            current = int(inventories.tm_counts(trainer).get(tm_name, 0))
            if current <= 0:
                continue
            take = min(remaining, current)
            if take <= 0:
                continue
            if not inventories.consume_tm(trainer, tm_name, take):
                return False
            remaining -= take
            if remaining <= 0:
                return True
        return remaining <= 0

    def _tm_details(self, tm_number: int) -> dict[str, Any] | None:
        tm_entry = self._tm_drop_map.get(int(tm_number))
        if not tm_entry:
            return None
        tm_label, raw_move_name = (
            tm_entry.split(" - ", 1)
            if " - " in tm_entry
            else (f"TM{int(tm_number):03d}", tm_entry)
        )
        raw_move_name = str(raw_move_name or "").strip()
        move_name = self.pokemon_data._display_move_name(raw_move_name)
        move_info = self.pokemon_data.move_info.get(normalize_lookup(raw_move_name), {})
        move_id: int | None = None
        for move_id_text, payload in self.pokemon_data.move_info_by_id.items():
            if normalize_lookup(str(payload.get("name") or "")) != normalize_lookup(raw_move_name):
                continue
            try:
                move_id = int(move_id_text)
            except (TypeError, ValueError):
                move_id = None
            break
        return {
            "tm_number": int(tm_number),
            "tm_label": str(tm_label).upper(),
            "move_id": move_id,
            "move_name": move_name,
            "type": str(move_info.get("type") or "Unknown").title(),
            "category": str(move_info.get("category") or "Unknown").title(),
            "power": move_info.get("power", "--"),
        }

    def _tm_lookup_text(self, details: dict[str, Any], *, owned: int) -> str:
        # Cleaned up RPG-style layout
        return (
            f"**{details['tm_label']} \u2014 {details['move_name']}**\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Type:** `{details['type']}` | **Cat:** `{details['category']}`\n"
            f"**Power:** `{details['power']}`\n\n"
            f"**In Bag:** `{max(int(owned), 0)}`"
        )

    async def _species_can_learn_tm_move(self, species: str, move_name: str) -> bool:
        target_move = normalize_lookup(move_name)
        if not target_move:
            return False
            
        # 1. LIGHTNING FAST PATH: Read from our new static JSON
        from bot.config import IMPORTED_DATA_DIR
        import json
        
        TM_COMPAT_PATH = IMPORTED_DATA_DIR / "tm_compat.json"
        
        if TM_COMPAT_PATH.exists():
            if not hasattr(self, "_tm_compat_cache"):
                try:
                    self._tm_compat_cache = json.loads(TM_COMPAT_PATH.read_text(encoding="utf-8"))
                except Exception:
                    self._tm_compat_cache = {}
            
            # Look up the move in our cache (e.g. {"thunderbolt": ["pikachu", "raichu"]})
            compatible_species = self._tm_compat_cache.get(target_move, [])
            base_species = self.encounters.base_species_key(species)
            
            # Check both the specific form and the base species
            return species_key(species) in compatible_species or base_species in compatible_species

        # 2. SLOW FALLBACK: Only used if tm_compat.json doesn't exist yet
        options = await self._training_moves_for_species(species)
        for entry in options:
            methods = [str(method) for method in (entry.get("methods") or [])]
            if "TM" not in methods:
                continue
            if normalize_lookup(str(entry.get("name") or "")) == target_move:
                return True
        return False

    async def _tm_compatible_pokemon(self, owned: list, move_name: str) -> list:
        compatible: list = []
        cache: dict[str, bool] = {}
        
        for pokemon in owned:
            key = species_key(str(pokemon.species))
            if key not in cache:
                # Because of the JSON cache, this await is effectively instant
                cache[key] = await self._species_can_learn_tm_move(str(pokemon.species), move_name)
            
            if cache[key]:
                compatible.append(pokemon)
                
        return compatible

    def _tm_picker_text(self, *, details: dict[str, Any], count: int, items: list, page: int, total: int, display_mode: str) -> str:
        max_page = max(1, ((max(total, 1) - 1) // TM_COMPAT_PAGE_SIZE) + 1)
        lines = [
            f"**{details['tm_label']} \u2014 {details['move_name']}**",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"**In Bag:** `{count}` | **Compatible:** `{total}`\n",
            "Select a Pokemon to teach this move:\n"
        ]
        
        if not items:
            lines.append("__No compatible Pokemon in your collection.__")
            return "\n".join(lines)
            
        start = page * TM_COMPAT_PAGE_SIZE + 1
        for index, pokemon in enumerate(items, start=start):
            lines.append(f"`[{index:<2}]` {self.pokemon_data.collection_entry_text(pokemon, display_mode)}")
            
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━", f"Page {page + 1}/{max_page}"])
        return "\n".join(lines)

    def _tm_picker_buttons(self, *, owner_id: int, tm_number: int, page: int, total: int, items: list) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * TM_COMPAT_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(str(index), data=f"tmuse:pick:{owner_id}:{tm_number}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(items, start=start)
        ]
        if number_buttons:
            rows.extend(chunk_buttons(number_buttons, per_row=5))
        max_page = max(0, (max(total, 1) - 1) // TM_COMPAT_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"tmuse:page:{owner_id}:{tm_number}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="tmuse:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"tmuse:page:{owner_id}:{tm_number}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Cancel", data=f"tmuse:cancel:{owner_id}".encode("utf-8"))])
        return rows

    def _tm_replace_text(self, pokemon, move_name: str) -> str:
        known_moves = [self.pokemon_data._display_move_name(str(move)) for move in json.loads(pokemon.moves_json)]
        lines = [
            f"**Teaching {move_name} to {pokemon.species}**",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"_{pokemon.species} already knows 4 moves. Select a move to forget:_\n"
        ]
        
        for index, move in enumerate(known_moves[:4], start=1):
            lines.append(f"`[{index}]` {move}")
            
        lines.extend(["", f"**New Move:** `{move_name}`"])
        return "\n".join(lines)

    def _tm_replace_buttons(self, *, owner_id: int, tm_number: int, pokemon_id: int) -> list[list[Button]]:
        row = [
            Button.inline(str(index), data=f"tmuse:replace:{owner_id}:{tm_number}:{pokemon_id}:{index}".encode("utf-8"))
            for index in range(1, 5)
        ]
        return [row, [Button.inline("Cancel", data=f"tmuse:cancel:{owner_id}".encode("utf-8"))]]

    def _use_action_item_name(self, action: str) -> str:
        return {
            "bottlecap": BOTTLE_CAP_ITEM,
            "goldbottlecap": GOLD_BOTTLE_CAP_ITEM,
            "maxsoup": KEY_ITEM_MAX_SOUP,
        }.get(action, "Item")

    def _use_action_owned_count(self, inventories: InventoryRepository, trainer, action: str) -> int:
        if action == "maxsoup":
            return int(inventories.key_item_count(trainer, KEY_ITEM_MAX_SOUP) or 0)
        if action == "bottlecap":
            return int(inventories.held_item_count(trainer, BOTTLE_CAP_ITEM) or 0)
        if action == "goldbottlecap":
            return int(inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM) or 0)
        return 0

    def _pokemon_can_use_action(self, pokemon, action: str) -> bool:
        if action in {"bottlecap", "goldbottlecap"}:
            return any(int(getattr(pokemon, f"iv_{stat_key}")) < 31 for stat_key in EV_STAT_ORDER)
        if action == "maxsoup":
            if has_form_state(pokemon):
                return False
            if species_key(str(pokemon.species)).endswith("-gmax"):
                return False
            return self._max_soup_target_species(str(pokemon.species)) is not None
        return False

    def _use_action_compatible_pokemon(self, owned: list, action: str) -> list:
        return [pokemon for pokemon in owned if self._pokemon_can_use_action(pokemon, action)]

    def _use_action_entry_text(self, *, action: str, owned: int, compatible_total: int) -> str:
        item_name = self._use_action_item_name(action)
        lines = [
            f"{item_name}",
            "",
            f"You have: {owned}x {item_name}",
            f"Applicable Pokemon: {compatible_total}",
        ]
        if owned <= 0:
            lines.append("")
            lines.append("You do not own this item right now.")
        elif compatible_total <= 0:
            lines.append("")
            lines.append("No compatible Pokemon are available right now.")
        else:
            lines.append("")
            lines.append("Tap Use to pick a Pokemon.")
        return "\n".join(lines)

    def _use_action_picker_text(
        self,
        *,
        action: str,
        count: int,
        items: list,
        page: int,
        total: int,
        display_mode: str,
    ) -> str:
        item_name = self._use_action_item_name(action)
        lines = [
            item_name,
            "",
            f"You have: {count}x {item_name}",
            "",
            f"Applicable Pokemon: {total}",
            "",
        ]
        if not items:
            lines.append("No compatible Pokemon found.")
            return "\n".join(lines)
        start = page * ITEM_USE_PICKER_PAGE_SIZE + 1
        for index, pokemon in enumerate(items, start=start):
            lines.append(f"{index}. {self.pokemon_data.collection_entry_text(pokemon, display_mode)}")
        return "\n".join(lines)

    def _use_action_picker_buttons(self, *, owner_id: int, action: str, page: int, total: int, items: list) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * ITEM_USE_PICKER_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(
                str(index),
                data=f"useact:pick:{owner_id}:{action}:{pokemon.id}".encode("utf-8"),
            )
            for index, pokemon in enumerate(items, start=start)
        ]
        if number_buttons:
            rows.extend(chunk_buttons(number_buttons, per_row=5))
        max_page = max(0, (max(total, 1) - 1) // ITEM_USE_PICKER_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"useact:page:{owner_id}:{action}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="useact:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"useact:page:{owner_id}:{action}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Cancel", data=f"useact:cancel:{owner_id}:{action}".encode("utf-8"))])
        return rows

    def _use_action_callback_context_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        action: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        count = self._use_action_owned_count(inventories, trainer, action)
        owned = self.sorted_owned_pokemon(trainer, pokemons)
        for pokemon in owned:
            session.expunge(pokemon)
        return {
            "count": count,
            "display_mode": str(trainer.display_mode or "none"),
            "owned": owned,
        }

    async def _open_use_action(self, event: NewMessage.Event, action: str) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            count = self._use_action_owned_count(inventories, trainer, action)
            compatible_total = len(self._use_action_compatible_pokemon(self.sorted_owned_pokemon(trainer, pokemons), action))
        response_text = self._use_action_entry_text(action=action, owned=count, compatible_total=compatible_total)
        response_buttons = (
            [[Button.inline(f"Use {self._use_action_item_name(action)}", data=f"useact:start:{int(event.sender_id or 0)}:{action}".encode("utf-8"))]]
            if count > 0 and compatible_total > 0
            else None
        )
        await event.respond(response_text, buttons=response_buttons)

    def _tm_callback_context_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        tm_number: int,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        count = self._tm_count_for_number(inventories, trainer, tm_number)
        owned = self.sorted_owned_pokemon(trainer, pokemons)
        for pokemon in owned:
            session.expunge(pokemon)
        return {
            "count": count,
            "display_mode": str(trainer.display_mode or "none"),
            "owned": owned,
        }

    async def on_tm(self, event: NewMessage.Event) -> None:
        tm_number = self._parse_tm_number_from_command(event.raw_text)
        if tm_number is None:
            await event.respond("Usage: /tm<number> or /tm <number>\nExample: /tm156")
            return
        details = self._tm_details(tm_number)
        if details is None:
            await event.respond("Invalid TM. TM does not exist.")
            return
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            count = self._tm_count_for_number(inventories, trainer, tm_number)
        lookup_text = self._tm_lookup_text(details, owned=count)
        buttons = (
            [[Button.inline("Use TM", data=f"tmuse:start:{int(event.sender_id or 0)}:{tm_number}".encode("utf-8"))]]
            if count > 0
            else None
        )
        if not event.is_private:
            await event.respond(lookup_text, buttons=buttons, parse_mode="md")
            return
        if count <= 0:
            await event.respond(lookup_text, parse_mode="md")
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id) # (Note: You may have named this _busy_reason)
        if busy_reason:
            await event.respond(f"{lookup_text}\n\n{busy_reason}", buttons=buttons, parse_mode="md")
            return
        await event.respond(lookup_text, buttons=buttons, parse_mode="md")

    def _resolve_stone_query(self, query: str) -> dict[str, str] | None:
        import difflib
        
        target = normalize_lookup(query)
        if not target:
            return None
            
        # 1. Exact Match
        for key, payload in self._stone_data.items():
            if target == key:
                return payload
            item_name = str(payload.get("item_name") or "")
            if target == normalize_lookup(item_name):
                return payload
                
        # 2. Fuzzy Match (Autocorrect)
        all_keys = list(self._stone_data.keys())
        close_matches = difflib.get_close_matches(target, all_keys, n=1, cutoff=0.6)
        
        if close_matches:
            best_match_key = close_matches[0]
            return self._stone_data[best_match_key]
                
        return None

    def _stones_help_text(self) -> str:
        return (
            "**Stone Lookup**\n"
            "Use `/stones <name>` to view a stone.\n"
            "Direct alias also works, like `/greninjaite`."
        )

    def _format_form_name(self, raw_name: str) -> str:
        """Formats raw keys like 'groudon-primal' into 'Primal Groudon'."""
        lowered = str(raw_name).strip().lower()
        parts = lowered.split("-")
        
        prefixes = {
            "mega": "Mega",
            "primal": "Primal",
            "origin": "Origin",
            "crowned": "Crowned"
        }
        
        for key, prefix in prefixes.items():
            if key in parts:
                parts.remove(key)
                formatted_parts = [p.upper() if len(p) == 1 else p.title() for p in parts]
                return f"{prefix} {' '.join(formatted_parts)}"
                
        return " ".join(p.upper() if len(p) == 1 else p.title() for p in parts)

    async def _show_megastone_info(self, event: NewMessage.Event, query: str) -> None:
        stone = self._resolve_stone_query(query)
        if stone is None:
            # Using .reply() so it quotes the user's message
            await event.reply("Invalid Mega Stone. Please check the spelling and try again.")
            return
            
        item_name = str(stone.get("item_name") or "").strip()
        owned = 0
        sender = await event.get_sender()
        
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            owned = int(inventories.held_item_count(trainer, item_name) or 0)
            
        base_species = self._format_form_name(stone.get("pokemon") or "Unknown")
        mega_species = self._format_form_name(stone.get("mega") or "Unknown")
        
        owned_text = f"`{owned}`" if owned > 0 else "`0` (Not Owned)"

        caption = (
            f"**{item_name}**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"**Effect:** Transforms {base_species} into {mega_species} during battle.\n"
            f"**Owned:** {owned_text}\n\n"
            "Equip this stone to your Pokemon using `/equip_item`."
        )
        
        image_url = str(stone.get("image") or "").strip()
        if image_url:
            # Using .reply() so it quotes the user's message
            await event.reply(caption, file=image_url, parse_mode="md")
        else:
            await event.reply(caption, parse_mode="md")

    async def on_stones(self, event: NewMessage.Event) -> None:
        query = self._command_query(event.raw_text)
        if not query:
            await event.respond(self._stones_help_text(), parse_mode="md")
            return
        await self._show_megastone_info(event, query)

    async def on_megastone(self, event: NewMessage.Event) -> None:
        query = self._command_query(event.raw_text)
        if not query:
            await event.respond("Usage: /megastone <name>")
            return
        await self._show_megastone_info(event, query)

    async def on_megastone_alias(self, event: NewMessage.Event) -> None:
        command_text = str(event.raw_text or "").strip().split(maxsplit=1)[0]
        command = command_text.split("@", 1)[0].lstrip("/")
        if not command:
            return
        await self._show_megastone_info(event, command)

    async def on_zstone(self, event: NewMessage.Event) -> None:
        query = self._command_query(event.raw_text)
        if not query:
            await event.respond("Usage: /zstone <name>")
            return
        target = normalize_lookup(query)
        item_name = next((name for name in Z_CRYSTALS if normalize_lookup(name) == target), None)
        if item_name is None:
            await event.respond("Invalid Z-Stone.")
            return
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            if inventories.held_item_count(trainer, item_name) <= 0:
                await event.respond("You dont have this item.")
                return
        text = (
            f"**{item_name}**\n"
            "When held by a compatible Pokemon, it can unleash a powered-up Z-Move in battle."
        )
        image_url = ""
        stone_payload = self._resolve_stone_query(item_name)
        if stone_payload is not None:
            image_url = str(stone_payload.get("image") or "").strip()
        if image_url:
            await event.respond(text, file=image_url, parse_mode="md")
            return
        await event.respond(text, parse_mode="md")

    def _item_use_label(self, action: str) -> str:
        return {
            "mochi": "Mochi",
            "candy": "Candy",
            "feather": "Feather",
        }.get(action, "Item")

    def item_use_selection_text(self, action: str, query: str, display_mode: str, matches: list) -> str:
        verb = {
            "mochi": "use mochi on",
            "candy": "use candy on",
            "feather": "use feather on",
        }.get(action, "use item on")
        lines = [f"Which Pokemon should {verb} '{query}'?", ""]
        for index, pokemon in enumerate(matches[:12], start=1):
            lines.append(f"{index}. {pokemon_display_label(pokemon, display_mode)}")
        return "\n".join(lines)

    def item_use_selection_buttons(self, action: str, matches: list) -> list[list[Button]]:
        buttons = [
            Button.inline(str(index), data=f"itemuse:pickpokemon:{action}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(matches[:12], start=1)
        ]
        return [buttons[index:index + 4] for index in range(0, len(buttons), 4)]

    def _medicine_keys_for_action(self, action: str) -> tuple[str, ...]:
        if action == "mochi":
            return MOCHI_KEYS
        if action == "feather":
            return FEATHER_KEYS
        if action == "candy":
            return CANDY_KEYS
        return ()

    def _item_requires_stat_choice(self, medicine_key: str) -> bool:
        kind = str(MEDICINE_DEFINITIONS.get(medicine_key, {}).get("kind") or "")
        return kind in {"mochi-lower", "feather-lower"}

    def _item_target_stat(self, medicine_key: str, stat_key: str | None = None) -> str | None:
        definition = MEDICINE_DEFINITIONS.get(medicine_key, {})
        target = definition.get("ev_stat") or stat_key
        return str(target) if target in EV_STAT_LABELS else None

    def _item_effect_text(self, medicine_key: str, stat_key: str | None = None) -> str:
        definition = MEDICINE_DEFINITIONS.get(medicine_key, {})
        kind = str(definition.get("kind") or "")
        if kind == "rare-candy":
            return "Each use: +1 level"
        if kind == "exp-candy":
            return f"Each use: +{int(definition.get('exp') or 0):,} EXP"
        target_stat = self._item_target_stat(medicine_key, stat_key)
        ev_amount = int(definition.get("ev_amount") or 0)
        if target_stat is None or ev_amount <= 0:
            return "Each use: no effect"
        sign = "+" if kind in {"mochi", "feather"} else "-"
        return f"Each use: {EV_STAT_LABELS[target_stat]} EV {sign}{ev_amount}"

    def item_use_text(self, action: str, pokemon_name: str, available_keys: list[str], counts: dict[str, int]) -> str:
        label = self._item_use_label(action)
        lines = [f"🎒 **{label} for {pokemon_name}**", "━━━━━━━━━━━━━━━━━━━━"]
        for key in available_keys:
            lines.append(f"• **{medicine_name(key)}** `x{int(counts.get(medicine_name(key), 0))}`")
        lines.extend(["", "__Select an item below.__"])
        return "\n".join(lines)
    
    def item_use_buttons(self, action: str, pokemon_id: int, available_keys: list[str]) -> list[list[Button]]:
        buttons = [
            Button.inline(
                medicine_name(key),
                data=f"itemuse:item:{action}:{pokemon_id}:{key}".encode("utf-8"),
            )
            for key in available_keys
        ]
        return chunk_buttons(buttons, per_row=2)
    
    def item_use_stat_text(self, pokemon, medicine_key: str, available_stats: list[str]) -> str:
        lines = [f"💊 **{medicine_name(medicine_key)} for {pokemon.species}**", "━━━━━━━━━━━━━━━━━━━━━━", "__Choose which EV to lower.__", ""]
        for stat_key in available_stats:
            lines.append(f"• **{EV_STAT_LABELS[stat_key]}:** `{int(getattr(pokemon, f'ev_{stat_key}'))}`")
        return "\n".join(lines)

    def item_use_stat_buttons(
        self,
        action: str,
        pokemon_id: int,
        medicine_key: str,
        available_stats: list[str],
    ) -> list[list[Button]]:
        buttons = [
            Button.inline(
                EV_STAT_LABELS[stat_key],
                data=f"itemuse:stat:{action}:{pokemon_id}:{medicine_key}:{stat_key}".encode("utf-8"),
            )
            for stat_key in available_stats
        ]
        rows = chunk_buttons(buttons, per_row=2)
        rows.append([Button.inline("Back", data=f"itemuse:pickpokemon:{action}:{pokemon_id}".encode("utf-8"))])
        return rows

    def item_use_amount_text(
        self,
        pokemon,
        medicine_key: str,
        *,
        available_count: int,
        stat_key: str | None = None,
    ) -> str:
        lines = [f"💊 **{medicine_name(medicine_key)} for {pokemon.species}**", ""]
        lines.append(f"📦 **Owned:** `{available_count}`")
        target_stat = self._item_target_stat(medicine_key, stat_key)
        if target_stat is not None:
            lines.append(f"🎯 **Target Stat:** `{EV_STAT_LABELS[target_stat]}`")
            lines.append(f"📊 **Current EV:** `{int(getattr(pokemon, f'ev_{target_stat}'))}`")
        lines.append(f"⚡ {self._item_effect_text(medicine_key, stat_key)}")
        lines.extend(["", "How many should be used?"])
        return "\n".join(lines)

    def item_use_amount_buttons(
        self,
        action: str,
        pokemon_id: int,
        medicine_key: str,
        *,
        stat_key: str | None = None,
    ) -> list[list[Button]]:
        amount_buttons = []
        for amount in ITEM_USE_QUANTITIES:
            if stat_key is None:
                payload = f"itemuse:apply:{action}:{pokemon_id}:{medicine_key}:{amount}"
            else:
                payload = f"itemuse:apply:{action}:{pokemon_id}:{medicine_key}:{stat_key}:{amount}"
            amount_buttons.append(Button.inline(f"+{amount}", data=payload.encode("utf-8")))
        rows = [amount_buttons]
        back_payload = (
            f"itemuse:item:{action}:{pokemon_id}:{medicine_key}"
            if stat_key is not None
            else f"itemuse:pickpokemon:{action}:{pokemon_id}"
        )
        rows.append([Button.inline("Back", data=back_payload.encode("utf-8"))])
        return rows

    def _refresh_pokemon_hp_after_stat_change(self, pokemon, old_stats: dict[str, int], new_stats: dict[str, int]) -> None:
        old_hp = int(old_stats.get("hp", pokemon.max_hp))
        new_hp = int(new_stats.get("hp", old_hp))
        hp_delta = new_hp - old_hp
        pokemon.max_hp = max(1, new_hp)
        pokemon.current_hp = min(max(0, pokemon.current_hp + hp_delta), pokemon.max_hp)

    async def _show_item_use_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        action: str,
        pokemon_id: int,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
            if pokemon is None:
                response_text = "That Pokemon is no longer available."
            else:
                counts = inventories.medicine_counts(trainer)
                available_keys = [
                    key for key in self._medicine_keys_for_action(action)
                    if int(counts.get(medicine_name(key), 0)) > 0
                ]
                if not available_keys:
                    response_text = f"You do not have any {self._item_use_label(action).lower()} right now."
                else:
                    response_text = self.item_use_text(action, pokemon.species, available_keys, counts)
                    response_buttons = self.item_use_buttons(action, pokemon.id, available_keys)

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons,parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons, parse_mode="md")

    async def _show_item_use_stat_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        action: str,
        pokemon_id: int,
        medicine_key: str,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
            if pokemon is None:
                response_text = "That Pokemon is no longer available."
            else:
                available_count = inventories.medicine_count(trainer, medicine_name(medicine_key))
                if available_count <= 0:
                    response_text = f"You do not have any {medicine_name(medicine_key)}."
                else:
                    ev_step = int(MEDICINE_DEFINITIONS[medicine_key].get("ev_amount") or 0)
                    available_stats = [
                        stat_key
                        for stat_key in EV_STAT_ORDER
                        if int(getattr(pokemon, f"ev_{stat_key}")) >= ev_step
                    ]
                    if not available_stats:
                        response_text = f"{pokemon.species} does not have enough EVs for {medicine_name(medicine_key)}."
                    else:
                        response_text = self.item_use_stat_text(pokemon, medicine_key, available_stats)
                        response_buttons = self.item_use_stat_buttons(action, pokemon.id, medicine_key, available_stats)

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons,parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons, parse_mode="md")

    async def _show_item_use_amount_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        action: str,
        pokemon_id: int,
        medicine_key: str,
        stat_key: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
            if pokemon is None:
                response_text = "That Pokemon is no longer available."
            else:
                available_count = inventories.medicine_count(trainer, medicine_name(medicine_key))
                if available_count <= 0:
                    response_text = f"You do not have any {medicine_name(medicine_key)}."
                elif self._item_requires_stat_choice(medicine_key) and stat_key not in EV_STAT_LABELS:
                    response_text = "Choose a stat first."
                else:
                    response_text = self.item_use_amount_text(
                        pokemon,
                        medicine_key,
                        available_count=available_count,
                        stat_key=stat_key,
                    )
                    response_buttons = self.item_use_amount_buttons(
                        action,
                        pokemon.id,
                        medicine_key,
                        stat_key=stat_key,
                    )

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons,parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons, parse_mode="md")

    async def _open_item_use(self, event: NewMessage.Event, action: str) -> None:
        query = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""
        if not query:
            await event.respond(f"Use /{action} <pokemon>.")
            return

        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            counts = inventories.medicine_counts(trainer)
            item_label = self._item_use_label(action).lower()
            available_keys = [
                key for key in self._medicine_keys_for_action(action)
                if int(counts.get(medicine_name(key), 0)) > 0
            ]
            if not available_keys:
                await event.respond(f"You do not have any {item_label} right now.")
                return

            matches = pokemons.find_by_query(trainer, query)
            if not matches:
                await event.respond(f"No owned Pokemon matched '{query}'.")
                return
            if len(matches) == 1:
                pokemon_id = int(matches[0].id)
            else:
                await event.respond(
                    self.item_use_selection_text(action, query, trainer.display_mode, matches),
                    buttons=self.item_use_selection_buttons(action, matches),
                )
                return

        await self._show_item_use_menu(event, action=action, pokemon_id=pokemon_id)

    async def on_mochi(self, event: NewMessage.Event) -> None:
        await self._open_item_use(event, "mochi")

    async def on_candy(self, event: NewMessage.Event) -> None:
        await self._open_item_use(event, "candy")

    async def on_feather(self, event: NewMessage.Event) -> None:
        await self._open_item_use(event, "feather")

    def _purge_command_use_sessions(self) -> None:
        now = datetime.utcnow()
        stale_ids = [
            session_id
            for session_id, session in self.command_use_sessions.items()
            if session.expires_at <= now
        ]
        for session_id in stale_ids:
            self.command_use_sessions.pop(session_id, None)

    def _create_command_use_session(
        self,
        *,
        owner_id: int,
        action: str,
        query: str,
        requested_nature: str | None = None,
    ) -> CommandUseSession:
        self._purge_command_use_sessions()
        session = CommandUseSession(
            session_id=secrets.token_hex(3),
            owner_id=int(owner_id),
            action=action,
            query=str(query or "").strip(),
            requested_nature=requested_nature,
            expires_at=datetime.utcnow() + timedelta(minutes=COMMAND_USE_SESSION_MINUTES),
        )
        self.command_use_sessions[session.session_id] = session
        return session

    def _get_command_use_session(self, *, owner_id: int, session_id: str) -> CommandUseSession | None:
        self._purge_command_use_sessions()
        session = self.command_use_sessions.get(session_id)
        if session is None:
            return None
        if int(session.owner_id) != int(owner_id):
            return None
        if session.expires_at <= datetime.utcnow():
            self.command_use_sessions.pop(session_id, None)
            return None
        return session

    def _clear_command_use_sessions_for_user(self, user_id: int) -> None:
        target_id = int(user_id)
        stale_ids = [
            session_id
            for session_id, session in self.command_use_sessions.items()
            if int(session.owner_id) == target_id
        ]
        for session_id in stale_ids:
            self.command_use_sessions.pop(session_id, None)

    def _purge_relearner_sessions(self) -> None:
        now = datetime.utcnow()
        stale_ids = [
            session_id
            for session_id, session in self.relearner_sessions.items()
            if session.expires_at <= now
        ]
        for session_id in stale_ids:
            self.relearner_sessions.pop(session_id, None)

    def _create_relearner_session(
        self,
        *,
        owner_id: int,
        query: str,
        pokemon_ids: list[int],
    ) -> RelearnerSession:
        self._purge_relearner_sessions()
        session = RelearnerSession(
            session_id=secrets.token_hex(3),
            owner_id=int(owner_id),
            query=str(query or "").strip(),
            pokemon_ids=[int(pokemon_id) for pokemon_id in pokemon_ids],
            expires_at=datetime.utcnow() + timedelta(minutes=RELEARNER_SESSION_MINUTES),
        )
        self.relearner_sessions[session.session_id] = session
        return session

    def _get_relearner_session(self, *, owner_id: int, session_id: str) -> RelearnerSession | None:
        self._purge_relearner_sessions()
        session = self.relearner_sessions.get(session_id)
        if session is None:
            return None
        if int(session.owner_id) != int(owner_id):
            return None
        if session.expires_at <= datetime.utcnow():
            self.relearner_sessions.pop(session_id, None)
            return None
        return session

    def _clear_relearner_sessions_for_user(self, user_id: int) -> None:
        target_id = int(user_id)
        stale_ids = [
            session_id
            for session_id, session in self.relearner_sessions.items()
            if int(session.owner_id) == target_id
        ]
        for session_id in stale_ids:
            self.relearner_sessions.pop(session_id, None)

    def _command_query(self, raw_text: str) -> str:
        parts = raw_text.split(maxsplit=1)
        if len(parts) <= 1:
            return ""
        return parts[1].strip()

    def _parse_mint_command(self, raw_text: str) -> tuple[str, str | None]:
        query = self._command_query(raw_text)
        if not query:
            return "", None
        tokens = query.split()
        if len(tokens) <= 1:
            return query, None
        maybe_nature = MINT_NATURE_LOOKUP.get(normalize_lookup(tokens[-1]))
        if not maybe_nature:
            return query, None
        pokemon_query = " ".join(tokens[:-1]).strip()
        if not pokemon_query:
            return query, None
        return pokemon_query, maybe_nature

    def _mint_options_for_pokemon(
        self,
        inventories: InventoryRepository,
        trainer,
        pokemon,
        *,
        requested_nature: str | None = None,
    ) -> list[dict[str, object]]:
        options = [
            entry
            for entry in self._training_available_natures(inventories, trainer)
            if normalize_lookup(str(entry.get("nature") or "")) != normalize_lookup(pokemon.nature)
        ]
        if requested_nature is not None:
            options = [
                entry
                for entry in options
                if normalize_lookup(str(entry.get("nature") or "")) == normalize_lookup(requested_nature)
            ]
        return sorted(options, key=lambda entry: str(entry.get("nature") or "").lower())

    async def _ability_options_for_pokemon(
        self,
        inventories: InventoryRepository,
        trainer,
        pokemon,
        *,
        required_item: str | None = None,
    ) -> list[dict[str, object]]:
        options = await self._training_available_abilities(inventories, trainer, pokemon)
        if required_item is not None:
            options = [
                entry
                for entry in options
                if normalize_lookup(str(entry.get("required_item") or "")) == normalize_lookup(required_item)
            ]
        return sorted(options, key=lambda entry: str(entry.get("name") or "").lower())

    def _bottlecap_stat_keys(self, pokemon) -> list[str]:
        return [
            stat_key
            for stat_key in EV_STAT_ORDER
            if int(getattr(pokemon, f"iv_{stat_key}")) < 31
        ]

    async def _command_use_matches(
        self,
        pokemons: PokemonRepository,
        inventories: InventoryRepository,
        trainer,
        *,
        action: str,
        query: str,
        requested_nature: str | None = None,
    ) -> tuple[list, list]:
        matches = pokemons.find_by_query(trainer, query)
        eligible: list = []
        for pokemon in matches:
            if action == "mint":
                if self._mint_options_for_pokemon(
                    inventories,
                    trainer,
                    pokemon,
                    requested_nature=requested_nature,
                ):
                    eligible.append(pokemon)
                continue
            if action == "bottlecap":
                if self._bottlecap_stat_keys(pokemon):
                    eligible.append(pokemon)
                continue
            if action == "abilitypatch":
                if await self._ability_options_for_pokemon(
                    inventories,
                    trainer,
                    pokemon,
                    required_item=ABILITY_PATCH_ITEM,
                ):
                    eligible.append(pokemon)
                continue
            if action == "abilitycapsule":
                if await self._ability_options_for_pokemon(
                    inventories,
                    trainer,
                    pokemon,
                    required_item=ABILITY_CAPSULE_ITEM,
                ):
                    eligible.append(pokemon)
                continue
            if action == "goldbottlecap" and self._pokemon_can_use_action(pokemon, "goldbottlecap"):
                eligible.append(pokemon)
        return matches, eligible

    def _command_use_item_label(self, action: str, *, requested_nature: str | None = None) -> str:
        if action == "mint":
            return f"{requested_nature} Mint" if requested_nature else "Mint"
        if action == "abilitypatch":
            return ABILITY_PATCH_ITEM
        if action == "abilitycapsule":
            return ABILITY_CAPSULE_ITEM
        if action == "bottlecap":
            return BOTTLE_CAP_ITEM
        if action == "goldbottlecap":
            return GOLD_BOTTLE_CAP_ITEM
        return "Item"

    def _command_use_unavailable_text(
        self,
        action: str,
        *,
        requested_nature: str | None = None,
        pokemon_name: str | None = None,
    ) -> str:
        if action == "mint":
            if pokemon_name:
                if requested_nature is not None:
                    return f"You do not have a usable {requested_nature} Mint for {pokemon_name}."
                return f"You do not have any usable mints for {pokemon_name}."
            if requested_nature is not None:
                return f"No matching Pokemon can use a {requested_nature} Mint right now."
            return "No matching Pokemon can use any mints right now."
        if action == "bottlecap":
            if pokemon_name:
                return f"{pokemon_name} already has all IVs maxed."
            return "No matching Pokemon can use a Bottle Cap right now."
        if action == "abilitypatch":
            if pokemon_name:
                return f"{pokemon_name} has no usable {ABILITY_PATCH_ITEM} option right now."
            return f"No matching Pokemon can use {ABILITY_PATCH_ITEM} right now."
        if action == "abilitycapsule":
            if pokemon_name:
                return f"{pokemon_name} has no usable {ABILITY_CAPSULE_ITEM} option right now."
            return f"No matching Pokemon can use {ABILITY_CAPSULE_ITEM} right now."
        if action == "goldbottlecap":
            if pokemon_name:
                return f"{pokemon_name} already has all IVs maxed."
            return "No matching Pokemon can use a Gold Bottle Cap right now."
        return "That item cannot be used right now."

    def _command_use_picker_text(
        self,
        session: CommandUseSession,
        *,
        items: list,
        page: int,
        total: int,
        display_mode: str,
    ) -> str:
        lines: list[str] = []
        if not items:
            lines.append(self._command_use_unavailable_text(
                session.action,
                requested_nature=session.requested_nature,
            ))
            return "\n".join(lines)
        start = page * ITEM_USE_PICKER_PAGE_SIZE + 1
        for index, pokemon in enumerate(items, start=start):
            lines.append(f"{index}. {pokemon_display_label(pokemon, display_mode)}")
        max_page = max(1, ((max(total, 1) - 1) // ITEM_USE_PICKER_PAGE_SIZE) + 1)
        if max_page > 1:
            lines.extend(["", f"Page {page + 1}/{max_page}"])
        lines.extend([
            "",
            f"Select which Pokemon to use the {self._command_use_item_label(session.action, requested_nature=session.requested_nature)} on.",
        ])
        return "\n".join(lines)

    def _command_use_picker_buttons(
        self,
        *,
        session_id: str,
        page: int,
        total: int,
        items: list,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * ITEM_USE_PICKER_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(
                str(index),
                data=f"cmduse:pick:{session_id}:{pokemon.id}".encode("utf-8"),
            )
            for index, pokemon in enumerate(items, start=start)
        ]
        if number_buttons:
            rows.extend(chunk_buttons(number_buttons, per_row=5))
        max_page = max(0, (max(total, 1) - 1) // ITEM_USE_PICKER_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<", data=f"cmduse:page:{session_id}:{page - 1}".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline(">", data=f"cmduse:page:{session_id}:{page + 1}".encode("utf-8")))
        if nav_row:
            rows.append(nav_row)
        rows.append([Button.inline("Cancel", data=f"cmduse:cancel:{session_id}".encode("utf-8"))])
        return rows

    def _command_use_close_buttons(self, owner_id: int) -> list[list[Button]]:
        return [[Button.inline("Cancel", data=f"cmduse:close:{int(owner_id)}".encode("utf-8"))]]

    def _mint_command_text(self, pokemon) -> str:
        return f"Select a mint for {pokemon.species}."

    def _mint_command_buttons(
        self,
        *,
        owner_id: int,
        pokemon_id: int,
        options: list[dict[str, object]],
    ) -> list[list[Button]]:
        rows = chunk_buttons([
            Button.inline(
                str(entry.get("nature") or "").lower(),
                data=(
                    f"cmduse:mint:{int(owner_id)}:{int(pokemon_id)}:"
                    f"{normalize_lookup(str(entry.get('nature') or ''))}"
                ).encode("utf-8"),
            )
            for entry in options
        ], per_row=2)
        rows.extend(self._command_use_close_buttons(owner_id))
        return rows

    def _ability_item_command_text(self, pokemon, *, required_item: str) -> str:
        return "\n".join([
            f"Select new ability for {pokemon.species}",
            f"Current: {pokemon.ability}",
            "",
            f"Item: {required_item}",
        ])

    def _ability_item_command_buttons(
        self,
        *,
        owner_id: int,
        pokemon_id: int,
        required_item: str,
        options: list[dict[str, object]],
    ) -> list[list[Button]]:
        rows = [[
            Button.inline(
                str(entry.get("name") or ""),
                data=(
                    f"cmduse:abil:{int(owner_id)}:{int(pokemon_id)}:"
                    f"{normalize_lookup(required_item)}:{normalize_lookup(str(entry.get('name') or ''))}"
                ).encode("utf-8"),
            )
        ] for entry in options]
        rows.extend(self._command_use_close_buttons(owner_id))
        return rows

    def _bottlecap_command_text(self, pokemon) -> str:
        return f"Select a stat for {pokemon.species}."

    def _bottlecap_command_buttons(
        self,
        *,
        owner_id: int,
        pokemon_id: int,
        stat_keys: list[str],
    ) -> list[list[Button]]:
        labels = {
            "hp": "HP",
            "atk": "ATK",
            "def": "DEF",
            "spa": "SPA",
            "spd": "SPD",
            "spe": "SPE",
        }
        rows = chunk_buttons([
            Button.inline(
                labels[stat_key],
                data=f"cmduse:bcap:{int(owner_id)}:{int(pokemon_id)}:{stat_key}".encode("utf-8"),
            )
            for stat_key in EV_STAT_ORDER
            if stat_key in stat_keys
        ], per_row=3)
        rows.extend(self._command_use_close_buttons(owner_id))
        return rows

    def _goldbottlecap_command_text(self, pokemon) -> str:
        return f"Use {GOLD_BOTTLE_CAP_ITEM} on {pokemon.species} to max all 6 IVs?"

    def _goldbottlecap_command_buttons(self, *, owner_id: int, pokemon_id: int) -> list[list[Button]]:
        return [
            [Button.inline(
                f"Use {GOLD_BOTTLE_CAP_ITEM}",
                data=f"cmduse:gbcap:{int(owner_id)}:{int(pokemon_id)}".encode("utf-8"),
            )],
            *self._command_use_close_buttons(owner_id),
        ]

    def _apply_mint_option(
        self,
        inventories: InventoryRepository,
        pokemons: PokemonRepository,
        trainer,
        pokemon,
        option: dict[str, object],
    ) -> tuple[bool, str]:
        nature_name = str(option.get("nature") or "").strip()
        item_name = str(option.get("item_name") or "").strip()
        if not nature_name or not item_name:
            return False, "That mint option is not available right now."
        if not inventories.consume_item(trainer, item_name):
            return False, f"You do not have {item_name} anymore."
        old_stats = self.pokemon_data.calculate_stats(pokemon)
        old_nature = str(pokemon.nature)
        pokemon.nature = nature_name
        new_stats = self.pokemon_data.calculate_stats(pokemon)
        self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
        pokemons.sync_packed_set(pokemon, self.pokemon_data)
        return True, f"{pokemon.species}: {old_nature} -> {nature_name} using {item_name}."

    def _apply_ability_item_option(
        self,
        inventories: InventoryRepository,
        pokemons: PokemonRepository,
        trainer,
        pokemon,
        option: dict[str, object],
    ) -> tuple[bool, str]:
        ability_name = str(option.get("name") or "").strip()
        required_item = str(option.get("required_item") or "").strip()
        if not ability_name or not required_item:
            return False, "That ability option is not available right now."
        if normalize_lookup(ability_name) == normalize_lookup(str(pokemon.ability or "")):
            return False, f"{pokemon.species} already has {ability_name}."
        if not inventories.consume_item(trainer, required_item):
            return False, f"You do not have {required_item} anymore."
        old_ability = str(pokemon.ability)
        pokemon.ability = ability_name
        pokemons.sync_packed_set(pokemon, self.pokemon_data)
        return True, f"{pokemon.species}: {old_ability} -> {ability_name} using {required_item}."

    def _apply_bottlecap_to_stat(
        self,
        inventories: InventoryRepository,
        pokemons: PokemonRepository,
        trainer,
        pokemon,
        stat_key: str,
    ) -> tuple[bool, str]:
        if stat_key not in EV_STAT_LABELS:
            return False, "Choose a valid stat."
        old_value = int(getattr(pokemon, f"iv_{stat_key}"))
        if old_value >= 31:
            return False, f"{pokemon.species}'s {EV_STAT_LABELS[stat_key]} IV is already maxed."
        if not inventories.consume_item(trainer, BOTTLE_CAP_ITEM):
            return False, "You do not have any Bottle Cap anymore."
        old_stats = self.pokemon_data.calculate_stats(pokemon)
        setattr(pokemon, f"iv_{stat_key}", 31)
        new_stats = self.pokemon_data.calculate_stats(pokemon)
        self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
        pokemons.sync_packed_set(pokemon, self.pokemon_data)
        return True, (
            f"{pokemon.species}: {EV_STAT_LABELS[stat_key]} IV {old_value} -> 31 "
            f"using {BOTTLE_CAP_ITEM}."
        )

    def _apply_goldbottlecap(
        self,
        inventories: InventoryRepository,
        pokemons: PokemonRepository,
        trainer,
        pokemon,
    ) -> tuple[bool, str]:
        if all(int(getattr(pokemon, f"iv_{stat_key}")) >= 31 for stat_key in EV_STAT_ORDER):
            return False, f"{pokemon.species} already has all IVs maxed."
        if not inventories.consume_item(trainer, GOLD_BOTTLE_CAP_ITEM):
            return False, "You do not have any Gold Bottle Cap anymore."
        old_stats = self.pokemon_data.calculate_stats(pokemon)
        for stat_key in EV_STAT_ORDER:
            setattr(pokemon, f"iv_{stat_key}", 31)
        new_stats = self.pokemon_data.calculate_stats(pokemon)
        self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
        pokemons.sync_packed_set(pokemon, self.pokemon_data)
        return True, f"{pokemon.species}: all IVs were maxed using {GOLD_BOTTLE_CAP_ITEM}."

    def _single_pokemon_match(self, pokemons: PokemonRepository, trainer, query: str):
        matches = pokemons.find_by_query(trainer, query)
        if not matches:
            return None, f"No owned Pokemon matched '{query}'."
        if len(matches) == 1:
            return matches[0], None
        lines = [f"Multiple Pokemon matched '{query}'. Be a bit more specific.", ""]
        for index, pokemon in enumerate(matches[:8], start=1):
            lines.append(f"{index}. {pokemon_display_label(pokemon, trainer.display_mode)}")
        return None, "\n".join(lines)

    async def _show_command_use_picker_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        command_session: CommandUseSession,
        page: int,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            matches, eligible = await self._command_use_matches(
                pokemons,
                inventories,
                trainer,
                action=command_session.action,
                query=command_session.query,
                requested_nature=command_session.requested_nature,
            )
            if not matches:
                response_text = f"No owned Pokemon matched '{command_session.query}'."
            elif not eligible:
                response_text = self._command_use_unavailable_text(
                    command_session.action,
                    requested_nature=command_session.requested_nature,
                )
            else:
                items, total, current_page = paginate_items(
                    eligible,
                    page=page,
                    per_page=ITEM_USE_PICKER_PAGE_SIZE,
                )
                response_text = self._command_use_picker_text(
                    command_session,
                    items=items,
                    page=current_page,
                    total=total,
                    display_mode=str(trainer.display_mode or "none"),
                )
                response_buttons = self._command_use_picker_buttons(
                    session_id=command_session.session_id,
                    page=current_page,
                    total=total,
                    items=items,
                )

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons)
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons)

    async def _show_command_use_target_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        action: str,
        pokemon_id: int,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
            if pokemon is None:
                response_text = "That Pokemon is no longer available."
            elif action == "mint":
                options = self._mint_options_for_pokemon(inventories, trainer, pokemon)
                if not options:
                    response_text = self._command_use_unavailable_text("mint", pokemon_name=pokemon.species)
                else:
                    response_text = self._mint_command_text(pokemon)
                    response_buttons = self._mint_command_buttons(
                        owner_id=event.sender_id,
                        pokemon_id=pokemon.id,
                        options=options,
                    )
            elif action == "bottlecap":
                if inventories.held_item_count(trainer, BOTTLE_CAP_ITEM) <= 0:
                    response_text = "You do not have any Bottle Cap."
                else:
                    stat_keys = self._bottlecap_stat_keys(pokemon)
                    if not stat_keys:
                        response_text = self._command_use_unavailable_text("bottlecap", pokemon_name=pokemon.species)
                    else:
                        response_text = self._bottlecap_command_text(pokemon)
                        response_buttons = self._bottlecap_command_buttons(
                            owner_id=event.sender_id,
                            pokemon_id=pokemon.id,
                            stat_keys=stat_keys,
                        )
            elif action in {"abilitypatch", "abilitycapsule"}:
                required_item = ABILITY_PATCH_ITEM if action == "abilitypatch" else ABILITY_CAPSULE_ITEM
                options = await self._ability_options_for_pokemon(
                    inventories,
                    trainer,
                    pokemon,
                    required_item=required_item,
                )
                if not options:
                    response_text = self._command_use_unavailable_text(action, pokemon_name=pokemon.species)
                else:
                    response_text = self._ability_item_command_text(pokemon, required_item=required_item)
                    response_buttons = self._ability_item_command_buttons(
                        owner_id=event.sender_id,
                        pokemon_id=pokemon.id,
                        required_item=required_item,
                        options=options,
                    )
            elif action == "goldbottlecap":
                if inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM) <= 0:
                    response_text = "You do not have any Gold Bottle Cap."
                elif not self._pokemon_can_use_action(pokemon, "goldbottlecap"):
                    response_text = self._command_use_unavailable_text("goldbottlecap", pokemon_name=pokemon.species)
                else:
                    response_text = self._goldbottlecap_command_text(pokemon)
                    response_buttons = self._goldbottlecap_command_buttons(
                        owner_id=event.sender_id,
                        pokemon_id=pokemon.id,
                    )

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons)
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons)

    async def _open_command_use_flow(
        self,
        event: NewMessage.Event,
        *,
        action: str,
        query: str,
        requested_nature: str | None = None,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        target_pokemon_id: int | None = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            matches, eligible = await self._command_use_matches(
                pokemons,
                inventories,
                trainer,
                action=action,
                query=query,
                requested_nature=requested_nature,
            )
            if not matches:
                response_text = f"No owned Pokemon matched '{query}'."
            elif action == "mint" and not self._training_available_natures(inventories, trainer):
                response_text = "You do not have any usable mints right now."
            elif action == "abilitypatch" and inventories.held_item_count(trainer, ABILITY_PATCH_ITEM) <= 0:
                response_text = f"You do not have any {ABILITY_PATCH_ITEM}."
            elif action == "abilitycapsule" and inventories.held_item_count(trainer, ABILITY_CAPSULE_ITEM) <= 0:
                response_text = f"You do not have any {ABILITY_CAPSULE_ITEM}."
            elif action == "bottlecap" and inventories.held_item_count(trainer, BOTTLE_CAP_ITEM) <= 0:
                response_text = "You do not have any Bottle Cap."
            elif action == "goldbottlecap" and inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM) <= 0:
                response_text = "You do not have any Gold Bottle Cap."
            elif not eligible:
                if len(matches) == 1:
                    response_text = self._command_use_unavailable_text(
                        action,
                        requested_nature=requested_nature,
                        pokemon_name=matches[0].species,
                    )
                else:
                    response_text = self._command_use_unavailable_text(
                        action,
                        requested_nature=requested_nature,
                    )
            elif len(eligible) == 1:
                pokemon = eligible[0]
                if action == "mint" and requested_nature is not None:
                    success, response_text = self._apply_mint_option(
                        inventories,
                        pokemons,
                        trainer,
                        pokemon,
                        self._mint_options_for_pokemon(
                            inventories,
                            trainer,
                            pokemon,
                            requested_nature=requested_nature,
                        )[0],
                    )
                    if not success:
                        response_buttons = None
                else:
                    target_pokemon_id = int(pokemon.id)
            else:
                command_session = self._create_command_use_session(
                    owner_id=int(event.sender_id or 0),
                    action=action,
                    query=query,
                    requested_nature=requested_nature,
                )
                items, total, current_page = paginate_items(
                    eligible,
                    page=0,
                    per_page=ITEM_USE_PICKER_PAGE_SIZE,
                )
                response_text = self._command_use_picker_text(
                    command_session,
                    items=items,
                    page=current_page,
                    total=total,
                    display_mode=str(trainer.display_mode or "none"),
                )
                response_buttons = self._command_use_picker_buttons(
                    session_id=command_session.session_id,
                    page=current_page,
                    total=total,
                    items=items,
                )

        if target_pokemon_id is not None:
            await self._show_command_use_target_menu(
                event,
                action=action,
                pokemon_id=target_pokemon_id,
                edit=False,
            )
            return
        if response_text is not None:
            await event.respond(response_text, buttons=response_buttons)

    async def on_mint(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        query, requested_nature = self._parse_mint_command(event.raw_text)
        if not query:
            await event.respond("Usage: /mint <pokemon> [nature]")
            return
        await self._open_command_use_flow(
            event,
            action="mint",
            query=query,
            requested_nature=requested_nature,
        )

    async def _open_direct_ability_item(
        self,
        event: NewMessage.Event,
        *,
        action: str,
        command_name: str,
    ) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        if not await self._ensure_command_unlocked(event, action):
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        query = self._command_query(event.raw_text)
        if not query:
            await event.respond(f"Usage: /{command_name} <pokemon>")
            return
        await self._open_command_use_flow(event, action=action, query=query)

    async def on_abilitypatch(self, event: NewMessage.Event) -> None:
        await self._open_direct_ability_item(
            event,
            action="abilitypatch",
            command_name="abilitypatch",
        )

    async def on_abilitycapsule(self, event: NewMessage.Event) -> None:
        await self._open_direct_ability_item(
            event,
            action="abilitycapsule",
            command_name="abilitycapsule",
        )

    async def on_bottlecap(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        query = self._command_query(event.raw_text)
        if not query:
            await self._open_use_action(event, "bottlecap")
            return
        await self._open_command_use_flow(event, action="bottlecap", query=query)

    async def on_goldbottlecap(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        query = self._command_query(event.raw_text)
        if not query:
            await self._open_use_action(event, "goldbottlecap")
            return
        await self._open_command_use_flow(event, action="goldbottlecap", query=query)

    async def _relearner_move_options(self, pokemon) -> list[dict[str, object]]:
        await self._prime_pokemon_move_history(pokemon)
        current_move_keys = {
            normalize_lookup(str(move_name))
            for move_name in json.loads(getattr(pokemon, "moves_json", "[]") or "[]")
        }
        options_by_key: dict[str, dict[str, object]] = {}

        def add_option(
            move_name: str,
            *,
            source_key: str,
            source_label: str,
            level: int | None = None,
        ) -> None:
            move_key_value = normalize_lookup(move_name)
            if not move_key_value or move_key_value in current_move_keys:
                return
            option = options_by_key.get(move_key_value)
            if option is None:
                option = {
                    "name": move_name,
                    "level": level,
                    "source_keys": [],
                    "source_labels": [],
                }
                options_by_key[move_key_value] = option
            if level is not None:
                existing_level = option.get("level")
                if existing_level is None or int(level) < int(existing_level):
                    option["level"] = int(level)
            source_keys = list(option.get("source_keys") or [])
            if source_key not in source_keys:
                source_keys.append(source_key)
                option["source_keys"] = source_keys
                option["source_labels"] = list(option.get("source_labels") or []) + [source_label]

        for entry in await self._training_levelup_moves_for_species(pokemon.species):
            move_name = str(entry.get("name") or "").strip()
            try:
                level_value = int(entry.get("level")) if entry.get("level") is not None else None
            except (TypeError, ValueError):
                level_value = None
            if level_value is not None and level_value > int(getattr(pokemon, "level", 1) or 1):
                continue
            if level_value is not None and level_value <= 0:
                add_option(move_name, source_key="evolution", source_label="Evolution", level=level_value)
            else:
                label = f"Lv. {level_value}" if level_value is not None else "Level Up"
                add_option(move_name, source_key="level_up", source_label=label, level=level_value)

        history = load_move_history(pokemon)
        for move_name in history.get("tm", []):
            add_option(str(move_name), source_key="tm", source_label="TM")
        for move_name in history.get("egg", []):
            add_option(str(move_name), source_key="egg", source_label="Egg")
        for move_name in history.get("tutor", []):
            add_option(str(move_name), source_key="tutor", source_label="Tutor")

        source_order = {
            "evolution": 0,
            "level_up": 1,
            "tm": 2,
            "egg": 3,
            "tutor": 4,
        }
        return sorted(
            options_by_key.values(),
            key=lambda entry: (
                0 if entry.get("level") is not None else 1,
                int(entry.get("level") or 0),
                min(source_order.get(str(source), 99) for source in list(entry.get("source_keys") or []) or ["zz"]),
                str(entry.get("name") or "").lower(),
            ),
        )

    def _relearner_picker_text(self, session: RelearnerSession, *, trainer, items: list, page: int, total: int) -> str:
        max_page = max(1, ((max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE) + 1)
        lines = [
            "**MOVE RELEARNER**",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"Search: `{session.query}`",
            "",
        ]
        if not items:
            lines.append("No matching Pokemon can use the move relearner right now.")
        else:
            start = page * POKEMON_LIST_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"`[{index:<2}]` {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━━", f"Page {page + 1}/{max_page}", "", "Select which Pokemon to use the move relearner on."])
        return "\n".join(lines)

    def _relearner_picker_buttons(self, *, session_id: str, page: int, total: int, items: list) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * POKEMON_LIST_PAGE_SIZE + 1
        pick_buttons = [
            Button.inline(str(index), data=f"relearn:pick:{session_id}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(items, start=start)
        ]
        if pick_buttons:
            rows.extend(chunk_buttons(pick_buttons, per_row=5))
        max_page = max(0, (max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<", data=f"relearn:page:{session_id}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="relearn:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline(">", data=f"relearn:page:{session_id}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Cancel", data=f"relearn:close:{session_id}".encode("utf-8"))])
        return rows

    def _relearner_move_text(self, pokemon, *, items: list[dict[str, object]], page: int, total: int, notice: str | None = None) -> str:
        max_page = max(1, ((max(total, 1) - 1) // RELEARNER_MOVE_PAGE_SIZE) + 1)
        lines = [
            "**MOVE RELEARNER**",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Current Moves: `{len(json.loads(pokemon.moves_json))}/4`",
            "",
        ])
        if not items:
            lines.append("No relearnable moves are available right now.")
        else:
            start = page * RELEARNER_MOVE_PAGE_SIZE + 1
            for index, entry in enumerate(items, start=start):
                source_labels = [str(value) for value in list(entry.get("source_labels") or []) if str(value).strip()]
                source_text = " / ".join(source_labels) if source_labels else "Relearn"
                lines.append(f"`[{index:<2}]` {entry['name']} ({source_text})")
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━━", f"Page {page + 1}/{max_page}", "", "Select a move to relearn."])
        return "\n".join(lines)

    def _relearner_move_buttons(self, *, owner_id: int, pokemon_id: int, page: int, total: int, items: list[dict[str, object]]) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * RELEARNER_MOVE_PAGE_SIZE + 1
        pick_buttons = [
            Button.inline(str(index), data=f"relearn:pickmove:{owner_id}:{pokemon_id}:{page}:{index - start}".encode("utf-8"))
            for index, _entry in enumerate(items, start=start)
        ]
        if pick_buttons:
            rows.extend(chunk_buttons(pick_buttons, per_row=5))
        max_page = max(0, (max(total, 1) - 1) // RELEARNER_MOVE_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<", data=f"relearn:move:{owner_id}:{pokemon_id}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="relearn:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline(">", data=f"relearn:move:{owner_id}:{pokemon_id}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Cancel", data=f"relearn:closeown:{owner_id}".encode("utf-8"))])
        return rows

    def _relearner_replace_text(self, pokemon, move_name: str) -> str:
        current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
        lines = [
            "**MOVE RELEARNER**",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"{pokemon.species} wants to relearn `{move_name}`.",
            "",
            "Choose which move to replace.",
            "",
        ]
        for index, current_move in enumerate(current_moves, start=1):
            lines.append(f"`[{index}]` {current_move}")
        return "\n".join(lines)

    def _relearner_replace_buttons(
        self,
        *,
        owner_id: int,
        pokemon_id: int,
        move_page: int,
        move_index: int,
        move_count: int,
    ) -> list[list[Button]]:
        replace_buttons = [
            Button.inline(
                str(index),
                data=f"relearn:replace:{owner_id}:{pokemon_id}:{move_page}:{move_index}:{index - 1}".encode("utf-8"),
            )
            for index in range(1, move_count + 1)
        ]
        return [
            replace_buttons,
            [Button.inline("Back", data=f"relearn:move:{owner_id}:{pokemon_id}:{move_page}".encode("utf-8"))],
        ]

    async def _show_relearner_picker(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: RelearnerSession,
        page: int,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon_list = pokemons.list_owned_pokemon_by_ids(trainer, session.pokemon_ids)
            items, total, current_page = paginate_items(pokemon_list, page=page, per_page=POKEMON_LIST_PAGE_SIZE)
            response_text = self._relearner_picker_text(session, trainer=trainer, items=items, page=current_page, total=total)
            response_buttons = self._relearner_picker_buttons(
                session_id=session.session_id,
                page=current_page,
                total=total,
                items=items,
            )
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons, parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons, parse_mode="md")

    async def _show_relearner_move_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        pokemon_id: int,
        page: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                response_text = "That Pokemon is no longer available."
            else:
                options = await self._relearner_move_options(pokemon)
                items, total, current_page = paginate_items(options, page=page, per_page=RELEARNER_MOVE_PAGE_SIZE)
                response_text = self._relearner_move_text(pokemon, items=items, page=current_page, total=total, notice=notice)
                response_buttons = self._relearner_move_buttons(
                    owner_id=int(event.sender_id or 0),
                    pokemon_id=pokemon_id,
                    page=current_page,
                    total=total,
                    items=items,
                )
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons, parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons, parse_mode="md")

    async def _show_relearner_replace_menu(
        self,
        event: CallbackQuery.Event,
        *,
        pokemon_id: int,
        move_page: int,
        move_index: int,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            options = await self._relearner_move_options(pokemon)
            items, _, _ = paginate_items(options, page=move_page, per_page=RELEARNER_MOVE_PAGE_SIZE)
            if move_index < 0 or move_index >= len(items):
                await event.answer("That move is no longer on this page.", alert=True)
                return
            move_name = str(items[move_index].get("name") or "").strip()
            await safe_event_edit(
                event,
                self._relearner_replace_text(pokemon, move_name),
                buttons=self._relearner_replace_buttons(
                    owner_id=int(event.sender_id or 0),
                    pokemon_id=pokemon_id,
                    move_page=move_page,
                    move_index=move_index,
                    move_count=len(json.loads(pokemon.moves_json)),
                ),
                parse_mode="md",
            )

    async def _apply_relearner_move(
        self,
        event: CallbackQuery.Event,
        *,
        pokemon_id: int,
        move_page: int,
        move_index: int,
        move_slot: int | None = None,
    ) -> None:
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            options = await self._relearner_move_options(pokemon)
            items, _, _ = paginate_items(options, page=move_page, per_page=RELEARNER_MOVE_PAGE_SIZE)
            if move_index < 0 or move_index >= len(items):
                await event.answer("That move is no longer on this page.", alert=True)
                return
            move_name = str(items[move_index].get("name") or "").strip()
            current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
            if normalize_lookup(move_name) in {normalize_lookup(move) for move in current_moves}:
                await event.answer(f"{pokemon.species} already knows {move_name}.", alert=True)
                return
            if len(current_moves) < 4:
                current_moves.append(move_name)
                pokemon.moves_json = json.dumps(current_moves)
                pokemons.sync_packed_set(pokemon, self.pokemon_data)
                notice = f"{pokemon.species} relearned {move_name}."
            else:
                if move_slot is None or move_slot < 0 or move_slot >= len(current_moves):
                    await event.answer("That move slot is invalid.", alert=True)
                    return
                old_move = current_moves[move_slot]
                current_moves[move_slot] = move_name
                pokemon.moves_json = json.dumps(current_moves)
                pokemons.sync_packed_set(pokemon, self.pokemon_data)
                notice = f"{pokemon.species} forgot {old_move} and relearned {move_name}."
        await self._show_relearner_move_menu(
            event,
            pokemon_id=pokemon_id,
            page=move_page,
            notice=notice,
            edit=True,
        )
        await event.answer("Move relearned.")

    async def on_relearner(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        if not await self._ensure_command_unlocked(event, "relearner"):
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        query = self._command_query(event.raw_text)
        if not query:
            await event.respond("Usage: /relearner <pokemon>")
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            matches = pokemons.find_by_query(trainer, query)
            if not matches:
                await event.respond(f"No owned Pokemon matched '{query}'.")
                return
            eligible_ids: list[int] = []
            for pokemon in matches:
                if await self._relearner_move_options(pokemon):
                    eligible_ids.append(int(pokemon.id))
        if not eligible_ids:
            if len(matches) == 1:
                await event.respond(f"{matches[0].species} has no relearnable moves right now.")
                return
            await event.respond("No matching Pokemon have relearnable moves right now.")
            return
        if len(eligible_ids) == 1:
            await self._show_relearner_move_menu(event, pokemon_id=eligible_ids[0], page=0, edit=False)
            return
        session = self._create_relearner_session(owner_id=int(event.sender_id or 0), query=query, pokemon_ids=eligible_ids)
        await self._show_relearner_picker(event, session=session, page=0, edit=False)

    async def handle_relearner_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 2:
            await event.answer("Unknown action.", alert=True)
            return
        action = parts[1]
        if action == "noop":
            await event.answer()
            return
        if action == "close":
            if len(parts) != 3:
                await event.answer("Unknown action.", alert=True)
                return
            session = self._get_relearner_session(owner_id=int(event.sender_id or 0), session_id=parts[2])
            if session is None:
                await event.answer("This menu expired. Use /relearner again.", alert=True)
                return
            self.relearner_sessions.pop(session.session_id, None)
            edited = await safe_event_edit(event, "Move Relearner menu closed.", buttons=None)
            if not edited:
                await event.respond("Move Relearner menu closed.")
            await event.answer("Closed.")
            return
        if action == "closeown":
            if len(parts) != 3 or not parts[2].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            if int(event.sender_id or 0) != int(parts[2]):
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            edited = await safe_event_edit(event, "Move Relearner menu closed.", buttons=None)
            if not edited:
                await event.respond("Move Relearner menu closed.")
            await event.answer("Closed.")
            return
        if action == "page":
            if len(parts) != 4:
                await event.answer("Unknown action.", alert=True)
                return
            session = self._get_relearner_session(owner_id=int(event.sender_id or 0), session_id=parts[2])
            if session is None:
                await event.answer("This menu expired. Use /relearner again.", alert=True)
                return
            page = int(parts[3]) if parts[3].lstrip("+-").isdigit() else 0
            await self._show_relearner_picker(event, session=session, page=page, edit=True)
            await event.answer()
            return
        if action == "pick":
            if len(parts) != 4 or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            session = self._get_relearner_session(owner_id=int(event.sender_id or 0), session_id=parts[2])
            if session is None:
                await event.answer("This menu expired. Use /relearner again.", alert=True)
                return
            self.relearner_sessions.pop(session.session_id, None)
            await self._show_relearner_move_menu(event, pokemon_id=int(parts[3]), page=0, edit=True)
            await event.answer()
            return
        if action == "move":
            if len(parts) != 5 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            page = int(parts[4]) if parts[4].lstrip("+-").isdigit() else 0
            await self._show_relearner_move_menu(event, pokemon_id=int(parts[3]), page=page, edit=True)
            await event.answer()
            return
        if action == "pickmove":
            if len(parts) != 6 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            pokemon_id = int(parts[3])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            move_page = int(parts[4]) if parts[4].lstrip("+-").isdigit() else 0
            move_index = int(parts[5]) if parts[5].lstrip("+-").isdigit() else -1
            sender = await event.get_sender()
            with db_session() as db:
                trainers = TrainerRepository(db)
                pokemons = PokemonRepository(db)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
            if len(current_moves) < 4:
                await self._apply_relearner_move(event, pokemon_id=pokemon_id, move_page=move_page, move_index=move_index)
                return
            await self._show_relearner_replace_menu(event, pokemon_id=pokemon_id, move_page=move_page, move_index=move_index)
            await event.answer()
            return
        if action == "replace":
            if len(parts) != 7 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            await self._apply_relearner_move(
                event,
                pokemon_id=int(parts[3]),
                move_page=int(parts[4]) if parts[4].lstrip("+-").isdigit() else 0,
                move_index=int(parts[5]) if parts[5].lstrip("+-").isdigit() else -1,
                move_slot=int(parts[6]) if parts[6].lstrip("+-").isdigit() else -1,
            )
            return
        await event.answer("Unknown action.", alert=True)

    def _max_soup_target_species(self, species: str) -> str | None:
        entry = self.pokemon_data.species_entry(f"{species}-gmax")
        name = str(entry.get("name") or "").strip()
        return name or None

    async def on_maxsoup(self, event: NewMessage.Event) -> None:
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        query = self._command_query(event.raw_text)
        if not query:
            await self._open_use_action(event, "maxsoup")
            return

        sender = await event.get_sender()
        updated_pokemon = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            if inventories.key_item_count(trainer, KEY_ITEM_MAX_SOUP) <= 0:
                await event.respond("You do not have any Max Soup.")
                return
            matches = pokemons.find_by_query(trainer, query)
            if not matches:
                await event.respond(f"No owned Pokemon matched '{query}'.")
                return
            if len(matches) > 1:
                lines = [f"Multiple Pokemon matched '{query}'. Be a bit more specific.", ""]
                for index, pokemon in enumerate(matches[:8], start=1):
                    lines.append(f"{index}. {pokemon_display_label(pokemon, trainer.display_mode)}")
                await event.respond("\n".join(lines))
                return

            pokemon = matches[0]
            if has_form_state(pokemon):
                await event.respond("Unfuse or reset this Pokemon's form before using Max Soup.")
                return
            target_species = self._max_soup_target_species(pokemon.species)
            if species_key(pokemon.species).endswith("-gmax"):
                await event.respond(f"{pokemon.species} is already in its Gmax form.")
                return
            if target_species is None:
                await event.respond(f"{pokemon.species} cannot use Max Soup.")
                return
            if not inventories.consume_key_item(trainer, KEY_ITEM_MAX_SOUP):
                await event.respond("You do not have any Max Soup.")
                return

            generated = await self.generator.generate_pokemon(
                species=target_species,
                level=int(pokemon.level),
                region=str(trainer.current_region),
                source_kind=str(pokemon.source_kind),
                friendship=int(pokemon.friendship),
                shiny=bool(pokemon.shiny),
                item=str(pokemon.item or ""),
                untradeable=bool(pokemon.untradeable),
                unreleasable=bool(pokemon.unreleasable),
                ivs={
                    "hp": int(pokemon.iv_hp),
                    "atk": int(pokemon.iv_atk),
                    "def": int(pokemon.iv_def),
                    "spa": int(pokemon.iv_spa),
                    "spd": int(pokemon.iv_spd),
                    "spe": int(pokemon.iv_spe),
                },
                evs={
                    "hp": int(pokemon.ev_hp),
                    "atk": int(pokemon.ev_atk),
                    "def": int(pokemon.ev_def),
                    "spa": int(pokemon.ev_spa),
                    "spd": int(pokemon.ev_spd),
                    "spe": int(pokemon.ev_spe),
                },
                moves=list(json.loads(pokemon.moves_json)),
                nature=str(pokemon.nature),
                ability=str(pokemon.ability),
                gender=str(pokemon.gender or ""),
                tera_type=str(pokemon.tera_type or ""),
            )
            pokemons.evolve_owned_pokemon(pokemon, generated)
            session.expunge(pokemon)
            updated_pokemon = pokemon

        if updated_pokemon is None:
            await event.respond("Max Soup failed.")
            return
        await self.stats.send_stats_card(event, updated_pokemon, page="summary")

    def _formchange_owned_items(self, inventories: InventoryRepository, trainer) -> list[str]:
        return [item_name for item_name in FORM_CHANGE_ITEM_ORDER if inventories.key_item_count(trainer, item_name) > 0]

    def _formchange_paginate(self, items: list, *, page: int) -> tuple[list, int, int]:
        total = len(items)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // FORM_CHANGE_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * FORM_CHANGE_PAGE_SIZE
        end = start + FORM_CHANGE_PAGE_SIZE
        return items[start:end], total, current_page

    def _formchange_item_buttons(self, item_names: list[str]) -> list[list[Button]] | None:
        if not item_names:
            return None
        buttons = [
            Button.inline(item_name, data=f"form:item:{item_key(item_name)}".encode("utf-8"))
            for item_name in item_names
        ]
        return chunk_buttons(buttons, per_row=2)

    def _formchange_picker_buttons(
        self,
        *,
        action_prefix: str,
        item_name_key: str,
        page: int,
        total: int,
        items: list,
        extra: str = "",
        back_data: str = "form:items",
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * FORM_CHANGE_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(
                str(index),
                data=f"{action_prefix}:{item_name_key}:{extra}{pokemon.id}".encode("utf-8"),
            )
            for index, pokemon in enumerate(items, start=start)
        ]
        for index in range(0, len(number_buttons), 5):
            rows.append(number_buttons[index:index + 5])
        max_page = max(0, (max(total, 1) - 1) // FORM_CHANGE_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"form:page:{action_prefix.split(':')[-1]}:{item_name_key}:{extra}{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="form:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"form:page:{action_prefix.split(':')[-1]}:{item_name_key}:{extra}{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Back", data=back_data.encode("utf-8"))])
        return rows

    async def on_formchange(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._reply_dm_only(event)
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            owned_items = self._formchange_owned_items(inventories, trainer)
        if not owned_items:
            await event.respond("You do not own any form-change or fusion key items right now.")
            return
        await event.respond(
            "Form Change\n\nChoose a key item to use.",
            buttons=self._formchange_item_buttons(owned_items),
        )

    async def _show_formchange_host_menu(self, event: CallbackQuery.Event, *, item_name_key: str, page: int) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = item_name_from_key(item_name_key)
            if item_name is None or inventories.key_item_count(trainer, item_name) <= 0:
                await safe_event_edit(event, "You no longer own that key item.", buttons=None)
                return
            hosts = [pokemon for pokemon in self.sorted_owned_pokemon(trainer, pokemons) if compatible_host(item_name_key, pokemon)]
            items, total, current_page = self._formchange_paginate(hosts, page=page)
            lines = ["Form Change", "", f"Item: {item_name}", ""]
            if item_requires_partner(item_name_key):
                lines.append("Choose the host Pokemon.")
                lines.append("Pick an already fused host again to unfuse it.")
            else:
                lines.append("Choose a compatible Pokemon.")
            lines.append("")
            if not items:
                lines.append("No compatible Pokemon are available.")
            else:
                start = current_page * FORM_CHANGE_PAGE_SIZE + 1
                for index, pokemon in enumerate(items, start=start):
                    lines.append(f"{index}. {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
            lines.extend(["", f"Page: {current_page + 1}/{max(1, ((max(total, 1) - 1) // FORM_CHANGE_PAGE_SIZE) + 1)}"])
            await safe_event_edit(
                event,
                "\n".join(lines),
                buttons=self._formchange_picker_buttons(
                    action_prefix="form:pickhost",
                    item_name_key=item_name_key,
                    page=current_page,
                    total=total,
                    items=items,
                    back_data="form:items",
                ),
            )

    async def _show_formchange_partner_menu(self, event: CallbackQuery.Event, *, item_name_key: str, host_id: int, page: int) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = item_name_from_key(item_name_key)
            if item_name is None or inventories.key_item_count(trainer, item_name) <= 0:
                await safe_event_edit(event, "You no longer own that key item.", buttons=None)
                return
            host = pokemons.get_owned_pokemon(trainer, host_id)
            if host is None or not compatible_host(item_name_key, host):
                await safe_event_edit(event, "That Pokemon is no longer available for this item.", buttons=None)
                return
            partners = [
                pokemon
                for pokemon in self.sorted_owned_pokemon(trainer, pokemons)
                if compatible_partner(item_name_key, host, pokemon)
            ]
            items, total, current_page = self._formchange_paginate(partners, page=page)
            lines = [
                "Form Change",
                "",
                f"Item: {item_name}",
                f"Host: {effective_species(host)}",
                "",
                "Choose the partner Pokemon.",
                "",
            ]
            if not items:
                lines.append("No compatible partner is available.")
            else:
                start = current_page * FORM_CHANGE_PAGE_SIZE + 1
                for index, pokemon in enumerate(items, start=start):
                    lines.append(f"{index}. {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
            lines.extend(["", f"Page: {current_page + 1}/{max(1, ((max(total, 1) - 1) // FORM_CHANGE_PAGE_SIZE) + 1)}"])
            await safe_event_edit(
                event,
                "\n".join(lines),
                buttons=self._formchange_picker_buttons(
                    action_prefix="form:pickpartner",
                    item_name_key=item_name_key,
                    page=current_page,
                    total=total,
                    items=items,
                    extra=f"{host_id}:",
                    back_data=f"form:item:{item_name_key}",
                ),
            )

    async def _show_formchange_form_menu(self, event: CallbackQuery.Event, *, item_name_key: str, pokemon_id: int) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = item_name_from_key(item_name_key)
            if item_name is None or inventories.key_item_count(trainer, item_name) <= 0:
                await safe_event_edit(event, "You no longer own that key item.", buttons=None)
                return
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None or not compatible_host(item_name_key, pokemon):
                await safe_event_edit(event, "That Pokemon is no longer available for this item.", buttons=None)
                return
            current_species = effective_species(pokemon)
            targets = item_form_targets(item_name_key)
            lines = [
                "Form Change",
                "",
                f"Item: {item_name}",
                f"Pokemon: {current_species}",
                "",
                "Choose a form.",
            ]
            buttons: list[Button] = []
            for index, target_species in enumerate(targets):
                label = target_species + (" *" if normalize_lookup(target_species) == normalize_lookup(current_species) else "")
                buttons.append(Button.inline(label, data=f"form:choose:{item_name_key}:{pokemon.id}:{index}".encode("utf-8")))
            await safe_event_edit(
                event,
                "\n".join(lines),
                buttons=chunk_buttons(buttons, per_row=2) + [[Button.inline("Back", data=f"form:item:{item_name_key}".encode("utf-8"))]],
            )

    def _restore_partner_slots(
        self,
        trainer,
        trainers: TrainerRepository,
        teams: TeamRepository,
        partner_snapshot: dict[str, Any],
        partner,
    ) -> None:
        restored = False
        desired_slots = {
            int(slot_index)
            for slot_index in (partner_snapshot.get("party_slots") or [])
            if int(slot_index) > 0
        }
        for slot in trainer.party_slots:
            if slot.slot_index in desired_slots and slot.pokemon_id is None:
                slot.pokemon = partner
                restored = True
        if not restored:
            trainers.place_in_first_party_slot(trainer, partner)

        team_map = {team.id: team for team in teams.list_teams(trainer)}
        for entry in partner_snapshot.get("team_slots") or []:
            if not isinstance(entry, dict):
                continue
            team = team_map.get(int(entry.get("team_id") or 0))
            slot_index = int(entry.get("slot_index") or 0)
            if team is None or slot_index <= 0:
                continue
            target_slot = next((slot for slot in teams.team_slots(team) if slot.slot_index == slot_index), None)
            if target_slot is None or target_slot.pokemon_id is not None:
                continue
            teams.assign_pokemon(team, slot_index, partner)

    async def _apply_form_species_change(
        self,
        event: CallbackQuery.Event,
        *,
        item_name_key: str,
        pokemon_id: int,
        target_species: str,
    ) -> None:
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        updated_pokemon = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = item_name_from_key(item_name_key)
            if item_name is None or inventories.key_item_count(trainer, item_name) <= 0:
                await event.answer("You no longer own that key item.", alert=True)
                return
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None or not compatible_host(item_name_key, pokemon):
                await event.answer("That Pokemon is no longer available for this item.", alert=True)
                return
            if normalize_lookup(target_species) == normalize_lookup(pokemon.species):
                set_form_state(pokemon, None)
            else:
                set_form_state(
                    pokemon,
                    {
                        "kind": "form",
                        "item": item_name,
                        "display_species": target_species,
                    },
                )
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            session.expunge(pokemon)
            updated_pokemon = pokemon

        if updated_pokemon is None:
            await event.answer("That form change failed.", alert=True)
            return
        await safe_event_edit(event, f"{effective_species(updated_pokemon)} is ready.", buttons=None)
        await self.stats.send_stats_card(event, updated_pokemon, page="summary")
        await event.answer("Form changed.")

    async def _apply_fusion(
        self,
        event: CallbackQuery.Event,
        *,
        item_name_key: str,
        host_id: int,
        partner_id: int,
    ) -> None:
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        updated_pokemon = None
        partner_species = ""
        signature_prompts: list[dict[str, Any]] = []
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = item_name_from_key(item_name_key)
            if item_name is None or inventories.key_item_count(trainer, item_name) <= 0:
                await event.answer("You no longer own that key item.", alert=True)
                return
            host = pokemons.get_owned_pokemon(trainer, host_id)
            partner = pokemons.get_owned_pokemon(trainer, partner_id)
            if host is None or partner is None:
                await event.answer("One of those Pokemon is no longer available.", alert=True)
                return
            if not compatible_host(item_name_key, host) or not compatible_partner(item_name_key, host, partner):
                await event.answer("Those Pokemon are not compatible with this item.", alert=True)
                return
            result_species = fusion_result_species(item_name_key, partner.species)
            if not result_species:
                await event.answer("Those Pokemon cannot fuse with this item.", alert=True)
                return
            partner_species = partner.species
            partner_snapshot = snapshot_owned_pokemon(partner)
            pokemons.clear_slots_for_pokemon(partner)
            pokemons.delete_owned_pokemon(partner)
            set_form_state(
                host,
                {
                    "kind": "fusion",
                    "item": item_name,
                    "display_species": result_species,
                    "partner_species": partner_species,
                    "partner_snapshot": partner_snapshot,
                },
            )
            signature_prompts = self.encounters.queue_fusion_signature_prompts(trainer, host)
            pokemons.sync_packed_set(host, self.pokemon_data)
            session.expunge(host)
            updated_pokemon = host

        if updated_pokemon is None:
            await event.answer("Fusion failed.", alert=True)
            return
        await safe_event_edit(
            event,
            f"{updated_pokemon.species} fused with {partner_species} into {effective_species(updated_pokemon)}.",
            buttons=None,
        )
        await self.stats.send_stats_card(event, updated_pokemon, page="summary")
        if signature_prompts:
            await self.encounters._send_progression_followups(
                event.sender_id,
                level_up_messages=[],
                pending_prompts=signature_prompts,
            )
        await event.answer("Fusion complete.")

    async def _remove_fusion(self, event: CallbackQuery.Event, *, item_name_key: str, pokemon_id: int) -> None:
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        updated_pokemon = None
        partner_species = ""
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            teams = TeamRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = item_name_from_key(item_name_key)
            if item_name is None or inventories.key_item_count(trainer, item_name) <= 0:
                await event.answer("You no longer own that key item.", alert=True)
                return
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            state = load_form_state(pokemon)
            if not state or active_item_key(pokemon) != item_name_key or str(state.get("kind") or "") != "fusion":
                await event.answer("That Pokemon is not fused with this item.", alert=True)
                return
            partner_snapshot = dict(state.get("partner_snapshot") or {})
            if not partner_snapshot:
                await event.answer("The fused partner data could not be restored.", alert=True)
                return
            prompt_entries = self.encounters._load_pending_move_entries(trainer)
            remaining_entries = [
                entry
                for entry in prompt_entries
                if not (
                    str(entry.get("kind") or "") == "fusion_signature"
                    and int(entry.get("pokemon_id") or 0) == int(pokemon.id)
                )
            ]
            if len(remaining_entries) != len(prompt_entries):
                self.encounters._store_pending_move_entries(trainer, remaining_entries)
            partner_species = str(state.get("partner_species") or partner_snapshot.get("species") or "Pokemon")
            restored_partner = pokemons.create_owned_pokemon(trainer=trainer, data=restore_payload_from_snapshot(partner_snapshot))
            self._restore_partner_slots(trainer, trainers, teams, partner_snapshot, restored_partner)
            set_form_state(pokemon, None)
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            session.expunge(pokemon)
            updated_pokemon = pokemon

        if updated_pokemon is None:
            await event.answer("Unfusion failed.", alert=True)
            return
        await safe_event_edit(
            event,
            f"{effective_species(updated_pokemon)} returned to its base form and {partner_species} was restored.",
            buttons=None,
        )
        await self.stats.send_stats_card(event, updated_pokemon, page="summary")
        await event.answer("Pokemon unfused.")

    async def on_train(self, event: NewMessage.Event) -> None:
        if not await self._ensure_command_unlocked(event, "train"):
            return
        if not event.is_private:
            await self._reply_dm_command_button(event, "train")
            return
        active_session = self._active_training_session(event.sender_id)
        if active_session is not None:
            await self._show_training_pokemon_menu(event, session=active_session, page=0)
            return

        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return

        await event.respond(
            self.training_entry_text(),
            buttons=self.training_entry_buttons(),
            parse_mode="md",
            link_preview=True,
        )

    async def on_breed(self, event: NewMessage.Event) -> None:
        if not await self._ensure_command_unlocked(event, "breed"):
            return
        if not event.is_private:
            await self._reply_dm_command_button(event, "breed")
            return
        await self.daycare.on_breed(event)

    async def on_breeddata(self, event: NewMessage.Event) -> None:
        if not await self._ensure_command_unlocked(event, "breeddata"):
            return
        if not event.is_private:
            await self._reply_dm_command_button(event, "breeddata")
            return
        await self.daycare.on_breeddata(event)

    async def on_incubate(self, event: NewMessage.Event) -> None:
        if not await self._ensure_command_unlocked(event, "incubate"):
            return
        if not event.is_private:
            await self._reply_dm_command_button(event, "incubate")
            return
        await self.daycare.on_incubate(event)

    async def on_incubator(self, event: NewMessage.Event) -> None:
        await self.on_incubate(event)

    async def on_forcecomplete(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return

        parts = str(event.raw_text or "").split(maxsplit=1)
        args_text = parts[1].strip() if len(parts) > 1 else ""
        if not args_text:
            target_id = int(event.sender_id or 0)
            changed = await self.daycare.forcecomplete_user(target_id)
            await self.daycare._tick(user_id=target_id)
            if not changed:
                await event.respond("No active daycare or egg timers were running for that user.")
                return
            await event.respond(
                f"Force completed `{target_id}`: " + ", ".join(changed) + ".",
                parse_mode="md",
            )
            return

        if normalize_lookup(args_text) in {"all", "-all"}:
            changed_by_user = await self.daycare.forcecomplete_all_users()
            await self.daycare._tick()
            if not changed_by_user:
                await event.respond("No active daycare or egg timers were running for any user.")
                return
            lines = [f"Force completed `{len(changed_by_user)}` user(s).", ""]
            for user_id in sorted(changed_by_user):
                lines.append(f"• `{user_id}`: {', '.join(changed_by_user[user_id])}")
            await event.respond("\n".join(lines), parse_mode="md")
            return

        ids = self._extract_integer_arguments(args_text)
        if not ids:
            await event.respond("Usage: `/forcecomplete <user_id>` or `/forcecomplete -all`", parse_mode="md")
            return
        target_id = int(ids[0])
        changed = await self.daycare.forcecomplete_user(target_id)
        if changed is None:
            await event.respond(f"No trainer data exists for `{target_id}`.", parse_mode="md")
            return
        await self.daycare._tick(user_id=target_id)
        if not changed:
            await event.respond(f"No active daycare or egg timers were running for `{target_id}`.", parse_mode="md")
            return
        await event.respond(
            f"Force completed `{target_id}`: " + ", ".join(changed) + ".",
            parse_mode="md",
        )

    def _training_lock_reason(self, user_id: int) -> str | None:
        reason = self.battle_service.pvp_lock_reason(user_id)
        if reason:
            return reason
        reason = self.battle_service.encounter_lock_reason(user_id)
        if reason:
            return reason
        if self.encounters.active_by_user.get(user_id) is not None:
            return "Finish your current encounter before training a Pokemon."
        return None

    def _active_training_session(self, user_id: int) -> TrainingSession | None:
        session = self.training_sessions.get(user_id)
        if session is None:
            return None
        if session.expires_at <= datetime.utcnow():
            self.training_sessions.pop(user_id, None)
            return None
        return session

    def _create_training_session(self, user_id: int, duration_key: str) -> TrainingSession:
        now = datetime.utcnow()
        session = TrainingSession(
            session_id=secrets.token_hex(3),
            user_id=int(user_id),
            duration_key=duration_key,
            duration_label=TRAINING_DURATION_LABELS[duration_key],
            cost_vp=TRAINING_DURATION_COSTS[duration_key],
            allowed_pokemon=TRAINING_POKEMON_LIMITS[duration_key],
            started_at=now,
            expires_at=now + timedelta(minutes=TRAINING_DURATION_MINUTES[duration_key]),
        )
        self.training_sessions[user_id] = session
        return session

    def _training_time_left_text(self, session: TrainingSession) -> str:
        remaining_seconds = max(0, int((session.expires_at - datetime.utcnow()).total_seconds()))
        if remaining_seconds <= 0:
            return "0m"
        total_minutes = remaining_seconds // 60
        days, remainder = divmod(total_minutes, 24 * 60)
        hours, minutes = divmod(remainder, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes and len(parts) < 2:
            parts.append(f"{minutes}m")
        if not parts:
            parts.append("under 1m")
        return " ".join(parts)

    async def _present_training_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        text: str,
        *,
        buttons=None,
        edit: bool = False,
    ) -> None:
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, text, buttons=buttons, parse_mode="md", link_preview=True)
            if edited:
                return
        await event.respond(text, buttons=buttons, parse_mode="md", link_preview=True)

    async def _require_training_session(
        self,
        event: CallbackQuery.Event,
        session_id: str,
    ) -> TrainingSession | None:
        active_session = self.training_sessions.get(event.sender_id)
        if active_session is None:
            await event.answer("No active training session. Use /train to start one.", alert=True)
            return None
        if active_session.expires_at <= datetime.utcnow():
            self.training_sessions.pop(event.sender_id, None)
            await safe_event_edit(event, "Training time is up. Session ended.", buttons=None)
            await event.answer("Training time is up.", alert=True)
            return None
        if active_session.session_id != session_id:
            await event.answer("That training menu is stale. Use /train to reopen your session.", alert=True)
            return None
        return active_session

    def _training_can_modify_pokemon(self, session: TrainingSession, pokemon_id: int) -> bool:
        return int(pokemon_id) in session.trained_pokemon_ids or len(session.trained_pokemon_ids) < int(session.allowed_pokemon)

    def _training_register_pokemon(self, session: TrainingSession, pokemon_id: int) -> None:
        session.trained_pokemon_ids.add(int(pokemon_id))

    def _training_species_key(self, species: str) -> str:
        return normalize_lookup(species)

    async def _training_moves_for_species(self, species: str) -> list[dict[str, object]]:
        cache_key = self._training_species_key(species)
        cached = self._training_move_catalog.get(cache_key)
        if cached is None:
            try:
                cached = await self.generator.list_training_moves(species)
            except ShowdownBridgeError:
                cached = []
            self._training_move_catalog[cache_key] = list(cached)
        return [dict(entry) for entry in cached]

    async def _training_levelup_moves_for_species(self, species: str) -> list[dict[str, object]]:
        cache_key = self._training_species_key(species)
        cached = self._training_levelup_move_catalog.get(cache_key)
        if cached is None:
            by_key: dict[str, dict[str, object]] = {}
            for entry in await self._training_moves_for_species(species):
                methods = [str(value) for value in list(entry.get("methods") or []) if str(value).strip()]
                if "Level Up" not in methods:
                    continue
                move_name = str(entry.get("name") or "").strip()
                move_key_value = normalize_lookup(move_name)
                if not move_key_value:
                    continue
                by_key[move_key_value] = {
                    "id": str(entry.get("id") or ""),
                    "name": move_name,
                    "methods": ["Level Up"],
                    "level": entry.get("level"),
                    "tm_name": "",
                }
            cached = sorted(
                by_key.values(),
                key=lambda entry: (
                    int(entry.get("level") or 0),
                    str(entry.get("name") or "").lower(),
                ),
            )
            self._training_levelup_move_catalog[cache_key] = list(cached)
        return [dict(entry) for entry in cached]

    def _training_tm_lookup(self, inventories: InventoryRepository, trainer) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for tm_name, count in inventories.tm_counts(trainer).items():
            if int(count) <= 0:
                continue
            label = str(tm_name).strip()
            move_text = label.split(" - ", 1)[1] if " - " in label else label
            move_id = str(move_text).replace("-", " ").strip()
            move_key_value = normalize_lookup(move_text) or normalize_lookup(move_id)
            if move_key_value and move_key_value not in lookup:
                lookup[move_key_value] = label
        return lookup

    async def _training_move_options(
        self,
        trainer,
        inventories: InventoryRepository,
        pokemon,
    ) -> list[dict[str, object]]:
        tm_lookup = self._training_tm_lookup(inventories, trainer)
        options: list[dict[str, object]] = []
        for entry in await self._training_moves_for_species(pokemon.species):
            move_name = str(entry.get("name") or "").strip()
            move_key_value = normalize_lookup(move_name) or normalize_lookup(str(entry.get("id") or ""))
            if not move_key_value:
                continue
            methods = [str(value) for value in (entry.get("methods") or []) if str(value).strip()]
            allowed_methods: list[str] = []
            tm_name = ""
            if "Level Up" in methods:
                allowed_methods.append("Level Up")
            if "TM" in methods and move_key_value in tm_lookup:
                allowed_methods.append("TM")
                tm_name = tm_lookup[move_key_value]
            if not allowed_methods:
                continue
            option = dict(entry)
            option["methods"] = allowed_methods
            option["tm_name"] = tm_name
            options.append(option)
        return options

    def _training_move_required_tm(self, entry: dict[str, object]) -> str | None:
        methods = [str(value) for value in (entry.get("methods") or []) if str(value).strip()]
        if "TM" not in methods or "Level Up" in methods:
            return None
        tm_name = str(entry.get("tm_name") or "").strip()
        return tm_name or None

    async def _training_abilities_for_species(self, species: str) -> list[dict[str, object]]:
        cache_key = self._training_species_key(species)
        cached = self._training_ability_catalog.get(cache_key)
        if cached is None:
            cached = await self.generator.list_abilities(species)
            self._training_ability_catalog[cache_key] = list(cached)
        return [dict(entry) for entry in cached]

    async def _breeding_profile_for_species(self, species: str) -> dict[str, Any]:
        cache_key = self._training_species_key(species)
        cached = self._breeding_profile_cache.get(cache_key)
        if cached is None:
            try:
                cached = await self.generator.breeding_profile(species)
            except ShowdownBridgeError:
                cached = {}
            self._breeding_profile_cache[cache_key] = dict(cached)
        return dict(cached)

    async def _prime_pokemon_move_history(self, pokemon) -> None:
        current_moves = [
            str(move).strip()
            for move in json.loads(getattr(pokemon, "moves_json", "[]") or "[]")
            if str(move).strip()
        ]
        if not current_moves:
            return

        training_moves = await self._training_moves_for_species(str(pokemon.species))
        training_by_key = {
            normalize_lookup(str(entry.get("name") or "")): {
                str(method).strip()
                for method in list(entry.get("methods") or [])
                if str(method).strip()
            }
            for entry in training_moves
            if normalize_lookup(str(entry.get("name") or ""))
        }
        breeding_profile = await self._breeding_profile_for_species(str(pokemon.species))
        egg_move_keys = {
            normalize_lookup(str(entry.get("name") or ""))
            for entry in list(breeding_profile.get("egg_moves") or [])
            if isinstance(entry, dict) and normalize_lookup(str(entry.get("name") or ""))
        }

        for move_name in current_moves:
            move_key_value = normalize_lookup(move_name)
            if not move_key_value:
                continue
            categories: list[str] = []
            methods = training_by_key.get(move_key_value, set())
            if "TM" in methods:
                categories.append("tm")
            if "Tutor" in methods:
                categories.append("tutor")
            if move_key_value in egg_move_keys:
                categories.append("egg")
            if categories:
                record_move_history(pokemon, categories=categories, move_names=[move_name])

    def training_entry_text(self) -> str:
        return (
            "[\u200c](https://files.catbox.moe/3rwynz.jpg)\n"
            "**TRAINING SPOT**\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Welcome to the Training Spot. Enhance your Pokemon's EVs, IVs, Level, and Moves here.\n\n"
            "How would you like to spend your time today?"
        )

    def training_entry_buttons(self) -> list[list[Button]]:
        return [[
            Button.inline("Enter", data="train:start".encode("utf-8")),
            Button.inline("Go Back", data="train:leave".encode("utf-8")),
        ]]

    def training_duration_text(self, *, current_vp: int | None = None) -> str:
        lines = [
            "[\u200c](https://files.catbox.moe/3rwynz.jpg)",
            "**TRAINING SPOT**",
            "━━━━━━━━━━━━━━━━━━━━━",
            "Select a training package. This determines how many Pokemon you can train within the next hour.",
            "",
            "`[ 2 PKMN ]` - 5,000 VP",
            "`[ 5 PKMN ]` - 10,000 VP",
            "`[10 PKMN ]` - 20,000 VP",
            "`[30 PKMN ]` - 50,000 VP",
        ]
        if current_vp is not None:
            lines.extend(["━━━━━━━━━━━━━━━━━━━━━", f"Available VP: `{current_vp:,}`"])
        return "\n".join(lines)

    def training_duration_buttons(self) -> list[list[Button]]:
        return [
            [
                Button.inline("2 Pokemon", data="train:buy:2pk".encode("utf-8")),
                Button.inline("5 Pokemon", data="train:buy:5pk".encode("utf-8")),
            ],
            [
                Button.inline("10 Pokemon", data="train:buy:10pk".encode("utf-8")),
                Button.inline("30 Pokemon", data="train:buy:30pk".encode("utf-8")),
            ],
            [Button.inline("Back", data="train:entry".encode("utf-8"))],
        ]

    def _training_header_lines(self, session: TrainingSession, *, title: str) -> list[str]:
        trained_count = len(session.trained_pokemon_ids)
        remaining_count = max(0, int(session.allowed_pokemon) - trained_count)
        return [
            f"**{title.upper()}**",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"Package    :: `{session.duration_label}`",
            f"Time Left  :: `{self._training_time_left_text(session)}`",
            f"Allowances :: `{remaining_count} / {session.allowed_pokemon}`",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]

    def training_pokemon_picker_text(
        self,
        session: TrainingSession,
        trainer,
        *,
        page: int,
        total: int,
        items: list,
        notice: str | None = None,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE) + 1)
        lines = self._training_header_lines(session, title="Training Spot")
        if notice:
            lines.extend([f"**Update:** _{notice}_", ""])
        lines.extend(["Choose the Pokemon you wish to train.", ""])
        if not items:
            lines.append("You do not own any Pokemon yet.")
        else:
            start = page * POKEMON_LIST_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"`[{index:<2}]` {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━", f"Page: {page + 1}/{max_page}"])
        return "\n".join(lines)

    def training_pokemon_picker_buttons(
        self,
        session_id: str,
        *,
        page: int,
        total: int,
        items: list,
    ) -> list[list[Button]] | None:
        if not items and total <= 0:
            return None

        rows: list[list[Button]] = []
        start = page * POKEMON_LIST_PAGE_SIZE + 1
        pick_buttons = [
            Button.inline(
                str(index),
                data=f"train:s:{session_id}:ps:{page}:{pokemon.id}".encode("utf-8"),
            )
            for index, pokemon in enumerate(items, start=start)
        ]
        if pick_buttons:
            rows.extend(chunk_buttons(pick_buttons, per_row=5))

        max_page = max(0, (max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"train:s:{session_id}:pl:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="train:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"train:s:{session_id}:pl:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        return rows

    def training_mode_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        notice: str | None = None,
    ) -> str:
        current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
        lines = self._training_header_lines(session, title="Training Spot")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Lv. {pokemon.level}",
            f"Nature: {pokemon.nature}",
            f"Ability: {pokemon.ability}",
            f"Moves: {', '.join(current_moves) if current_moves else 'None'}",
            "",
            "How do you want to train this Pokemon?",
        ])
        return "\n".join(lines)

    def training_mode_buttons(self, session_id: str, *, page: int, pokemon_id: int) -> list[list[Button]]:
        return [
            [
                Button.inline("Normal Training", data=f"train:s:{session_id}:nm:{page}:{pokemon_id}".encode("utf-8")),
                Button.inline("Hyper Training", data=f"train:s:{session_id}:hm:{page}:{pokemon_id}".encode("utf-8")),
            ],
            [Button.inline("Back", data=f"train:s:{session_id}:pl:{page}".encode("utf-8"))],
        ]

    def training_normal_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        notice: str | None = None,
    ) -> str:
        lines = self._training_header_lines(session, title="Normal Training")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Lv. {pokemon.level}",
            f"Total EVs: {pokemon_total_ev(pokemon)}/{MAX_TOTAL_EVS}",
            "",
            "Choose what you want to change.",
        ])
        return "\n".join(lines)

    def training_normal_buttons(self, session_id: str, *, page: int, pokemon_id: int) -> list[list[Button]]:
        return [
            [
                Button.inline("Level", data=f"train:s:{session_id}:lv:{page}:{pokemon_id}".encode("utf-8")),
                Button.inline("EVs", data=f"train:s:{session_id}:ev:{page}:{pokemon_id}".encode("utf-8")),
                Button.inline("Moves", data=f"train:s:{session_id}:mv:{page}:{pokemon_id}:0".encode("utf-8")),
            ],
            [Button.inline("Back", data=f"train:s:{session_id}:md:{page}:{pokemon_id}".encode("utf-8"))],
        ]

    def training_hyper_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        capsule_count: int,
        patch_count: int,
        bottle_cap_count: int,
        gold_bottle_cap_count: int,
        tera_shard_total: int,
        notice: str | None = None,
    ) -> str:
        lines = self._training_header_lines(session, title="Hyper Training")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Lv. {pokemon.level}",
            f"Mints use bag items. Capsule: {capsule_count} | Patch: {patch_count}",
            f"Bottle Cap: {bottle_cap_count} | Gold Bottle Cap: {gold_bottle_cap_count}",
            f"Tera Shards (all types): {tera_shard_total} | Tera change costs {TERA_TYPE_CHANGE_SHARD_COST}",
            "",
            "Choose what you want to change.",
        ])
        return "\n".join(lines)

    def training_hyper_buttons(self, session_id: str, *, page: int, pokemon_id: int) -> list[list[Button]]:
        return [
            [
                Button.inline("Nature", data=f"train:s:{session_id}:nt:{page}:{pokemon_id}:0".encode("utf-8")),
                Button.inline("Ability", data=f"train:s:{session_id}:ab:{page}:{pokemon_id}".encode("utf-8")),
                Button.inline("IVs", data=f"train:s:{session_id}:iv:{page}:{pokemon_id}".encode("utf-8")),
            ],
            [
                Button.inline("Tera Type", data=f"train:s:{session_id}:tt:{page}:{pokemon_id}".encode("utf-8")),
            ],
            [Button.inline("Back", data=f"train:s:{session_id}:md:{page}:{pokemon_id}".encode("utf-8"))],
        ]

    def training_level_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        notice: str | None = None,
    ) -> str:
        lines = self._training_header_lines(session, title="Normal Training - Level")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} is currently Lv. {pokemon.level}.",
            "Level can only go up here.",
            "",
            "Choose how much to increase.",
        ])
        return "\n".join(lines)

    def training_level_buttons(self, session_id: str, *, page: int, pokemon_id: int) -> list[list[Button]]:
        buttons = [
            Button.inline(
                f"+{step}",
                data=f"train:s:{session_id}:la:{page}:{pokemon_id}:{step}".encode("utf-8"),
            )
            for step in TRAINING_LEVEL_STEPS
        ]
        return [
            buttons,
            [Button.inline("Back", data=f"train:s:{session_id}:nm:{page}:{pokemon_id}".encode("utf-8"))],
        ]

    def training_ev_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        notice: str | None = None,
    ) -> str:
        lines = self._training_header_lines(session, title="Normal Training - EVs")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Total EVs: {pokemon_total_ev(pokemon)}/{MAX_TOTAL_EVS}",
            "",
        ])
        for stat_key in EV_STAT_ORDER:
            lines.append(f"{EV_STAT_LABELS[stat_key]}: {int(getattr(pokemon, f'ev_{stat_key}'))}")
        lines.extend(["", "Choose a stat to adjust."])
        return "\n".join(lines)

    def training_ev_buttons(self, session_id: str, *, page: int, pokemon_id: int) -> list[list[Button]]:
        labels = [
            ("HP", "hp"),
            ("Atk", "atk"),
            ("Def", "def"),
            ("SpA", "spa"),
            ("SpD", "spd"),
            ("Spe", "spe"),
        ]
        rows = chunk_buttons([
            Button.inline(
                label,
                data=f"train:s:{session_id}:es:{page}:{pokemon_id}:{stat_key}".encode("utf-8"),
            )
            for label, stat_key in labels
        ], per_row=3)
        rows.append([Button.inline("Back", data=f"train:s:{session_id}:nm:{page}:{pokemon_id}".encode("utf-8"))])
        return rows

    def training_ev_stat_text(
        self,
        session: TrainingSession,
        pokemon,
        stat_key: str,
        *,
        notice: str | None = None,
    ) -> str:
        stat_label = EV_STAT_LABELS.get(stat_key, stat_key.upper())
        lines = self._training_header_lines(session, title=f"Normal Training - {stat_label}")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | {stat_label} EVs: {int(getattr(pokemon, f'ev_{stat_key}'))}",
            f"Total EVs: {pokemon_total_ev(pokemon)}/{MAX_TOTAL_EVS}",
            "",
            "Use the buttons below to adjust this stat.",
        ])
        return "\n".join(lines)

    def training_ev_stat_buttons(
        self,
        session_id: str,
        *,
        page: int,
        pokemon_id: int,
        stat_key: str,
    ) -> list[list[Button]]:
        negatives = [
            Button.inline(
                str(step),
                data=f"train:s:{session_id}:ea:{page}:{pokemon_id}:{stat_key}:{step}".encode("utf-8"),
            )
            for step in TRAINING_EV_STEPS
            if step < 0
        ]
        positives = [
            Button.inline(
                f"+{step}",
                data=f"train:s:{session_id}:ea:{page}:{pokemon_id}:{stat_key}:{step}".encode("utf-8"),
            )
            for step in TRAINING_EV_STEPS
            if step > 0
        ]
        return [
            negatives,
            positives,
            [Button.inline("Back", data=f"train:s:{session_id}:ev:{page}:{pokemon_id}".encode("utf-8"))],
        ]

    def _training_available_natures(self, inventories: InventoryRepository, trainer) -> list[dict[str, object]]:
        options: list[dict[str, object]] = []
        for nature_name, item_name in TRAINING_NATURE_MINTS:
            count = inventories.held_item_count(trainer, item_name)
            if count <= 0:
                continue
            options.append({
                "nature": nature_name,
                "item_name": item_name,
                "count": count,
            })
        return options

    async def _training_available_abilities(
        self,
        inventories: InventoryRepository,
        trainer,
        pokemon,
    ) -> list[dict[str, object]]:
        options: list[dict[str, object]] = []
        for entry in await self._training_abilities_for_species(pokemon.species):
            name = str(entry.get("name") or "").strip()
            if not name or normalize_lookup(name) == normalize_lookup(pokemon.ability):
                continue
            required_item = ABILITY_PATCH_ITEM if bool(entry.get("hidden")) else ABILITY_CAPSULE_ITEM
            count = inventories.held_item_count(trainer, required_item)
            if count <= 0:
                continue
            options.append({
                "name": name,
                "hidden": bool(entry.get("hidden")),
                "slot": str(entry.get("slot") or ""),
                "required_item": required_item,
                "count": count,
            })
        return options

    def _training_available_tera_types(
        self,
        inventories: InventoryRepository,
        trainer,
        pokemon,
    ) -> list[dict[str, object]]:
        options: list[dict[str, object]] = []
        current_tera = normalize_lookup(str(getattr(pokemon, "tera_type", "") or ""))
        for shard_name in TERA_SHARDS:
            count = inventories.held_item_count(trainer, shard_name)
            if count < TERA_TYPE_CHANGE_SHARD_COST:
                continue
            type_name = str(shard_name).replace(" Tera Shard", "").strip()
            if normalize_lookup(type_name) == current_tera:
                continue
            options.append({
                "type_name": type_name,
                "shard_name": shard_name,
                "count": count,
            })
        return options

    def training_move_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        page: int,
        total: int,
        items: list[dict[str, object]],
        notice: str | None = None,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // TRAINING_MOVE_PAGE_SIZE) + 1)
        current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
        lines = self._training_header_lines(session, title="Normal Training - Moves")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species}",
            "Current Moves:",
        ])
        if current_moves:
            for index, move_name in enumerate(current_moves, start=1):
                lines.append(f"{index}. {move_name}")
        else:
            lines.append("None")
        lines.extend([
            "",
            "Choose a move to teach.",
            "Level Up moves are free. Owned TMs are also allowed here.",
            "Tutor moves are not available in this menu.",
            "",
        ])
        if not items:
            lines.append("No trainable moves found.")
        else:
            start = page * TRAINING_MOVE_PAGE_SIZE + 1
            for index, entry in enumerate(items, start=start):
                methods = [str(value) for value in (entry.get("methods") or []) if str(value).strip()]
                method_labels: list[str] = []
                if "Level Up" in methods:
                    if entry.get("level") is not None:
                        method_labels.append(f"Level {entry['level']}")
                    else:
                        method_labels.append("Level Up")
                if "TM" in methods:
                    tm_name = str(entry.get("tm_name") or "").strip()
                    method_labels.append(tm_name.split(" - ", 1)[0] if tm_name else "TM")
                for method in methods:
                    if method in {"Level Up", "TM"}:
                        continue
                    if method == "Evolution":
                        evolution_species = str(entry.get("evolution_species") or "").strip()
                        method_labels.append(
                            f"Evolution ({evolution_species})" if evolution_species else "Evolution"
                        )
                    else:
                        method_labels.append(method)
                method_text = "/".join(method_labels) if method_labels else "Move"
                lines.append(f"{index}. {entry['name']} ({method_text})")
        lines.extend(["", f"Page: {page + 1}/{max_page}"])
        return "\n".join(lines)

    def training_move_buttons(
        self,
        session_id: str,
        *,
        page: int,
        total: int,
        items: list[dict[str, object]],
        pokemon_id: int,
        pokemon_page: int,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * TRAINING_MOVE_PAGE_SIZE
        pick_buttons = [
            Button.inline(
                str(start + index + 1),
                data=f"train:s:{session_id}:mp:{pokemon_page}:{pokemon_id}:{page}:{index}".encode("utf-8"),
            )
            for index, _ in enumerate(items)
        ]
        if pick_buttons:
            rows.extend(chunk_buttons(pick_buttons, per_row=4))

        max_page = max(0, (max(total, 1) - 1) // TRAINING_MOVE_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"train:s:{session_id}:mv:{pokemon_page}:{pokemon_id}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="train:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"train:s:{session_id}:mv:{pokemon_page}:{pokemon_id}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Back", data=f"train:s:{session_id}:nm:{pokemon_page}:{pokemon_id}".encode("utf-8"))])
        return rows

    def training_move_replace_text(
        self,
        session: TrainingSession,
        pokemon,
        move_name: str,
    ) -> str:
        current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
        lines = self._training_header_lines(session, title="Normal Training - Replace Move")
        lines.extend([
            f"{pokemon.species} wants to learn {move_name}.",
            "",
            "Choose which move to replace.",
            "",
        ])
        for index, current_move in enumerate(current_moves, start=1):
            lines.append(f"{index}. {current_move}")
        return "\n".join(lines)

    def training_move_replace_buttons(
        self,
        session_id: str,
        *,
        pokemon_page: int,
        pokemon_id: int,
        move_page: int,
        move_index: int,
        move_count: int,
    ) -> list[list[Button]]:
        replace_buttons = [
            Button.inline(
                str(index),
                data=f"train:s:{session_id}:mr:{pokemon_page}:{pokemon_id}:{move_page}:{move_index}:{index - 1}".encode("utf-8"),
            )
            for index in range(1, move_count + 1)
        ]
        return [
            replace_buttons,
            [Button.inline("Back", data=f"train:s:{session_id}:mv:{pokemon_page}:{pokemon_id}:{move_page}".encode("utf-8"))],
        ]

    def training_nature_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        page: int,
        total: int,
        items: list[dict[str, object]],
        notice: str | None = None,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // TRAINING_NATURE_PAGE_SIZE) + 1)
        lines = self._training_header_lines(session, title="Hyper Training - Nature")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Current Nature: {pokemon.nature}",
            "",
        ])
        if not items:
            lines.append("You do not have any usable mints right now.")
        else:
            start = page * TRAINING_NATURE_PAGE_SIZE + 1
            for index, entry in enumerate(items, start=start):
                lines.append(f"{index}. {entry['nature']} ({entry['item_name']} x{entry['count']})")
        lines.extend(["", f"Page: {page + 1}/{max_page}"])
        return "\n".join(lines)

    def training_nature_buttons(
        self,
        session_id: str,
        *,
        page: int,
        total: int,
        items: list[dict[str, object]],
        pokemon_id: int,
        pokemon_page: int,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * TRAINING_NATURE_PAGE_SIZE
        pick_buttons = [
            Button.inline(
                str(start + index + 1),
                data=f"train:s:{session_id}:na:{pokemon_page}:{pokemon_id}:{page}:{index}".encode("utf-8"),
            )
            for index, _ in enumerate(items)
        ]
        if pick_buttons:
            rows.extend(chunk_buttons(pick_buttons, per_row=4))

        max_page = max(0, (max(total, 1) - 1) // TRAINING_NATURE_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"train:s:{session_id}:nt:{pokemon_page}:{pokemon_id}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="train:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"train:s:{session_id}:nt:{pokemon_page}:{pokemon_id}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Back", data=f"train:s:{session_id}:hm:{pokemon_page}:{pokemon_id}".encode("utf-8"))])
        return rows

    def training_ability_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        options: list[dict[str, object]],
        notice: str | None = None,
    ) -> str:
        lines = self._training_header_lines(session, title="Hyper Training - Ability")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Current Ability: {pokemon.ability}",
            "",
        ])
        if not options:
            lines.append("You do not have a valid capsule or patch for another ability right now.")
        else:
            for index, entry in enumerate(options, start=1):
                lines.append(f"{index}. {entry['name']} ({entry['required_item']} x{entry['count']})")
        return "\n".join(lines)

    def training_ability_buttons(
        self,
        session_id: str,
        *,
        pokemon_page: int,
        pokemon_id: int,
        options: list[dict[str, object]],
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        if options:
            rows.extend(chunk_buttons([
                Button.inline(
                    str(index),
                    data=f"train:s:{session_id}:aa:{pokemon_page}:{pokemon_id}:{index - 1}".encode("utf-8"),
                )
                for index, _ in enumerate(options, start=1)
            ], per_row=3))
        rows.append([Button.inline("Back", data=f"train:s:{session_id}:hm:{pokemon_page}:{pokemon_id}".encode("utf-8"))])
        return rows

    def training_iv_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        bottle_cap_count: int,
        gold_bottle_cap_count: int,
        notice: str | None = None,
    ) -> str:
        lines = self._training_header_lines(session, title="Hyper Training - IVs")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species}",
            f"Bottle Cap: {bottle_cap_count} | Gold Bottle Cap: {gold_bottle_cap_count}",
            "",
        ])
        for stat_key in EV_STAT_ORDER:
            lines.append(f"{EV_STAT_LABELS[stat_key]} IV: {int(getattr(pokemon, f'iv_{stat_key}'))}")
        lines.extend([
            "",
            "Choose one IV to max, or use Gold Bottle Cap to max all 6.",
        ])
        return "\n".join(lines)

    def training_iv_buttons(
        self,
        session_id: str,
        *,
        pokemon_page: int,
        pokemon_id: int,
        can_single: bool,
        can_all: bool,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        if can_single:
            rows.extend(chunk_buttons([
                Button.inline(
                    label,
                    data=f"train:s:{session_id}:ia:{pokemon_page}:{pokemon_id}:{stat_key}".encode("utf-8"),
                )
                for label, stat_key in (
                    ("HP", "hp"),
                    ("Atk", "atk"),
                    ("Def", "def"),
                    ("SpA", "spa"),
                    ("SpD", "spd"),
                    ("Spe", "spe"),
                )
            ], per_row=3))
        if can_all:
            rows.append([Button.inline("All 6", data=f"train:s:{session_id}:ia:{pokemon_page}:{pokemon_id}:all".encode("utf-8"))])
        rows.append([Button.inline("Back", data=f"train:s:{session_id}:hm:{pokemon_page}:{pokemon_id}".encode("utf-8"))])
        return rows

    def training_tera_text(
        self,
        session: TrainingSession,
        pokemon,
        *,
        options: list[dict[str, object]],
        notice: str | None = None,
    ) -> str:
        current_tera = str(getattr(pokemon, "tera_type", "") or "Unknown")
        lines = self._training_header_lines(session, title="Hyper Training - Tera Type")
        if notice:
            lines.extend([f"Update: {notice}", ""])
        lines.extend([
            f"{pokemon.species} | Current Tera Type: {current_tera}",
            f"Cost per change: {TERA_TYPE_CHANGE_SHARD_COST} matching Tera Shards",
            "",
        ])
        if not options:
            lines.append("You do not have enough matching shards for a new Tera Type.")
        else:
            for index, entry in enumerate(options, start=1):
                lines.append(f"{index}. {entry['type_name']} ({entry['shard_name']} x{entry['count']})")
        return "\n".join(lines)

    def training_tera_buttons(
        self,
        session_id: str,
        *,
        pokemon_page: int,
        pokemon_id: int,
        options: list[dict[str, object]],
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        if options:
            rows.extend(chunk_buttons([
                Button.inline(
                    str(index),
                    data=f"train:s:{session_id}:ta:{pokemon_page}:{pokemon_id}:{index - 1}".encode("utf-8"),
                )
                for index, _ in enumerate(options, start=1)
            ], per_row=4))
        rows.append([Button.inline("Back", data=f"train:s:{session_id}:hm:{pokemon_page}:{pokemon_id}".encode("utf-8"))])
        return rows

    async def _show_training_pokemon_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            items, total, current_page = self.pokemon_page(trainer, pokemons, page=page)
            await self._present_training_menu(
                event,
                self.training_pokemon_picker_text(
                    session,
                    trainer,
                    page=current_page,
                    total=total,
                    items=items,
                    notice=notice,
                ),
                buttons=self.training_pokemon_picker_buttons(
                    session.session_id,
                    page=current_page,
                    total=total,
                    items=items,
                ),
                edit=edit,
            )

    async def _show_training_mode_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            await self._present_training_menu(
                event,
                self.training_mode_text(session, pokemon, notice=notice),
                buttons=self.training_mode_buttons(session.session_id, page=page, pokemon_id=pokemon_id),
                edit=edit,
            )

    async def _show_training_normal_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            await self._present_training_menu(
                event,
                self.training_normal_text(session, pokemon, notice=notice),
                buttons=self.training_normal_buttons(session.session_id, page=page, pokemon_id=pokemon_id),
                edit=edit,
            )

    async def _show_training_hyper_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            await self._present_training_menu(
                event,
                self.training_hyper_text(
                    session,
                    pokemon,
                    capsule_count=inventories.held_item_count(trainer, ABILITY_CAPSULE_ITEM),
                    patch_count=inventories.held_item_count(trainer, ABILITY_PATCH_ITEM),
                    bottle_cap_count=inventories.held_item_count(trainer, BOTTLE_CAP_ITEM),
                    gold_bottle_cap_count=inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM),
                    tera_shard_total=sum(inventories.held_item_count(trainer, shard_name) for shard_name in TERA_SHARDS),
                    notice=notice,
                ),
                buttons=self.training_hyper_buttons(session.session_id, page=page, pokemon_id=pokemon_id),
                edit=edit,
            )

    async def _show_training_level_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            await self._present_training_menu(
                event,
                self.training_level_text(session, pokemon, notice=notice),
                buttons=self.training_level_buttons(session.session_id, page=page, pokemon_id=pokemon_id),
                edit=edit,
            )

    async def _show_training_ev_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            await self._present_training_menu(
                event,
                self.training_ev_text(session, pokemon, notice=notice),
                buttons=self.training_ev_buttons(session.session_id, page=page, pokemon_id=pokemon_id),
                edit=edit,
            )

    async def _show_training_ev_stat_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        page: int,
        pokemon_id: int,
        stat_key: str,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            await self._present_training_menu(
                event,
                self.training_ev_stat_text(session, pokemon, stat_key, notice=notice),
                buttons=self.training_ev_stat_buttons(
                    session.session_id,
                    page=page,
                    pokemon_id=pokemon_id,
                    stat_key=stat_key,
                ),
                edit=edit,
            )

    async def _show_training_move_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        pokemon_page: int,
        pokemon_id: int,
        move_page: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            current_move_keys = {
                normalize_lookup(str(move_name))
                for move_name in json.loads(pokemon.moves_json)
            }
            all_moves = [
                entry
                for entry in await self._training_move_options(trainer, inventories, pokemon)
                if normalize_lookup(str(entry.get("name") or "")) not in current_move_keys
            ]
            items, total, current_page = paginate_items(all_moves, page=move_page, per_page=TRAINING_MOVE_PAGE_SIZE)
            await self._present_training_menu(
                event,
                self.training_move_text(
                    session,
                    pokemon,
                    page=current_page,
                    total=total,
                    items=items,
                    notice=notice,
                ),
                buttons=self.training_move_buttons(
                    session.session_id,
                    page=current_page,
                    total=total,
                    items=items,
                    pokemon_id=pokemon_id,
                    pokemon_page=pokemon_page,
                ),
                edit=edit,
            )

    async def _show_training_move_replace_menu(
        self,
        event: CallbackQuery.Event,
        *,
        session: TrainingSession,
        pokemon_page: int,
        pokemon_id: int,
        move_page: int,
        move_index: int,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            current_move_keys = {
                normalize_lookup(str(move_name))
                for move_name in json.loads(pokemon.moves_json)
            }
            all_moves = [
                entry
                for entry in await self._training_move_options(trainer, inventories, pokemon)
                if normalize_lookup(str(entry.get("name") or "")) not in current_move_keys
            ]
            items, _, _ = paginate_items(all_moves, page=move_page, per_page=TRAINING_MOVE_PAGE_SIZE)
            if move_index < 0 or move_index >= len(items):
                await event.answer("That move is no longer on this page.", alert=True)
                return
            move_name = str(items[move_index]["name"])
            current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
            await self._present_training_menu(
                event,
                self.training_move_replace_text(session, pokemon, move_name),
                buttons=self.training_move_replace_buttons(
                    session.session_id,
                    pokemon_page=pokemon_page,
                    pokemon_id=pokemon_id,
                    move_page=move_page,
                    move_index=move_index,
                    move_count=len(current_moves),
                ),
                edit=True,
            )

    async def _show_training_nature_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        pokemon_page: int,
        pokemon_id: int,
        nature_page: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            options = self._training_available_natures(inventories, trainer)
            items, total, current_page = paginate_items(options, page=nature_page, per_page=TRAINING_NATURE_PAGE_SIZE)
            await self._present_training_menu(
                event,
                self.training_nature_text(
                    session,
                    pokemon,
                    page=current_page,
                    total=total,
                    items=items,
                    notice=notice,
                ),
                buttons=self.training_nature_buttons(
                    session.session_id,
                    page=current_page,
                    total=total,
                    items=items,
                    pokemon_id=pokemon_id,
                    pokemon_page=pokemon_page,
                ),
                edit=edit,
            )

    async def _show_training_ability_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        pokemon_page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            options = await self._training_available_abilities(inventories, trainer, pokemon)
            await self._present_training_menu(
                event,
                self.training_ability_text(session, pokemon, options=options, notice=notice),
                buttons=self.training_ability_buttons(
                    session.session_id,
                    pokemon_page=pokemon_page,
                    pokemon_id=pokemon_id,
                    options=options,
                ),
                edit=edit,
            )

    async def _show_training_iv_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        pokemon_page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            bottle_cap_count = inventories.held_item_count(trainer, BOTTLE_CAP_ITEM)
            gold_bottle_cap_count = inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM)
            await self._present_training_menu(
                event,
                self.training_iv_text(
                    session,
                    pokemon,
                    bottle_cap_count=bottle_cap_count,
                    gold_bottle_cap_count=gold_bottle_cap_count,
                    notice=notice,
                ),
                buttons=self.training_iv_buttons(
                    session.session_id,
                    pokemon_page=pokemon_page,
                    pokemon_id=pokemon_id,
                    can_single=(bottle_cap_count > 0 or gold_bottle_cap_count > 0),
                    can_all=(gold_bottle_cap_count > 0),
                ),
                edit=edit,
            )

    async def _show_training_tera_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        session: TrainingSession,
        pokemon_page: int,
        pokemon_id: int,
        notice: str | None = None,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await self._present_training_menu(event, "That Pokemon is no longer available.", edit=edit)
                return
            options = self._training_available_tera_types(inventories, trainer, pokemon)
            await self._present_training_menu(
                event,
                self.training_tera_text(session, pokemon, options=options, notice=notice),
                buttons=self.training_tera_buttons(
                    session.session_id,
                    pokemon_page=pokemon_page,
                    pokemon_id=pokemon_id,
                    options=options,
                ),
                edit=edit,
            )

    async def _apply_training_level(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        page: int,
        pokemon_id: int,
        step: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            if pokemon.level >= 100:
                await event.answer(f"{pokemon.species} is already level 100.", alert=True)
                return
            actual_step = min(max(1, int(step)), 100 - int(pokemon.level))
            old_level = int(pokemon.level)
            old_stats = self.pokemon_data.calculate_stats(pokemon)
            pokemon.level = old_level + actual_step
            pokemon.experience = max(
                int(pokemon.experience),
                int(self.pokemon_data.starting_experience(pokemon.species, pokemon.level)),
            )
            new_stats = self.pokemon_data.calculate_stats(pokemon)
            self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
            notice = f"Level {old_level} -> {pokemon.level}"
        await self._show_training_level_menu(
            event,
            session=session,
            page=page,
            pokemon_id=pokemon_id,
            notice=notice,
            edit=True,
        )
        await event.answer("Level updated.")

    async def _apply_training_ev_change(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        page: int,
        pokemon_id: int,
        stat_key: str,
        delta: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        if stat_key not in EV_STAT_LABELS:
            await event.answer("Choose a valid stat.", alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            old_value = int(getattr(pokemon, f"ev_{stat_key}"))
            total_before = pokemon_total_ev(pokemon)
            requested_delta = int(delta)
            if requested_delta > 0:
                max_for_stat = MAX_EV_PER_STAT - old_value
                max_for_total = MAX_TOTAL_EVS - total_before
                actual_delta = min(requested_delta, max_for_stat, max_for_total)
            else:
                actual_delta = max(requested_delta, -old_value)
            if actual_delta == 0:
                await event.answer("That EV cannot be adjusted any further.", alert=True)
                return
            old_stats = self.pokemon_data.calculate_stats(pokemon)
            new_value = old_value + actual_delta
            setattr(pokemon, f"ev_{stat_key}", new_value)
            new_stats = self.pokemon_data.calculate_stats(pokemon)
            self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
            notice = f"{EV_STAT_LABELS[stat_key]} EV {old_value} -> {new_value} ({actual_delta:+d})"
        await self._show_training_ev_stat_menu(
            event,
            session=session,
            page=page,
            pokemon_id=pokemon_id,
            stat_key=stat_key,
            notice=notice,
            edit=True,
        )
        await event.answer("EV updated.")

    async def _apply_training_move_pick(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        pokemon_page: int,
        pokemon_id: int,
        move_page: int,
        move_index: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            await self._prime_pokemon_move_history(pokemon)
            current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
            current_move_keys = {normalize_lookup(move_name) for move_name in current_moves}
            all_moves = [
                entry
                for entry in await self._training_move_options(trainer, inventories, pokemon)
                if normalize_lookup(str(entry.get("name") or "")) not in current_move_keys
            ]
            items, _, current_page = paginate_items(all_moves, page=move_page, per_page=TRAINING_MOVE_PAGE_SIZE)
            if move_index < 0 or move_index >= len(items):
                await event.answer("That move is no longer on this page.", alert=True)
                return
            selected_entry = dict(items[move_index])
            move_name = str(selected_entry.get("name") or "").strip()
            if not move_name:
                await event.answer("That move is not available right now.", alert=True)
                return
            methods = [str(value) for value in (selected_entry.get("methods") or []) if str(value).strip()]
            required_tm = self._training_move_required_tm(selected_entry)
            if "TM" in methods and "Level Up" not in methods and not required_tm:
                await event.answer("That TM is not available right now.", alert=True)
                return
            if len(current_moves) < 4:
                if required_tm and not inventories.consume_tm(trainer, required_tm, 1):
                    await event.answer(f"You do not have {required_tm} anymore.", alert=True)
                    return
                current_moves.append(move_name)
                pokemon.moves_json = json.dumps(current_moves)
                if required_tm:
                    record_move_history(pokemon, categories=["tm"], move_names=[move_name])
                pokemons.sync_packed_set(pokemon, self.pokemon_data)
                self._training_register_pokemon(session, pokemon_id)
                if required_tm:
                    notice = f"{pokemon.species} learned {move_name} using {required_tm}."
                else:
                    notice = f"{pokemon.species} learned {move_name}."
            else:
                await self._show_training_move_replace_menu(
                    event,
                    session=session,
                    pokemon_page=pokemon_page,
                    pokemon_id=pokemon_id,
                    move_page=current_page,
                    move_index=move_index,
                )
                await event.answer()
                return
        await self._show_training_move_menu(
            event,
            session=session,
            pokemon_page=pokemon_page,
            pokemon_id=pokemon_id,
            move_page=move_page,
            notice=notice,
            edit=True,
        )
        await event.answer("Move learned.")

    async def _apply_training_move_replace(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        pokemon_page: int,
        pokemon_id: int,
        move_page: int,
        move_index: int,
        move_slot: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            await self._prime_pokemon_move_history(pokemon)
            current_moves = [str(move) for move in json.loads(pokemon.moves_json)]
            if move_slot < 0 or move_slot >= len(current_moves):
                await event.answer("That move slot is invalid.", alert=True)
                return
            current_move_keys = {normalize_lookup(move_name) for move_name in current_moves}
            all_moves = [
                entry
                for entry in await self._training_move_options(trainer, inventories, pokemon)
                if normalize_lookup(str(entry.get("name") or "")) not in current_move_keys
            ]
            items, _, _ = paginate_items(all_moves, page=move_page, per_page=TRAINING_MOVE_PAGE_SIZE)
            if move_index < 0 or move_index >= len(items):
                await event.answer("That move is no longer on this page.", alert=True)
                return
            selected_entry = dict(items[move_index])
            move_name = str(selected_entry.get("name") or "").strip()
            if not move_name:
                await event.answer("That move is not available right now.", alert=True)
                return
            methods = [str(value) for value in (selected_entry.get("methods") or []) if str(value).strip()]
            required_tm = self._training_move_required_tm(selected_entry)
            if "TM" in methods and "Level Up" not in methods and not required_tm:
                await event.answer("That TM is not available right now.", alert=True)
                return
            if required_tm and not inventories.consume_tm(trainer, required_tm, 1):
                await event.answer(f"You do not have {required_tm} anymore.", alert=True)
                return
            old_move = current_moves[move_slot]
            current_moves[move_slot] = move_name
            pokemon.moves_json = json.dumps(current_moves)
            if required_tm:
                record_move_history(pokemon, categories=["tm"], move_names=[move_name])
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
            if required_tm:
                notice = f"{pokemon.species} forgot {old_move} and learned {move_name} using {required_tm}."
            else:
                notice = f"{pokemon.species} forgot {old_move} and learned {move_name}."
        await self._show_training_move_menu(
            event,
            session=session,
            pokemon_page=pokemon_page,
            pokemon_id=pokemon_id,
            move_page=move_page,
            notice=notice,
            edit=True,
        )
        await event.answer("Move replaced.")

    async def _apply_training_nature(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        pokemon_page: int,
        pokemon_id: int,
        nature_page: int,
        nature_index: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            options = self._training_available_natures(inventories, trainer)
            items, _, _ = paginate_items(options, page=nature_page, per_page=TRAINING_NATURE_PAGE_SIZE)
            if nature_index < 0 or nature_index >= len(items):
                await event.answer("That nature is no longer on this page.", alert=True)
                return
            selected = items[nature_index]
            nature_name = str(selected["nature"])
            item_name = str(selected["item_name"])
            if normalize_lookup(pokemon.nature) == normalize_lookup(nature_name):
                await event.answer(f"{pokemon.species} is already {nature_name}.", alert=True)
                return
            if not inventories.consume_item(trainer, item_name):
                await event.answer(f"You do not have {item_name} anymore.", alert=True)
                return
            old_stats = self.pokemon_data.calculate_stats(pokemon)
            old_nature = pokemon.nature
            pokemon.nature = nature_name
            new_stats = self.pokemon_data.calculate_stats(pokemon)
            self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
            notice = f"{old_nature} -> {nature_name} using {item_name}"
        await self._show_training_nature_menu(
            event,
            session=session,
            pokemon_page=pokemon_page,
            pokemon_id=pokemon_id,
            nature_page=nature_page,
            notice=notice,
            edit=True,
        )
        await event.answer("Nature updated.")

    async def _apply_training_ability(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        pokemon_page: int,
        pokemon_id: int,
        ability_index: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            options = await self._training_available_abilities(inventories, trainer, pokemon)
            if ability_index < 0 or ability_index >= len(options):
                await event.answer("That ability option is no longer available.", alert=True)
                return
            selected = options[ability_index]
            ability_name = str(selected["name"])
            required_item = str(selected["required_item"])
            if not inventories.consume_item(trainer, required_item):
                await event.answer(f"You do not have {required_item} anymore.", alert=True)
                return
            old_ability = pokemon.ability
            pokemon.ability = ability_name
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
            notice = f"{old_ability} -> {ability_name} using {required_item}"
        await self._show_training_ability_menu(
            event,
            session=session,
            pokemon_page=pokemon_page,
            pokemon_id=pokemon_id,
            notice=notice,
            edit=True,
        )
        await event.answer("Ability updated.")

    async def _apply_training_iv(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        pokemon_page: int,
        pokemon_id: int,
        stat_key: str,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            bottle_cap_count = inventories.held_item_count(trainer, BOTTLE_CAP_ITEM)
            gold_bottle_cap_count = inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM)
            old_stats = self.pokemon_data.calculate_stats(pokemon)

            if stat_key == "all":
                if gold_bottle_cap_count <= 0:
                    await event.answer("You need a Gold Bottle Cap for that.", alert=True)
                    return
                if all(int(getattr(pokemon, f"iv_{key}")) >= 31 for key in EV_STAT_ORDER):
                    await event.answer("All IVs are already maxed.", alert=True)
                    return
                inventories.consume_item(trainer, GOLD_BOTTLE_CAP_ITEM)
                for key in EV_STAT_ORDER:
                    setattr(pokemon, f"iv_{key}", 31)
                item_used = GOLD_BOTTLE_CAP_ITEM
                notice = f"All IVs were maxed with {item_used}."
            else:
                if stat_key not in EV_STAT_LABELS:
                    await event.answer("Choose a valid IV.", alert=True)
                    return
                if int(getattr(pokemon, f"iv_{stat_key}")) >= 31:
                    await event.answer(f"{EV_STAT_LABELS[stat_key]} IV is already maxed.", alert=True)
                    return
                item_used = BOTTLE_CAP_ITEM if bottle_cap_count > 0 else GOLD_BOTTLE_CAP_ITEM
                if not inventories.consume_item(trainer, item_used):
                    await event.answer(f"You do not have {item_used} anymore.", alert=True)
                    return
                setattr(pokemon, f"iv_{stat_key}", 31)
                notice = f"{EV_STAT_LABELS[stat_key]} IV was maxed with {item_used}."

            new_stats = self.pokemon_data.calculate_stats(pokemon)
            self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
        await self._show_training_iv_menu(
            event,
            session=session,
            pokemon_page=pokemon_page,
            pokemon_id=pokemon_id,
            notice=notice,
            edit=True,
        )
        await event.answer("IV updated.")

    async def _apply_training_tera(
        self,
        event: CallbackQuery.Event,
        session: TrainingSession,
        *,
        pokemon_page: int,
        pokemon_id: int,
        tera_index: int,
    ) -> None:
        busy_reason = self._training_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        sender = await event.get_sender()
        with db_session() as db:
            trainers = TrainerRepository(db)
            pokemons = PokemonRepository(db)
            inventories = InventoryRepository(db)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not self._training_can_modify_pokemon(session, pokemon_id):
                await event.answer("This 1-hour package cannot train more Pokemon.", alert=True)
                return
            options = self._training_available_tera_types(inventories, trainer, pokemon)
            if tera_index < 0 or tera_index >= len(options):
                await event.answer("That Tera Type option is no longer available.", alert=True)
                return
            selected = options[tera_index]
            tera_type = str(selected["type_name"])
            shard_name = str(selected["shard_name"])
            if not inventories.consume_item(trainer, shard_name, TERA_TYPE_CHANGE_SHARD_COST):
                await event.answer(f"You need {TERA_TYPE_CHANGE_SHARD_COST} {shard_name}.", alert=True)
                return
            old_tera = str(getattr(pokemon, "tera_type", "") or "Unknown")
            pokemon.tera_type = tera_type
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            self._training_register_pokemon(session, pokemon_id)
            notice = f"{old_tera} -> {tera_type} using {shard_name} x{TERA_TYPE_CHANGE_SHARD_COST}"
        await self._show_training_tera_menu(
            event,
            session=session,
            pokemon_page=pokemon_page,
            pokemon_id=pokemon_id,
            notice=notice,
            edit=True,
        )
        await event.answer("Tera Type updated.")

    async def handle_train_callback(self, event: CallbackQuery.Event, data: str) -> None:
        if not event.is_private:
            await event.answer("Use training in private chat.", alert=True)
            return
        parts = data.split(":")
        if data == "train:noop":
            await event.answer()
            return
        if data in {"train:entry", "train:start"}:
            active_session = self._active_training_session(event.sender_id)
            if active_session is not None:
                await self._show_training_pokemon_menu(event, session=active_session, page=0, edit=True)
            elif data == "train:start":
                sender = await event.get_sender()
                with db_session() as db:
                    trainers = TrainerRepository(db)
                    trainer = trainers.ensure_trainer(
                        telegram_user_id=event.sender_id,
                        username=getattr(sender, "username", None),
                        display_name=display_name(sender),
                    )
                    vp = trainer.inventory.victory_points if trainer.inventory else 0
                await self._present_training_menu(
                    event,
                    self.training_duration_text(current_vp=vp),
                    buttons=self.training_duration_buttons(),
                    edit=True,
                )
            else:
                await self._present_training_menu(
                    event,
                    self.training_entry_text(),
                    buttons=self.training_entry_buttons(),
                    edit=True,
                )
            await event.answer()
            return
        if data == "train:leave":
            await safe_event_edit(event, "Hope we see you soon...", buttons=None)
            await event.answer()
            return
        if len(parts) == 3 and parts[1] == "buy":
            duration_key = parts[2]
            if duration_key not in TRAINING_DURATION_COSTS:
                await event.answer("Unknown training duration.", alert=True)
                return
            active_session = self._active_training_session(event.sender_id)
            if active_session is not None:
                await self._show_training_pokemon_menu(event, session=active_session, page=0, edit=True)
                await event.answer("Your training session is already active.")
                return
            busy_reason = self._training_lock_reason(event.sender_id)
            if busy_reason:
                await event.answer(busy_reason, alert=True)
                return
            sender = await event.get_sender()
            with db_session() as db:
                trainers = TrainerRepository(db)
                inventories = InventoryRepository(db)
                pokemons = PokemonRepository(db)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                if not pokemons.list_owned_pokemon(trainer):
                    await event.answer("You need at least one Pokemon before using the training spot.", alert=True)
                    return
                cost = TRAINING_DURATION_COSTS[duration_key]
                if not inventories.consume_victory_points(trainer, cost):
                    await event.answer(
                        f"Not enough VP. You need {cost:,} VP but only have {trainer.inventory.victory_points:,} VP.",
                        alert=True,
                    )
                    return
            session = self._create_training_session(event.sender_id, duration_key)
            await self._show_training_pokemon_menu(
                event,
                session=session,
                page=0,
                notice=f"{session.duration_label} started.",
                edit=True,
            )
            await event.answer("Training started.")
            return
        if len(parts) < 5 or parts[1] != "s":
            await event.answer("Unknown training action.", alert=True)
            return

        session = await self._require_training_session(event, parts[2])
        if session is None:
            return
        action = parts[3]

        if action == "pl" and len(parts) == 5:
            await self._show_training_pokemon_menu(event, session=session, page=int(parts[4]), edit=True)
            await event.answer()
            return
        if action in {"ps", "md"} and len(parts) == 6:
            await self._show_training_mode_menu(
                event,
                session=session,
                page=int(parts[4]),
                pokemon_id=int(parts[5]),
                edit=True,
            )
            await event.answer()
            return
        if action == "nm" and len(parts) == 6:
            await self._show_training_normal_menu(event, session=session, page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "hm" and len(parts) == 6:
            await self._show_training_hyper_menu(event, session=session, page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "lv" and len(parts) == 6:
            await self._show_training_level_menu(event, session=session, page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "la" and len(parts) == 7:
            await self._apply_training_level(event, session, page=int(parts[4]), pokemon_id=int(parts[5]), step=int(parts[6]))
            return
        if action == "ev" and len(parts) == 6:
            await self._show_training_ev_menu(event, session=session, page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "es" and len(parts) == 7:
            await self._show_training_ev_stat_menu(event, session=session, page=int(parts[4]), pokemon_id=int(parts[5]), stat_key=parts[6], edit=True)
            await event.answer()
            return
        if action == "ea" and len(parts) == 8:
            await self._apply_training_ev_change(event, session, page=int(parts[4]), pokemon_id=int(parts[5]), stat_key=parts[6], delta=int(parts[7]))
            return
        if action == "mv" and len(parts) == 7:
            await self._show_training_move_menu(event, session=session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), move_page=int(parts[6]), edit=True)
            await event.answer()
            return
        if action == "mp" and len(parts) == 8:
            await self._apply_training_move_pick(event, session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), move_page=int(parts[6]), move_index=int(parts[7]))
            return
        if action == "mr" and len(parts) == 9:
            await self._apply_training_move_replace(event, session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), move_page=int(parts[6]), move_index=int(parts[7]), move_slot=int(parts[8]))
            return
        if action == "nt" and len(parts) == 7:
            await self._show_training_nature_menu(event, session=session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), nature_page=int(parts[6]), edit=True)
            await event.answer()
            return
        if action == "na" and len(parts) == 8:
            await self._apply_training_nature(event, session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), nature_page=int(parts[6]), nature_index=int(parts[7]))
            return
        if action == "ab" and len(parts) == 6:
            await self._show_training_ability_menu(event, session=session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "aa" and len(parts) == 7:
            await self._apply_training_ability(event, session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), ability_index=int(parts[6]))
            return
        if action == "tt" and len(parts) == 6:
            await self._show_training_tera_menu(event, session=session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "ta" and len(parts) == 7:
            await self._apply_training_tera(event, session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), tera_index=int(parts[6]))
            return
        if action == "iv" and len(parts) == 6:
            await self._show_training_iv_menu(event, session=session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), edit=True)
            await event.answer()
            return
        if action == "ia" and len(parts) == 7:
            await self._apply_training_iv(event, session, pokemon_page=int(parts[4]), pokemon_id=int(parts[5]), stat_key=parts[6])
            return

        await event.answer("Unknown training action.", alert=True)

    def _pokemon_change_lock_reason(self, user_id: int) -> str | None:
        reason = self.battle_service.pvp_lock_reason(user_id)
        if reason:
            return reason
        reason = self.battle_service.encounter_lock_reason(user_id)
        if reason:
            return reason
        if self.encounters.active_by_user.get(user_id) is not None:
            return "Finish your current encounter before changing held items."
        return None

    async def _held_item_catalog_entries(self) -> list[str]:
        if self._held_item_catalog is None:
            self._held_item_catalog = await self.generator.list_held_items()
        return list(self._held_item_catalog)

    def held_item_page(
        self,
        inventories: InventoryRepository,
        trainer,
        *,
        category_key: str,
        page: int,
    ) -> tuple[list[tuple[str, int]], int, int]:
        entries = sorted(
            [
                (name, amount)
                for name, amount in inventories.held_item_counts(trainer).items()
                if equip_item_category(name) == category_key
            ],
            key=lambda item: item[0].lower(),
        )
        total = len(entries)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // EQUIP_ITEM_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * EQUIP_ITEM_PAGE_SIZE
        end = start + EQUIP_ITEM_PAGE_SIZE
        return entries[start:end], total, current_page

    def equip_item_categories(
        self,
        inventories: InventoryRepository,
        trainer,
    ) -> list[tuple[str, str, int]]:
        grouped: dict[str, int] = {key: 0 for key, _ in EQUIP_CATEGORY_ORDER}
        for item_name, amount in inventories.held_item_counts(trainer).items():
            if int(amount) <= 0:
                continue
            grouped[equip_item_category(item_name)] += 1
        return [
            (key, label, grouped[key])
            for key, label in EQUIP_CATEGORY_ORDER
            if grouped.get(key, 0) > 0
        ]

    def equip_category_list_text(self, categories: list[tuple[str, str, int]]) -> str:
        lines = [
            "🎒 **Equip Held Items**",
            "━━━━━━━━━━━━━━━━━━━━━",
            "Select a category to view your items:",
            ""
        ]
        if not categories:
            lines.append("_You do not have any held items right now._")
            return "\n".join(lines)
            
        for _, label, count in categories:
            lines.append(f"🔹 **{label}**: `{count} items`")
        return "\n".join(lines)

    def equip_category_list_buttons(
        self,
        categories: list[tuple[str, str, int]],
    ) -> list[list[Button]] | None:
        buttons = [
            Button.inline(label, data=f"equip:list:{key}:0".encode("utf-8"))
            for key, label, _ in categories
        ]
        return chunk_buttons(buttons, per_row=2) if buttons else None

    def equip_item_list_text(
        self,
        *,
        category_key: str,
        page: int,
        total: int,
        items: list[tuple[str, int]],
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // EQUIP_ITEM_PAGE_SIZE) + 1)
        category_label = EQUIP_CATEGORY_LABELS.get(category_key, category_key.title())
        
        lines = [
            f"🎒 **Equip: {category_label}**",
            "━━━━━━━━━━━━━━━━━━━━━"
        ]
        if not items:
            lines.append(f"_You do not have any {category_label} right now._")
        else:
            for index, (item_name, count) in enumerate(items, start=1):
                lines.append(f"`[{index:<2}]` **{item_name}** `(x{count})`")
                
        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━", 
            f"__Page {page + 1}/{max_page}__"
        ])
        return "\n".join(lines)

    def equip_item_list_buttons(
        self,
        *,
        category_key: str,
        page: int,
        total: int,
        items: list[tuple[str, int]],
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        number_buttons = [
            Button.inline(
                str(index),
                data=f"equip:item:{category_key}:{page}:{normalize_lookup(item_name)}".encode("utf-8"),
            )
            for index, (item_name, _) in enumerate(items, start=1)
        ]
        if number_buttons:
            rows.extend(chunk_buttons(number_buttons, per_row=5))

        max_page = max(0, (max(total, 1) - 1) // EQUIP_ITEM_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"equip:list:{category_key}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="equip:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"equip:list:{category_key}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Categories", data="equip:list".encode("utf-8"))])
        return rows

    def equip_pokemon_page(
        self,
        trainer,
        pokemons: PokemonRepository,
        *,
        page: int,
    ) -> tuple[list, int, int]:
        pokemon_list = self.sorted_owned_pokemon(trainer, pokemons)
        total = len(pokemon_list)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // EQUIP_ITEM_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * EQUIP_ITEM_PAGE_SIZE
        end = start + EQUIP_ITEM_PAGE_SIZE
        return pokemon_list[start:end], total, current_page

    def equip_pokemon_picker_text(
        self,
        trainer,
        *,
        item_name: str,
        page: int,
        total: int,
        items: list,
    ) -> str:
        from bot.game.fusion import effective_species # Ensure we get the correct form name
        max_page = max(1, ((max(total, 1) - 1) // EQUIP_ITEM_PAGE_SIZE) + 1)
        
        lines = [
            f"🎯 **Who should hold {item_name}?**",
            "━━━━━━━━━━━━━━━━━━━━━"
        ]
        if not items:
            lines.append("_You do not own any Pokemon yet._")
        else:
            start = page * EQUIP_ITEM_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                shiny_icon = " ✨" if pokemon.shiny else ""
                sp = effective_species(pokemon)
                current_item = pokemon.item if pokemon.item else "None"
                
                lines.append(f"`[{index:<2}]` **{sp}**{shiny_icon} `(Holding: {current_item})`")
                
        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━", 
            f"__Page {page + 1}/{max_page}__"
        ])
        return "\n".join(lines)

    def equip_pokemon_picker_buttons(
        self,
        *,
        category_key: str,
        item_key: str,
        item_page: int,
        page: int,
        total: int,
        items: list,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * EQUIP_ITEM_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(
                str(index),
                data=f"equip:pick:{category_key}:{item_page}:{item_key}:{page}:{pokemon.id}".encode("utf-8"),
            )
            for index, pokemon in enumerate(items, start=start)
        ]
        rows.extend(chunk_buttons(number_buttons, per_row=5))

        max_page = max(0, (max(total, 1) - 1) // EQUIP_ITEM_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"equip:pokemon:{category_key}:{item_page}:{item_key}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data="equip:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"equip:pokemon:{category_key}:{item_page}:{item_key}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Back", data=f"equip:list:{category_key}:{item_page}".encode("utf-8"))])
        return rows

    def equip_confirm_text(self, item_name: str, pokemon) -> str:
        from bot.game.fusion import effective_species
        sp = effective_species(pokemon)
        current_item = pokemon.item if pokemon.item else "Nothing"
        
        lines = [
            f"🔄 **Confirm Equip**",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"Target: **{sp}**",
            f"Current Item: `{current_item}`",
            f"New Item: `{item_name}`",
            "",
            f"__Are you sure you want to equip {item_name}?__"
        ]
        if pokemon.item:
            lines.append(f"_{current_item} will be returned to your bag._")
            
        return "\n".join(lines)

    def equip_confirm_buttons(
        self,
        *,
        category_key: str,
        item_key: str,
        item_page: int,
        pokemon_page: int,
        pokemon_id: int,
    ) -> list[list[Button]]:
        return [[
            Button.inline("Confirm", data=f"equip:confirm:{category_key}:{item_page}:{item_key}:{pokemon_page}:{pokemon_id}".encode("utf-8")),
            Button.inline("Cancel", data=f"equip:pokemon:{category_key}:{item_page}:{item_key}:{pokemon_page}".encode("utf-8")),
        ]]

    async def _show_equip_item_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        category_key: str | None = None,
        page: int,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            if category_key is None:
                categories = self.equip_item_categories(inventories, trainer)
                response_text = self.equip_category_list_text(categories)
                response_buttons = self.equip_category_list_buttons(categories)
            else:
                items, total, current_page = self.held_item_page(
                    inventories,
                    trainer,
                    category_key=category_key,
                    page=page,
                )
                response_text = self.equip_item_list_text(
                    category_key=category_key,
                    page=current_page,
                    total=total,
                    items=items,
                )
                response_buttons = self.equip_item_list_buttons(
                    category_key=category_key,
                    page=current_page,
                    total=total,
                    items=items,
                )

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons,parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons,parse_mode="md")

    async def _show_equip_pokemon_menu(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        *,
        category_key: str,
        item_key: str,
        item_page: int,
        page: int,
        edit: bool = False,
    ) -> None:
        sender = await event.get_sender()
        response_text: str | None = None
        response_buttons = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = inventories.held_item_name(trainer, item_key)
            if item_name is None:
                response_text = "That item is no longer available."
            else:
                items, total, current_page = self.equip_pokemon_page(trainer, pokemons, page=page)
                response_text = self.equip_pokemon_picker_text(
                    trainer,
                    item_name=item_name,
                    page=current_page,
                    total=total,
                    items=items,
                )
                if items:
                    response_buttons = self.equip_pokemon_picker_buttons(
                        category_key=category_key,
                        item_key=item_key,
                        item_page=item_page,
                        page=current_page,
                        total=total,
                        items=items,
                    )

        if response_text is None:
            return
        if edit and isinstance(event, CallbackQuery.Event):
            edited = await safe_event_edit(event, response_text, buttons=response_buttons,parse_mode="md")
            if edited:
                return
        await event.respond(response_text, buttons=response_buttons,parse_mode="md")

    async def _show_equip_confirm_menu(
        self,
        event: CallbackQuery.Event,
        *,
        category_key: str,
        item_key: str,
        item_page: int,
        pokemon_page: int,
        pokemon_id: int,
    ) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = inventories.held_item_name(trainer, item_key)
            if item_name is None:
                await event.answer("That item is no longer available.", alert=True)
                return
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            await safe_event_edit(
                event,
                self.equip_confirm_text(item_name, pokemon),
                buttons=self.equip_confirm_buttons(
                    category_key=category_key,
                    item_key=item_key,
                    item_page=item_page,
                    pokemon_page=pokemon_page,
                    pokemon_id=pokemon_id,
                ),
                parse_mode="md"
            )

    async def on_equip_items(self, event: NewMessage.Event) -> None:
        if not await self._ensure_command_unlocked(event, "equip_item"):
            return
        if not event.is_private:
            await self._reply_dm_command_button(event, "equip_item")
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.respond(busy_reason)
            return
        await self._show_equip_item_menu(event, category_key=None, page=0)

    async def on_addallitem(self, event: NewMessage.Event) -> None:
        await respond_locked(event, ADMIN_COMMAND_LOCK_MESSAGE)
        return
        amount = 1
        parts = event.raw_text.split()
        if len(parts) >= 2:
            try:
                amount = max(1, int(parts[1]))
            except (TypeError, ValueError):
                await event.respond("Use /addallitem or /addallitem <amount>.")
                return

        item_names = await self._held_item_catalog_entries()
        if not item_names:
            await event.respond("Could not load the held item catalog.")
            return

        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            for item_name in item_names:
                inventories.add_item(trainer, item_name, amount)

        await event.respond(f"Added {amount}x of {len(item_names)} held items to your bag.", parse_mode="md")

    async def _confirm_equip_item(
        self,
        event: CallbackQuery.Event,
        *,
        item_key: str,
        pokemon_id: int,
    ) -> None:
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return

        sender = await event.get_sender()
        response_text = "Could not equip that item."
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            item_name = inventories.held_item_name(trainer, item_key)
            if item_name is None:
                await event.answer("That item is no longer available.", alert=True)
                return
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if normalize_lookup(pokemon.item or "") == normalize_lookup(item_name):
                await event.answer(f"{pokemon.species} is already holding {item_name}.", alert=True)
                return
            if not inventories.consume_item(trainer, item_name):
                await event.answer("You do not have that item anymore.", alert=True)
                return
            previous_item = str(pokemon.item or "").strip()
            if previous_item:
                inventories.add_item(trainer, previous_item)
            pokemon.item = item_name
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            response_text = f"{pokemon.species} is now holding {item_name}."
            if previous_item:
                response_text += f"\nReturned {previous_item} to your bag."

        edited = await safe_event_edit(event, response_text, buttons=None)
        if not edited:
            await event.respond(response_text)
        await event.answer("Held item equipped.")

    async def _apply_stat_medicine(
        self,
        event: CallbackQuery.Event,
        pokemon_id: int,
        medicine_key: str,
        *,
        amount: int,
        stat_key: str | None = None,
    ) -> None:
        sender = await event.get_sender()
        medicine_label = medicine_name(medicine_key)
        definition = MEDICINE_DEFINITIONS[medicine_key]
        kind = str(definition.get("kind") or "")
        action = "mochi" if "mochi" in kind else "feather"
        target_stat = self._item_target_stat(medicine_key, stat_key)
        ev_step = int(definition.get("ev_amount") or 0)

        if target_stat is None or ev_step <= 0:
            await event.answer("Choose a valid stat first.", alert=True)
            return

        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            available_count = inventories.medicine_count(trainer, medicine_label)
            if available_count <= 0:
                await event.answer(f"You do not have any {medicine_label}.", alert=True)
                return
            requested_amount = max(1, int(amount))
            use_limit = min(requested_amount, available_count)

            old_ev = int(getattr(pokemon, f"ev_{target_stat}"))
            old_stats = self.pokemon_data.calculate_stats(pokemon)
            
            if kind in {"mochi", "feather"}:
                total_before = pokemon_total_ev(pokemon)
                max_use_stat = max(0, (MAX_EV_PER_STAT - old_ev) // ev_step)
                max_use_total = max(0, (MAX_TOTAL_EVS - total_before) // ev_step)
                actual_use = min(use_limit, max_use_stat, max_use_total)
                if actual_use <= 0:
                    await event.answer("That Pokemon cannot gain more EVs from this item.", alert=True)
                    return
                new_ev = old_ev + actual_use * ev_step
            elif kind in {"mochi-lower", "feather-lower"}:
                actual_use = min(use_limit, old_ev // ev_step)
                if actual_use <= 0:
                    await event.answer(f"{EV_STAT_LABELS[target_stat]} does not have enough EVs for {medicine_label}.", alert=True)
                    return
                new_ev = old_ev - actual_use * ev_step
            else:
                await event.answer("This item is not configured for EV use.", alert=True)
                return

            setattr(pokemon, f"ev_{target_stat}", new_ev)
            new_stats = self.pokemon_data.calculate_stats(pokemon)
            self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
            pokemons.sync_packed_set(pokemon, self.pokemon_data)
            inventories.consume_medicine(trainer, medicine_label, actual_use)
            
            success_text = (
                f"✅ **Used {actual_use}x {medicine_label} on {pokemon.species}.**\n"
                f"🔹 {EV_STAT_LABELS[target_stat]} EV: `{old_ev} ➔ {new_ev}`"
            )

            # Re-fetch inventory to check if we can keep the menu open
            available_count = inventories.medicine_count(trainer, medicine_label)
            if available_count > 0:
                menu_text = self.item_use_amount_text(
                    pokemon, medicine_key, available_count=available_count, stat_key=stat_key
                )
                response_text = f"{success_text}\n━━━━━━━━━━━━━━━━━━━━━━\n{menu_text}"
                response_buttons = self.item_use_amount_buttons(
                    action, pokemon.id, medicine_key, stat_key=stat_key
                )
            else:
                response_text = f"{success_text}\n━━━━━━━━━━━━━━━━━━━━━━\n_You ran out of {medicine_label}._"
                response_buttons = [[Button.inline("⬅️ Back", data=f"itemuse:pickpokemon:{action}:{pokemon.id}".encode("utf-8"))]]

        await safe_event_edit(event, response_text, buttons=response_buttons, parse_mode="md")
        await event.answer(f"Used {medicine_label}.")

    async def _apply_mochi(
        self,
        event: CallbackQuery.Event,
        pokemon_id: int,
        medicine_key: str,
        *,
        amount: int,
        stat_key: str | None = None,
    ) -> None:
        await self._apply_stat_medicine(
            event,
            pokemon_id,
            medicine_key,
            amount=amount,
            stat_key=stat_key,
        )

    async def _apply_feather(
        self,
        event: CallbackQuery.Event,
        pokemon_id: int,
        medicine_key: str,
        *,
        amount: int,
        stat_key: str | None = None,
    ) -> None:
        await self._apply_stat_medicine(
            event,
            pokemon_id,
            medicine_key,
            amount=amount,
            stat_key=stat_key,
        )

    async def _apply_candy(
        self,
        event: CallbackQuery.Event,
        pokemon_id: int,
        medicine_key: str,
        *,
        amount: int,
    ) -> None:
        sender = await event.get_sender()
        medicine_label = medicine_name(medicine_key)
        definition = MEDICINE_DEFINITIONS[medicine_key]

        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            pokemon = pokemons.get_owned_pokemon(trainer, int(pokemon_id))
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            available_count = inventories.medicine_count(trainer, medicine_label)
            if available_count <= 0:
                await event.answer(f"You do not have any {medicine_label}.", alert=True)
                return
            requested_amount = max(1, int(amount))
            use_limit = min(requested_amount, available_count)

            if medicine_key == "rare-candy":
                if pokemon.level >= 100:
                    await event.answer(f"{pokemon.species} is already level 100.", alert=True)
                    return
                growth = self.pokemon_data.growth_rate(pokemon.species)
                current_floor = self.pokemon_data.level_curve_value(growth, pokemon.level)
                current_total = max(int(pokemon.experience), int(current_floor))
                target_level = min(100, pokemon.level + use_limit)
                actual_use = max(0, target_level - pokemon.level)
                if actual_use <= 0:
                    await event.answer(f"{pokemon.species} is already level 100.", alert=True)
                    return
                target_total = self.pokemon_data.level_curve_value(growth, target_level)
                gain_amount = max(0, int(target_total) - current_total)
            else:
                exp_per_item = int(definition["exp"] or 0)
                if exp_per_item <= 0:
                    await event.answer("This candy is not configured.", alert=True)
                    return
                if pokemon.level >= 100:
                    await event.answer(f"{pokemon.species} is already level 100.", alert=True)
                    return
                growth = self.pokemon_data.growth_rate(pokemon.species)
                current_floor = self.pokemon_data.level_curve_value(growth, pokemon.level)
                current_total = max(int(pokemon.experience), int(current_floor))
                cap_total = self.pokemon_data.level_curve_value(growth, 100)
                exp_to_cap = max(0, int(cap_total) - current_total)
                actual_use = min(use_limit, (exp_to_cap + exp_per_item - 1) // exp_per_item if exp_to_cap > 0 else 0)
                if actual_use <= 0:
                    await event.answer(f"{pokemon.species} cannot gain more EXP.", alert=True)
                    return
                gain_amount = actual_use * exp_per_item

            progression_text, pending_prompts = await self.encounters._apply_experience_gain(
                session,
                trainer,
                pokemon,
                gain_amount,
            )
            inventories.consume_medicine(trainer, medicine_label, actual_use)

            success_lines = [f"✅ **Used {actual_use}x {medicine_label} on {pokemon.species}.**"]
            if medicine_key != "rare-candy":
                success_lines.append(f"🔹 `+{gain_amount:,} EXP`")
            if progression_text:
                success_lines.extend(["", progression_text])

            success_text = "\n".join(success_lines)
            available_count = inventories.medicine_count(trainer, medicine_label)

            if available_count > 0:
                menu_text = self.item_use_amount_text(pokemon, medicine_key, available_count=available_count)
                response_text = f"{success_text}\n━━━━━━━━━━━━━━━━━━━━━━\n{menu_text}"
                response_buttons = self.item_use_amount_buttons("candy", pokemon.id, medicine_key)
            else:
                response_text = f"{success_text}\n━━━━━━━━━━━━━━━━━━━━━━\n_You ran out of {medicine_label}._"
                response_buttons = [[Button.inline("⬅️ Back", data=f"itemuse:pickpokemon:candy:{pokemon.id}".encode("utf-8"))]]

        if pending_prompts:
            await self.encounters._send_progression_followups(
                event.sender_id,
                level_up_messages=[],
                pending_prompts=pending_prompts,
            )
            
        await safe_event_edit(event, response_text, buttons=response_buttons, parse_mode="md")
        await event.answer("Candy used.")

    async def on_addballs(self, event: NewMessage.Event) -> None:
        await respond_locked(event, ADMIN_COMMAND_LOCK_MESSAGE)
        return
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.respond("Use /addballs <amount> or /addballs <amount> <ball|all>.")
            return

        amount: int | None = None
        target = "all"
        for token in parts[1:]:
            if token.lstrip("+-").isdigit():
                amount = int(token)
            else:
                target = token
        if amount is None or amount <= 0:
            await event.respond("Ball amount must be a positive number.")
            return

        ball_kinds = list(BALL_ORDER)
        if target.lower() != "all":
            ball_kind = normalize_ball_kind(target)
            if ball_kind is None:
                await event.respond("Unknown ball. Example: /addballs 50 ultra or /addballs 25 all.")
                return
            ball_kinds = [ball_kind]

        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            for ball_kind in ball_kinds:
                inventories.add_ball(trainer, ball_kind, amount)

        if len(ball_kinds) == 1:
            await event.respond(f"Added {amount} {ball_label(ball_kinds[0])}(s) to your bag.")
            return
        await event.respond(f"Added {amount} of every supported ball to your bag.")

    async def on_shop(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            shop_text, page, max_page = await self.shop_text(trainer, inventories, category="balls", page=0)
        await event.respond(shop_text, buttons=self.shop_buttons("balls", page=page, max_page=max_page, trainer=trainer), parse_mode="md")
        return

        shop_text = (
            "🏪 **PokéMart**\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔵 **Pokéballs:**\n"
            "• Regular Ball — 5 VP\n"
            "• Great Ball — 7 VP\n"
            "• Ultra Ball — 12 VP\n"
            "• Repeat Ball — 15 VP\n"
            "• Nest Ball — 20 VP\n\n"
            f"💰 **Your Victory Points:** {vp:,} VP\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "**Commands:**\n"
            "`Buy  →` /buy [item] [quantity]\n"
            "`Sell →` /sell [item] [quantity]\n\n"
            "**Examples:**\n"
            "• /buy Ultra 2\n"
            "• /sell greatball 3\n\n"
            "__Selling returns 30% of item value (minimum 1 VP).__"
        )
        await event.respond(shop_text, parse_mode="md")

    async def o_buy(self, event: NewMessage.Event) -> None:
        parsed = self.parse_shop_item(event.raw_text)
        if parsed is None:
            await event.respond("Usage: /buy [item] [quantity]\nExample: /buy Rare Candy 2")
            return

        item_arg, qty = parsed
        ball_kind = normalize_ball_kind(item_arg)
        medicine_key = normalize_medicine_key(item_arg)
        ball_prices = {"poke": 5, "great": 7, "ultra": 12, "repeat": 15, "nest": 20}

        item_label: str | None = None
        cost_each: int | None = None
        currency = "vp"
        add_action: tuple[str, str, int] | None = None
        battle_offer: dict[str, Any] | None = None
        unique_key_item = False
        if ball_kind in ball_prices:
            item_label = ball_label(ball_kind)
            cost_each = ball_prices[ball_kind]
            add_action = ("ball", ball_kind, 1)
        elif medicine_key is not None and medicine_shop_price(medicine_key) is not None:
            item_label = medicine_name(medicine_key)
            cost_each = medicine_shop_price(medicine_key)
            add_action = ("medicine", medicine_name(medicine_key), 1)
        else:
            key_offer = self._key_item_shop_offer(item_arg)
            if key_offer is not None:
                item_label = str(key_offer["display_name"])
                cost_each = int(key_offer["price"])
                currency = str(key_offer["currency"])
                add_action = ("key", str(key_offer["name"]), int(key_offer.get("amount") or 1))
                unique_key_item = bool(key_offer.get("unique"))
            else:
                battle_offer = self._battle_shop_offer(event.sender_id, item_arg)
                if battle_offer is not None:
                    if qty != 1:
                        await event.respond("Battle shop items can only be bought once per week, one at a time.")
                        return
                    item_label = str(battle_offer["display_name"])
                    cost_each = int(battle_offer["price"])
                    currency = str(battle_offer["currency"])
                    kind = str(battle_offer["kind"])
                    if kind == "tm":
                        add_action = ("tm", str(battle_offer["name"]), int(battle_offer.get("amount") or 1))
                    else:
                        add_action = ("item", str(battle_offer["name"]), int(battle_offer.get("amount") or 1))
                else:
                    held_offer = await self._held_shop_offer(item_arg)
                    if held_offer is None:
                        await event.respond("That item is not available in the shop.")
                        return
                    item_label = str(held_offer["display_name"])
                    cost_each = int(held_offer["price"])
                    currency = str(held_offer["currency"])
                    add_action = ("item", str(held_offer["name"]), int(held_offer.get("amount") or 1))

        sender = await event.get_sender()
        cost = int(cost_each or 0) * qty
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            if unique_key_item and qty != 1:
                await event.respond(f"{item_label} can only be bought one at a time.")
                return
            if unique_key_item and inventories.key_item_count(trainer, str(add_action[1])) > 0:
                await event.respond(f"You already own {item_label}.")
                return
            if battle_offer is not None and self._battle_shop_is_purchased(trainer, battle_offer):
                await event.respond(f"You already bought {item_label} this week.")
                return
            if currency == "lp":
                if not inventories.consume_league_points(trainer, cost):
                    await event.respond(
                        f"Not enough LP. You need {cost:,} LP but only have {getattr(trainer.inventory, 'league_points', 0):,} LP."
                    )
                    return
            else:
                if not inventories.consume_victory_points(trainer, cost):
                    await event.respond(
                        f"Not enough VP. You need {cost:,} VP but only have {trainer.inventory.victory_points:,} VP."
                    )
                    return
            purchased_qty = self._apply_shop_add_action(inventories, trainer, add_action, qty)
            if battle_offer is not None:
                self._mark_battle_shop_purchased(trainer, battle_offer)
            remaining_vp = trainer.inventory.victory_points
            remaining_lp = int(getattr(trainer.inventory, "league_points", 0) or 0)

        paid_label = "LP" if currency == "lp" else "VP"
        await event.respond(
            f"Purchased {purchased_qty}x {item_label} for {cost:,} {paid_label}.\n"
            f"Remaining VP: {remaining_vp:,}\n"
            f"Remaining LP: {remaining_lp:,}"
        )
        return

        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.respond("Usage: /buy [item] [quantity]\nExample: /buy Ultra 2")
            return

        item_arg = parts[1]
        qty = 1
        if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) > 0:
            qty = int(parts[2])
        elif len(parts) >= 3:
            await event.respond("Quantity must be a positive number.")
            return

        from bot.game.balls import normalize_ball_kind as _normalize_ball_kind, ball_label as _ball_label
        ball_kind = normalize_ball_kind(item_arg)
        shop_prices = {"poke": 5, "great": 7, "ultra": 12, "repeat": 15, "nest": 20}
        if ball_kind not in shop_prices:
            await event.respond(
                "That item is not available in the shop.\n"
                "Available: Regular Ball, Great Ball, Ultra Ball, Repeat Ball, Nest Ball"
            )
            return

        cost = shop_prices[ball_kind] * qty
        sender = await event.get_sender()
        success = False
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            if inventories.consume_victory_points(trainer, cost):
                inventories.add_ball(trainer, ball_kind, qty)
                success = True
                remaining = trainer.inventory.victory_points
            else:
                remaining = trainer.inventory.victory_points

        if success:
            await event.respond(
                f"✅ Purchased **{qty}x {ball_label(ball_kind)}** for **{cost:,} VP**!\n"
                f"💰 Remaining VP: {remaining:,}",
                parse_mode="md",
            )
        else:
            needed = shop_prices[ball_kind] * qty
            await event.respond(
                f"❌ Not enough VP! You need **{needed:,} VP** but only have **{remaining:,} VP**.",
                parse_mode="md",
            )

    async def on_sell(self, event: NewMessage.Event) -> None:
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.respond("Usage: /sell [item] [quantity]\nExample: /sell greatball 3")
            return

        item_arg = parts[1]
        qty = 1
        if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) > 0:
            qty = int(parts[2])
        elif len(parts) >= 3:
            await event.respond("Quantity must be a positive number.")
            return

        from bot.game.balls import normalize_ball_kind, ball_label
        ball_kind = normalize_ball_kind(item_arg)
        shop_prices = {"poke": 5, "great": 7, "ultra": 12, "repeat": 15, "nest": 20}
        if ball_kind not in shop_prices:
            await event.respond(
                "That item cannot be sold here.\n"
                "Sellable items: Regular Ball, Great Ball, Ultra Ball, Repeat Ball, Nest Ball"
            )
            return

        sell_price_each = max(1, int(shop_prices[ball_kind] * 0.3))
        total_payout = sell_price_each * qty

        sender = await event.get_sender()
        success = False
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            have = inventories.ball_count(trainer, ball_kind)
            if have < qty:
                await event.respond(
                    f"❌ You only have **{have}x {ball_label(ball_kind)}** in your bag.",
                    parse_mode="md",
                )
                return
            for _ in range(qty):
                inventories.consume_ball(trainer, ball_kind)
            inventories.add_victory_points(trainer, total_payout)
            success = True
            remaining = trainer.inventory.victory_points

        if success:
            await event.respond(
                f"💸 Sold **{qty}x {ball_label(ball_kind)}** for **{total_payout:,} VP**!\n"
                f"💰 New VP balance: {remaining:,}",
                parse_mode="md",
            )

    async def on_addvp(self, event: NewMessage.Event) -> None:
        await respond_locked(event, ADMIN_COMMAND_LOCK_MESSAGE)
        return
        """Admin command: grant 10,000 VP to every registered trainer."""
        from sqlalchemy import select as sa_select
        from bot.db.models import Trainer
        count = 0
        with db_session() as session:
            trainers_list = list(session.scalars(sa_select(Trainer)))
            for trainer in trainers_list:
                if trainer.inventory is None:
                    from bot.db.models import Inventory
                    trainer.inventory = Inventory()
                trainer.inventory.victory_points += 10000
                count += 1
        await event.respond(f"✅ Granted 10,000 VP to {count} trainer(s).")

    async def on_addsp(self, event: NewMessage.Event) -> None:
        await respond_locked(event, ADMIN_COMMAND_LOCK_MESSAGE)
        return
        """Admin command: grant 50 SP to every registered trainer."""
        from sqlalchemy import select as sa_select
        from bot.db.models import Trainer
        count = 0
        with db_session() as session:
            trainers_list = list(session.scalars(sa_select(Trainer)))
            for trainer in trainers_list:
                if trainer.inventory is None:
                    from bot.db.models import Inventory
                    trainer.inventory = Inventory()
                trainer.inventory.season_points += 50
                count += 1
        await event.respond(f"✅ Granted 50 SP to {count} trainer(s).")

    async def on_addlp(self, event: NewMessage.Event) -> None:
        await respond_locked(event, ADMIN_COMMAND_LOCK_MESSAGE)
        return
        """Admin command: grant 10,000 LP to every registered trainer."""
        from sqlalchemy import select as sa_select
        from bot.db.models import Trainer
        count = 0
        with db_session() as session:
            trainers_list = list(session.scalars(sa_select(Trainer)))
            for trainer in trainers_list:
                if trainer.inventory is None:
                    from bot.db.models import Inventory
                    trainer.inventory = Inventory()
                trainer.inventory.league_points += 10000
                count += 1
        await event.respond(f"Granted 10,000 LP to {count} trainer(s).")

    async def on_record(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return
        from sqlalchemy import func, select as sa_select
        from bot.db.models import Trainer

        with db_session() as session:
            total_users = int(session.scalar(sa_select(func.count(Trainer.id))) or 0)
        await event.respond(f"Total registered users: `{total_users}`", parse_mode="md")

    async def on_top(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return
        if not await self._ensure_command_unlocked(event, "top"):
            return
        if not event.is_private:
            await self._reply_dm_command_button(event, "top")
            return
        from sqlalchemy import desc, select as sa_select
        from bot.db.models import Inventory, Trainer

        with db_session() as session:
            rows = session.execute(
                sa_select(Trainer.display_name, Inventory.victory_points)
                .join(Inventory, Inventory.trainer_id == Trainer.id)
                .order_by(desc(Inventory.victory_points), Trainer.id.asc())
                .limit(5)
            ).all()
        if not rows:
            await event.respond("No trainers found.")
            return
        lines = ["**Top 5 VP Users**"]
        for index, (name, vp_value) in enumerate(rows, start=1):
            lines.append(f"{index}. {str(name or 'Trainer')} - `{int(vp_value or 0):,} VP`")
        await event.respond("\n".join(lines), parse_mode="md")

    async def on_status(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return
        from sqlalchemy import func, select as sa_select
        from bot.db.models import OwnedPokemon, Trainer

        uptime_seconds = max(0, int((datetime.utcnow() - self._service_started_at).total_seconds()))
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_text = f"{days}d {hours}h {minutes}m {seconds}s"

        with db_session() as session:
            admin_repo = AdminRepository(session)
            total_users = int(session.scalar(sa_select(func.count(Trainer.id))) or 0)
            total_pokemon = int(session.scalar(sa_select(func.count(OwnedPokemon.id))) or 0)
            total_banned = int(admin_repo.count_banned_users())
            total_groups = int(admin_repo.count_group_chats())

        lines = [
            "**Bot Status**",
            f"Uptime: `{uptime_text}`",
            f"Registered users: `{total_users}`",
            f"Owned Pokemon: `{total_pokemon}`",
            f"Banned users: `{total_banned}`",
            f"Known groups: `{total_groups}`",
            f"Active encounters: `{len(self.encounters.active_by_user)}`",
            f"Active training sessions: `{len(self.training_sessions)}`",
            f"Active trades: `{len(self.trade_sessions)}`",
            f"Active battles: `{len(self.battle_service.battles_by_id)}`",
        ]
        await event.respond("\n".join(lines), parse_mode="md")

    def _normalize_admin_item_category(self, value: str) -> str | None:
        aliases = {
            "vp": "vp",
            "victorypoint": "vp",
            "victorypoints": "vp",
            "lp": "lp",
            "leaguepoint": "lp",
            "leaguepoints": "lp",
            "ht": "ht",
            "holowearticket": "ht",
            "holoweartickets": "ht",
            "battlebox": "battlebox",
            "battleboxes": "battlebox",
            "trainerbox": "battlebox",
            "trainerboxes": "battlebox",
            "daycarecandy": "daycarecandy",
            "maxsoup": "maxsoup",
            "dynamaxcandy": "dynamaxcandy",
            "omniring": "omniring",
            "zcrystal": "zcrystal",
            "zcrystals": "zcrystal",
            "tm": "tm",
            "tms": "tm",
            "stone": "stone",
            "stones": "stone",
            "mint": "mint",
            "mints": "mint",
            "ball": "ball",
            "balls": "ball",
            "held": "held",
            "item": "held",
            "items": "held",
            "abilitycapsule": "abilitycapsule",
            "abilitypatch": "abilitypatch",
            "bottlecap": "bottlecap",
            "goldbottlecap": "goldbottlecap",
        }
        return aliases.get(normalize_lookup(value))

    def _admin_item_usage_text(self, command_name: str) -> str:
        return (
            f"Reply to a trainer with `/{command_name} <category> <amount>` or "
            f"`/{command_name} <category> <name> <amount>`.\n"
            "Examples: `/additem vp 1000`, `/additem tm 44 2`, `/additem held leftovers 1`"
        )

    def _match_named_entry(self, names: Sequence[str], query: str) -> str | None:
        target = normalize_lookup(query)
        return next((name for name in names if normalize_lookup(name) == target), None)

    def _resolve_admin_tm_label(self, query: str) -> str | None:
        text = str(query or "").strip()
        if not text:
            return None
        tm_number = self._extract_tm_number(text)
        if tm_number is not None:
            return self._tm_drop_map.get(int(tm_number))
        target = normalize_lookup(text)
        for label in self._tm_drop_map.values():
            prefix, _, move_name = str(label).partition(" - ")
            candidates = {
                normalize_lookup(label),
                normalize_lookup(prefix),
                normalize_lookup(move_name),
            }
            if target in candidates:
                return label
        return None

    def _stone_item_names(self, held_catalog: Sequence[str]) -> list[str]:
        names: list[str] = []
        for name in held_catalog:
            lowered = str(name).strip().lower()
            normalized = normalize_lookup(name)
            if normalized == "jadeorb":
                names.append(str(name))
                continue
            if lowered.endswith("ite") or lowered.endswith("ite x") or lowered.endswith("ite y") or lowered.endswith("ite z"):
                names.append(str(name))
        return names

    def _mint_item_names(self) -> list[str]:
        return [item_name for _nature_name, item_name in TRAINING_NATURE_MINTS]

    def _format_admin_summary(self, summary: Counter[str]) -> list[str]:
        return [f"• {name}: `{count}`" for name, count in sorted(summary.items())]

    def _weighted_random_choice(self, entries: Sequence[tuple[str, int]]) -> str | None:
        filtered = [(name, int(count)) for name, count in entries if int(count) > 0]
        if not filtered:
            return None
        total = sum(count for _name, count in filtered)
        pick = random.randint(1, total)
        running = 0
        for name, count in filtered:
            running += count
            if pick <= running:
                return name
        return filtered[-1][0]

    def _redeem_species_catalog(self) -> list[str]:
        names = list(dict.fromkeys(self._pokechain_display_names.values()))
        if names:
            return names
        seen: set[str] = set()
        catalog: list[str] = []
        for key, payload in self.pokemon_data.species_reference.items():
            name = str((payload or {}).get("name") or key).strip()
            normalized = normalize_lookup(name)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            catalog.append(name)
        return catalog

    def _resolve_redeem_species_name(self, query: str) -> str | None:
        return self._match_named_entry(self._redeem_species_catalog(), query)

    def _generate_redeem_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(10))

    async def _parse_makeredeem_payload(self, args_text: str) -> tuple[dict[str, Any] | None, str | None]:
        tokens = [part.strip() for part in str(args_text or "").split(",") if part.strip()]
        if not tokens:
            return None, "Usage: `/makeredeem vp-1000, lp-2000, egg-1, tm-4, ms-r, pk-pikachu, z-r, l-15`"

        held_catalog = await self._held_item_catalog_entries()
        mega_candidates = self._stone_item_names(held_catalog)
        z_candidates = sorted(Z_CRYSTALS)
        rewards: list[dict[str, Any]] = []
        max_redemptions: int | None = 1

        for token in tokens:
            if "-" not in token:
                return None, f"Invalid redeem token: `{token}`"
            raw_prefix, raw_value = token.split("-", 1)
            prefix = normalize_lookup(raw_prefix)
            value = str(raw_value or "").strip()
            value_key = normalize_lookup(value)
            if not prefix or not value:
                return None, f"Invalid redeem token: `{token}`"

            if prefix in {"l", "limit"}:
                if value_key in {"i", "inf", "infinite", "infinity", "unlimited"}:
                    max_redemptions = None
                    continue
                if not value.isdigit() or int(value) <= 0:
                    return None, "Redeem limit must be a positive number or `l-i` for infinite."
                max_redemptions = int(value)
                continue

            if prefix == "vp":
                if not value.isdigit() or int(value) <= 0:
                    return None, "VP reward must be a positive number."
                rewards.append({"kind": "vp", "amount": int(value)})
                continue

            if prefix == "lp":
                if not value.isdigit() or int(value) <= 0:
                    return None, "LP reward must be a positive number."
                rewards.append({"kind": "lp", "amount": int(value)})
                continue

            if prefix == "egg":
                if not value.isdigit() or int(value) <= 0:
                    return None, "Egg reward must be a positive number."
                rewards.append({"kind": "egg", "amount": int(value)})
                continue

            if prefix == "tm":
                if value_key == "r":
                    rewards.append({"kind": "tm_random", "amount": 4})
                    continue
                if not value.isdigit() or int(value) <= 0:
                    return None, "TM reward must be `tm-r` or `tm-<count>`."
                rewards.append({"kind": "tm_random", "amount": int(value)})
                continue

            if prefix in {"ms", "mega", "megastone"}:
                if value_key == "r":
                    rewards.append({"kind": "mega_random", "amount": 1})
                    continue
                item_name = self._match_named_entry(mega_candidates, value)
                if item_name is None:
                    return None, f"Unknown mega stone: `{value}`"
                rewards.append({"kind": "mega_specific", "name": item_name, "amount": 1})
                continue

            if prefix in {"pk", "pokemon"}:
                if value_key == "r":
                    rewards.append({"kind": "pokemon_random", "amount": 1})
                    continue
                species_name = self._resolve_redeem_species_name(value)
                if species_name is None:
                    return None, f"Unknown Pokemon: `{value}`"
                rewards.append({"kind": "pokemon_specific", "species": species_name, "amount": 1})
                continue

            if prefix in {"z", "zstone", "zcrystal"}:
                if value_key == "r":
                    rewards.append({"kind": "z_random", "amount": 1})
                    continue
                item_name = self._match_named_entry(z_candidates, value)
                if item_name is None:
                    return None, f"Unknown Z-Crystal: `{value}`"
                rewards.append({"kind": "z_specific", "name": item_name, "amount": 1})
                continue

            return None, f"Unknown redeem token: `{token}`"

        if not rewards:
            return None, "Add at least one redeem reward before creating the code."

        return {
            "rewards": rewards,
            "limit": max_redemptions,
        }, None

    def _format_redeem_rewards(self, rewards_payload: dict[str, Any]) -> list[str]:
        rewards = list(rewards_payload.get("rewards") or [])
        lines: list[str] = []
        for reward in rewards:
            kind = str((reward or {}).get("kind") or "")
            amount = max(1, int((reward or {}).get("amount") or 1))
            if kind == "vp":
                lines.append(f"Victory Points x{amount:,}")
            elif kind == "lp":
                lines.append(f"League Points x{amount:,}")
            elif kind == "egg":
                lines.append(f"Egg x{amount}")
            elif kind == "tm_random":
                lines.append(f"Random TM x{amount}")
            elif kind == "mega_random":
                lines.append("Random Mega Stone")
            elif kind == "mega_specific":
                lines.append(str(reward.get("name") or "Mega Stone"))
            elif kind == "pokemon_random":
                lines.append("Random Pokemon Lv. 1")
            elif kind == "pokemon_specific":
                lines.append(f"{reward.get('species', 'Pokemon')} Lv. 1")
            elif kind == "z_random":
                lines.append("Random Z-Crystal")
            elif kind == "z_specific":
                lines.append(str(reward.get("name") or "Z-Crystal"))
        return lines

    async def _grant_redeem_egg(self, trainer, teams: TeamRepository) -> str:
        species_pool = await self.daycare._egg_species()
        if not species_pool:
            raise ValueError("No egg species are available right now.")
        chosen_species = random.choice(species_pool)
        pokemon_data = await self.generator.generate_pokemon(
            species=chosen_species,
            level=1,
            region=trainer.current_region,
            source_kind="egg",
            friendship=70,
            shiny=False,
            item="",
        )
        egg_cycles = self.daycare._egg_cycles_for_species(pokemon_data["species"])
        eggs = self.daycare._egg_entries(trainer)
        eggs.append(
            self.daycare._activate_claimed_egg(
                {
                    "id": secrets.token_hex(6),
                    "source": "reward",
                    "egg_cycles": egg_cycles,
                    "pokemon_data": pokemon_data,
                },
                claimed_at=int(datetime.utcnow().timestamp()),
                accelerated=self.daycare._team_has_hatch_accelerator(trainer, teams),
            )
        )
        self.daycare._store_egg_entries(trainer, eggs)
        return "Egg"

    async def _grant_redeem_pokemon(self, trainer, pokemons: PokemonRepository, *, species_name: str) -> str:
        generated = await self.generator.generate_pokemon(
            species=species_name,
            level=1,
            region=trainer.current_region,
            source_kind="redeem",
            friendship=70,
            shiny=False,
            item="",
        )
        pokemons.create_owned_pokemon(trainer=trainer, data=generated)
        return str(generated.get("species") or species_name)

    async def _apply_redeem_rewards(
        self,
        trainer,
        *,
        rewards_payload: dict[str, Any],
        inventories: InventoryRepository,
        pokemons: PokemonRepository,
        teams: TeamRepository,
    ) -> list[str]:
        summaries: list[str] = []
        held_catalog = await self._held_item_catalog_entries()
        mega_candidates = self._stone_item_names(held_catalog)
        z_candidates = sorted(Z_CRYSTALS)
        tm_pool = list(self._tm_drop_map.values())
        pokemon_pool = self._redeem_species_catalog()

        for reward in list(rewards_payload.get("rewards") or []):
            kind = str((reward or {}).get("kind") or "")
            amount = max(1, int((reward or {}).get("amount") or 1))
            if kind == "vp":
                inventories.add_victory_points(trainer, amount)
                summaries.append(f"Victory Points x{amount:,}")
                continue
            if kind == "lp":
                inventories.add_league_points(trainer, amount)
                summaries.append(f"League Points x{amount:,}")
                continue
            if kind == "egg":
                for _ in range(amount):
                    summaries.append(await self._grant_redeem_egg(trainer, teams))
                continue
            if kind == "tm_random":
                if not tm_pool:
                    raise ValueError("No TM rewards are configured.")
                for _ in range(amount):
                    tm_name = random.choice(tm_pool)
                    inventories.add_tm(trainer, tm_name, 1)
                    summaries.append(tm_name)
                continue
            if kind == "mega_random":
                if not mega_candidates:
                    raise ValueError("No mega stones are configured.")
                for _ in range(amount):
                    item_name = random.choice(mega_candidates)
                    inventories.add_item(trainer, item_name, 1)
                    summaries.append(item_name)
                continue
            if kind == "mega_specific":
                item_name = str(reward.get("name") or "").strip()
                if not item_name:
                    raise ValueError("Redeem reward is missing a mega stone name.")
                inventories.add_item(trainer, item_name, amount)
                summaries.extend([item_name] * amount)
                continue
            if kind == "pokemon_random":
                if not pokemon_pool:
                    raise ValueError("No redeemable Pokemon species are configured.")
                for _ in range(amount):
                    summaries.append(await self._grant_redeem_pokemon(trainer, pokemons, species_name=random.choice(pokemon_pool)))
                continue
            if kind == "pokemon_specific":
                species_name = str(reward.get("species") or "").strip()
                if not species_name:
                    raise ValueError("Redeem reward is missing a Pokemon species.")
                for _ in range(amount):
                    summaries.append(await self._grant_redeem_pokemon(trainer, pokemons, species_name=species_name))
                continue
            if kind == "z_random":
                if not z_candidates:
                    raise ValueError("No Z-Crystals are configured.")
                for _ in range(amount):
                    item_name = random.choice(z_candidates)
                    inventories.add_item(trainer, item_name, 1)
                    summaries.append(item_name)
                continue
            if kind == "z_specific":
                item_name = str(reward.get("name") or "").strip()
                if not item_name:
                    raise ValueError("Redeem reward is missing a Z-Crystal name.")
                inventories.add_item(trainer, item_name, amount)
                summaries.extend([item_name] * amount)
                continue
            raise ValueError(f"Unknown redeem reward kind: {kind}")

        return summaries

    async def _handle_admin_command_lock(self, event: NewMessage.Event, *, lock: bool) -> None:
        parts = str(event.raw_text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            locked_commands = await run_db_work_async(
                lambda session: CommandLockRepository(session).list_locked(),
                read_only=True,
            )
            if not locked_commands:
                await event.respond("No commands are locked right now.")
                return
            await event.respond(
                "Locked commands: " + ", ".join(f"`/{name}`" for name in locked_commands),
                parse_mode="md",
            )
            return

        command_name = self._normalize_lockable_command_name(parts[1].strip())
        if command_name is None:
            available = ", ".join(f"`/{name}`" for name in sorted({"breed", "breeddata", "equip_item", "incubate", "redeem", "top", "train"}))
            await event.respond(f"Unknown lockable command.\nAvailable: {available}", parse_mode="md")
            return

        if lock:
            await run_db_work_async(
                lambda session: CommandLockRepository(session).lock(
                    command_name,
                    locked_by_user_id=int(event.sender_id or 0),
                ),
                read_only=False,
            )
            await event.respond(f"`/{command_name}` is now locked for users.", parse_mode="md")
            return

        changed = await run_db_work_async(
            lambda session: CommandLockRepository(session).unlock(command_name),
            read_only=False,
        )
        await event.respond(
            f"`/{command_name}` {'is now unlocked' if changed else 'was not locked'}.",
            parse_mode="md",
        )

    async def _handle_admin_locked_commands(self, event: NewMessage.Event) -> None:
        locked_commands = await run_db_work_async(
            lambda session: CommandLockRepository(session).list_locked(),
            read_only=True,
        )
        if not locked_commands:
            await event.respond("No commands are locked right now.")
            return
        await event.respond(
            "Locked commands: " + ", ".join(f"`/{name}`" for name in locked_commands),
            parse_mode="md",
        )

    async def _handle_admin_makeredeem(self, event: NewMessage.Event) -> None:
        parts = str(event.raw_text or "").split(maxsplit=1)
        args_text = parts[1] if len(parts) > 1 else ""
        payload, error = await self._parse_makeredeem_payload(args_text)
        if error or payload is None:
            await event.respond(str(error), parse_mode="md" if "`" in str(error) else None)
            return

        with db_session() as session:
            redeem_repo = RedeemCodeRepository(session)
            code = self._generate_redeem_code()
            while redeem_repo.get_code(code) is not None:
                code = self._generate_redeem_code()
            entry = redeem_repo.create_code(
                code=code,
                rewards_payload=payload,
                max_redemptions=payload.get("limit"),
                created_by_user_id=int(event.sender_id or 0),
            )

        limit_value = payload.get("limit")
        limit_text = "Infinite" if limit_value is None else str(int(limit_value))
        lines = [
            "**Redeem Code Created**",
            f"Code: `{entry.code}`",
            f"Limit: `{limit_text}`",
            "",
            "**Rewards**",
        ]
        lines.extend(f"• {line}" for line in self._format_redeem_rewards(payload))
        await event.respond("\n".join(lines), parse_mode="md")

    async def on_redeem(self, event: NewMessage.Event) -> None:
        if not await self._ensure_command_unlocked(event, "redeem"):
            return
        parts = str(event.raw_text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await event.respond("Usage: `/redeem <code>`", parse_mode="md")
            return
        code = RedeemCodeRepository.normalize_code(parts[1].strip().split()[0])
        sender = await event.get_sender()

        try:
            with db_session() as session:
                redeem_repo = RedeemCodeRepository(session)
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                teams = TeamRepository(session)

                trainer = trainers.get_by_telegram_user_id(int(event.sender_id or 0))
                if trainer is None:
                    await event.respond("Start your profile first with `/start` before using redeem codes.", parse_mode="md")
                    return

                entry = redeem_repo.get_code(code)
                if entry is None:
                    await event.respond("That redeem code does not exist.")
                    return
                if redeem_repo.has_user_redeemed(entry, int(event.sender_id or 0)):
                    await event.respond("You already redeemed that code.")
                    return
                if not redeem_repo.can_redeem(entry):
                    await event.respond("That redeem code has reached its limit.")
                    return

                rewards_payload = redeem_repo.parse_rewards(entry)
                reward_lines = await self._apply_redeem_rewards(
                    trainer,
                    rewards_payload=rewards_payload,
                    inventories=inventories,
                    pokemons=pokemons,
                    teams=teams,
                )
                redeem_repo.record_redemption(entry, telegram_user_id=int(event.sender_id or 0))
                remaining = redeem_repo.remaining_redemptions(entry)
        except ValueError as exc:
            message = str(exc)
            if message == "already_redeemed":
                message = "You already redeemed that code."
            elif message == "redeem_limit_reached":
                message = "That redeem code has reached its limit."
            await event.respond(message)
            return
        except Exception:
            logger.exception(
                "Redeem failed for user_id=%s raw_code=%r normalized_code=%r",
                int(event.sender_id or 0),
                parts[1].strip(),
                code,
            )
            await event.respond("Redeem failed right now. Please try again in a moment.")
            return

        lines = [
            f"`{display_name(sender)}` redeemed `{code}`.",
            "",
            "**Rewards Received**",
        ]
        lines.extend(f"• {line}" for line in reward_lines)
        if remaining is None:
            lines.extend(["", "Remaining uses: `Infinite`"])
        else:
            lines.extend(["", f"Remaining uses: `{remaining}`"])
        await event.respond("\n".join(lines), parse_mode="md")

    async def _handle_admin_item_command(self, event: NewMessage.Event, *, action: str) -> None:
        command_name = "additem" if action == "add" else "remove"
        target_user = await self._reply_target_user(event)
        if target_user is None:
            await event.respond(self._admin_item_usage_text(command_name), parse_mode="md")
            return

        parts = str(event.raw_text or "").split()
        if len(parts) < 3 or not parts[-1].isdigit():
            await event.respond(self._admin_item_usage_text(command_name), parse_mode="md")
            return

        amount = int(parts[-1])
        if amount <= 0:
            await event.respond("Amount must be a positive number.")
            return

        category = self._normalize_admin_item_category(parts[1])
        if category is None:
            await event.respond("Unknown item category.")
            return
        detail = " ".join(parts[2:-1]).strip()
        detail_key = normalize_lookup(detail)

        held_catalog: list[str] = []
        if category in {"held", "stone", "mint"}:
            held_catalog = await self._held_item_catalog_entries()

        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.get_by_telegram_user_id(int(target_user.id))
            if trainer is None:
                await event.respond(f"{display_name(target_user)} hasn't started their adventure yet.")
                return

            summary: Counter[str] = Counter()
            error: str | None = None

            if category == "vp":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_victory_points(trainer, amount)
                    summary["Victory Points"] += amount
                elif not inventories.consume_victory_points(trainer, amount):
                    error = "That trainer does not have enough Victory Points."
                else:
                    summary["Victory Points"] += amount
            elif category == "lp":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_league_points(trainer, amount)
                    summary["League Points"] += amount
                elif trainer.inventory.league_points < amount:
                    error = "That trainer does not have enough League Points."
                else:
                    trainer.inventory.league_points -= amount
                    summary["League Points"] += amount
            elif category == "ht":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_key_item(trainer, KEY_ITEM_HOLOWEAR_TICKET, amount)
                    summary[KEY_ITEM_HOLOWEAR_TICKET] += amount
                elif not inventories.consume_key_item(trainer, KEY_ITEM_HOLOWEAR_TICKET, amount):
                    error = f"That trainer does not have enough {KEY_ITEM_HOLOWEAR_TICKET}."
                else:
                    summary[KEY_ITEM_HOLOWEAR_TICKET] += amount
            elif category == "battlebox":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_key_item(trainer, KEY_ITEM_TRAINER_BOX, amount)
                    summary[KEY_ITEM_TRAINER_BOX] += amount
                elif not inventories.consume_key_item(trainer, KEY_ITEM_TRAINER_BOX, amount):
                    error = f"That trainer does not have enough {KEY_ITEM_TRAINER_BOX}."
                else:
                    summary[KEY_ITEM_TRAINER_BOX] += amount
            elif category == "daycarecandy":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_key_item(trainer, KEY_ITEM_DAYCARE_CANDY, amount)
                    summary[KEY_ITEM_DAYCARE_CANDY] += amount
                elif not inventories.consume_key_item(trainer, KEY_ITEM_DAYCARE_CANDY, amount):
                    error = f"That trainer does not have enough {KEY_ITEM_DAYCARE_CANDY}."
                else:
                    summary[KEY_ITEM_DAYCARE_CANDY] += amount
            elif category == "maxsoup":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_key_item(trainer, KEY_ITEM_MAX_SOUP, amount)
                    summary[KEY_ITEM_MAX_SOUP] += amount
                elif not inventories.consume_key_item(trainer, KEY_ITEM_MAX_SOUP, amount):
                    error = f"That trainer does not have enough {KEY_ITEM_MAX_SOUP}."
                else:
                    summary[KEY_ITEM_MAX_SOUP] += amount
            elif category == "dynamaxcandy":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_key_item(trainer, KEY_ITEM_DYNAMAX_CANDY, amount)
                    summary[KEY_ITEM_DYNAMAX_CANDY] += amount
                elif not inventories.consume_key_item(trainer, KEY_ITEM_DYNAMAX_CANDY, amount):
                    error = f"That trainer does not have enough {KEY_ITEM_DYNAMAX_CANDY}."
                else:
                    summary[KEY_ITEM_DYNAMAX_CANDY] += amount
            elif category == "omniring":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_key_item(trainer, KEY_ITEM_OMNI_RING, amount)
                    summary[KEY_ITEM_OMNI_RING] += amount
                elif not inventories.consume_key_item(trainer, KEY_ITEM_OMNI_RING, amount):
                    error = f"That trainer does not have enough {KEY_ITEM_OMNI_RING}."
                else:
                    summary[KEY_ITEM_OMNI_RING] += amount
            elif category == "abilitycapsule":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_item(trainer, ABILITY_CAPSULE_ITEM, amount)
                    summary[ABILITY_CAPSULE_ITEM] += amount
                elif inventories.held_item_count(trainer, ABILITY_CAPSULE_ITEM) < amount:
                    error = f"That trainer does not have enough {ABILITY_CAPSULE_ITEM}."
                else:
                    inventories.consume_item(trainer, ABILITY_CAPSULE_ITEM, amount)
                    summary[ABILITY_CAPSULE_ITEM] += amount
            elif category == "abilitypatch":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_item(trainer, ABILITY_PATCH_ITEM, amount)
                    summary[ABILITY_PATCH_ITEM] += amount
                elif inventories.held_item_count(trainer, ABILITY_PATCH_ITEM) < amount:
                    error = f"That trainer does not have enough {ABILITY_PATCH_ITEM}."
                else:
                    inventories.consume_item(trainer, ABILITY_PATCH_ITEM, amount)
                    summary[ABILITY_PATCH_ITEM] += amount
            elif category == "bottlecap":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_item(trainer, BOTTLE_CAP_ITEM, amount)
                    summary[BOTTLE_CAP_ITEM] += amount
                elif inventories.held_item_count(trainer, BOTTLE_CAP_ITEM) < amount:
                    error = f"That trainer does not have enough {BOTTLE_CAP_ITEM}."
                else:
                    inventories.consume_item(trainer, BOTTLE_CAP_ITEM, amount)
                    summary[BOTTLE_CAP_ITEM] += amount
            elif category == "goldbottlecap":
                if detail:
                    error = self._admin_item_usage_text(command_name)
                elif action == "add":
                    inventories.add_item(trainer, GOLD_BOTTLE_CAP_ITEM, amount)
                    summary[GOLD_BOTTLE_CAP_ITEM] += amount
                elif inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM) < amount:
                    error = f"That trainer does not have enough {GOLD_BOTTLE_CAP_ITEM}."
                else:
                    inventories.consume_item(trainer, GOLD_BOTTLE_CAP_ITEM, amount)
                    summary[GOLD_BOTTLE_CAP_ITEM] += amount
            elif category not in {"ball", "tm", "held", "stone", "zcrystal", "mint"}:
                error = "That item category is not supported yet."

            if error:
                await event.respond(error, parse_mode="md" if "`/" in error else None)
                return

            if category == "ball":
                if not detail:
                    error = self._admin_item_usage_text(command_name)
                elif detail_key == "any":
                    available = inventories.ball_counts(trainer) if action == "remove" else [(ball_kind, 1) for ball_kind in BALL_ORDER]
                    if action == "remove" and sum(count for _name, count in available) < amount:
                        error = "That trainer does not have enough balls in total."
                    else:
                        local_counts = {name: count for name, count in available}
                        for _ in range(amount):
                            picked = self._weighted_random_choice(list(local_counts.items()))
                            if picked is None:
                                error = "No matching balls are available."
                                break
                            if action == "add":
                                inventories.add_ball(trainer, picked, 1)
                            else:
                                inventories.consume_ball(trainer, picked)
                                local_counts[picked] = max(0, int(local_counts.get(picked, 0)) - 1)
                            summary[ball_label(picked)] += 1
                else:
                    ball_kind = normalize_ball_kind(detail)
                    if ball_kind is None:
                        error = "Unknown ball."
                    elif action == "add":
                        inventories.add_ball(trainer, ball_kind, amount)
                        summary[ball_label(ball_kind)] += amount
                    elif inventories.ball_count(trainer, ball_kind) < amount:
                        error = f"That trainer does not have enough {ball_label(ball_kind)}."
                    else:
                        for _ in range(amount):
                            inventories.consume_ball(trainer, ball_kind)
                        summary[ball_label(ball_kind)] += amount
            elif category == "tm":
                if not detail:
                    error = self._admin_item_usage_text(command_name)
                elif detail_key == "any":
                    available = list(inventories.tm_counts(trainer).items()) if action == "remove" else [(label, 1) for label in self._tm_drop_map.values()]
                    if action == "remove" and sum(int(count) for _name, count in available) < amount:
                        error = "That trainer does not have enough TMs in total."
                    else:
                        local_counts = {name: int(count) for name, count in available}
                        for _ in range(amount):
                            picked = self._weighted_random_choice(list(local_counts.items()))
                            if picked is None:
                                error = "No matching TMs are available."
                                break
                            if action == "add":
                                inventories.add_tm(trainer, picked, 1)
                            else:
                                inventories.consume_tm(trainer, picked, 1)
                                local_counts[picked] = max(0, int(local_counts.get(picked, 0)) - 1)
                            summary[picked] += 1
                else:
                    tm_label = self._resolve_admin_tm_label(detail)
                    if tm_label is None:
                        error = "Unknown TM."
                    elif action == "add":
                        inventories.add_tm(trainer, tm_label, amount)
                        summary[tm_label] += amount
                    else:
                        current_name, current_count = next(
                            (
                                (name, int(count))
                                for name, count in inventories.tm_counts(trainer).items()
                                if normalize_lookup(name) == normalize_lookup(tm_label)
                            ),
                            (None, 0),
                        )
                        if not current_name or current_count < amount:
                            error = f"That trainer does not have enough {tm_label}."
                        else:
                            inventories.consume_tm(trainer, current_name, amount)
                            summary[current_name] += amount
            elif category in {"held", "stone", "zcrystal", "mint"}:
                if not detail:
                    error = self._admin_item_usage_text(command_name)
                else:
                    if category == "held":
                        candidate_names = list(held_catalog)
                    elif category == "stone":
                        candidate_names = self._stone_item_names(held_catalog)
                    elif category == "zcrystal":
                        candidate_names = sorted(Z_CRYSTALS)
                    else:
                        candidate_names = self._mint_item_names()

                    if detail_key == "any":
                        available_items = inventories.held_item_counts(trainer) if action == "remove" else {name: 1 for name in candidate_names}
                        allowed = {normalize_lookup(name) for name in candidate_names}
                        filtered = {
                            name: int(count)
                            for name, count in available_items.items()
                            if int(count) > 0 and normalize_lookup(name) in allowed
                        }
                        if action == "remove" and sum(filtered.values()) < amount:
                            error = "That trainer does not have enough matching items."
                        else:
                            local_counts = dict(filtered)
                            if action == "add":
                                local_counts = {name: 1 for name in candidate_names}
                            for _ in range(amount):
                                picked = self._weighted_random_choice(list(local_counts.items()))
                                if picked is None:
                                    error = "No matching items are available."
                                    break
                                if action == "add":
                                    inventories.add_item(trainer, picked, 1)
                                else:
                                    inventories.consume_item(trainer, picked, 1)
                                    local_counts[picked] = max(0, int(local_counts.get(picked, 0)) - 1)
                                summary[picked] += 1
                    else:
                        item_name = self._match_named_entry(candidate_names, detail)
                        if item_name is None:
                            error = "Unknown item name."
                        elif action == "add":
                            inventories.add_item(trainer, item_name, amount)
                            summary[item_name] += amount
                        elif inventories.held_item_count(trainer, item_name) < amount:
                            error = f"That trainer does not have enough {item_name}."
                        else:
                            inventories.consume_item(trainer, item_name, amount)
                            summary[item_name] += amount

            if error:
                await event.respond(error, parse_mode="md" if "`/" in error else None)
                return

        verb = "Added" if action == "add" else "Removed"
        lines = [f"{verb} item data for `{display_name(target_user)}`."]
        if summary:
            lines.extend(["", *self._format_admin_summary(summary)])
        await event.respond("\n".join(lines), parse_mode="md")

    async def _handle_admin_addexp(self, event: NewMessage.Event) -> None:
        target_user = await self._reply_target_user(event)
        if target_user is None:
            await event.respond("Reply to a trainer with `/addexp <amount>`.", parse_mode="md")
            return

        parts = str(event.raw_text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await event.respond("Usage: `/addexp <amount>`", parse_mode="md")
            return
        amount = int(parts[1].strip())
        if amount <= 0:
            await event.respond("EXP amount must be positive.")
            return

        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.get_by_telegram_user_id(int(target_user.id))
            if trainer is None:
                await event.respond(f"{display_name(target_user)} hasn't started their adventure yet.")
                return
            result = trainers.add_trainer_exp(trainer, amount)

        lines = [
            f"Added EXP to `{display_name(target_user)}`.",
            f"EXP: `{result['old_exp']}` -> `{result['new_exp']}`",
            f"Level: `{result['old_level']}` -> `{result['new_level']}`",
        ]
        reward_lines = list(result.get("level_reward_lines") or [])
        if reward_lines:
            lines.extend(["", "**Level Rewards**"])
            lines.extend([f"• {line}" for line in reward_lines])
        await event.respond("\n".join(lines), parse_mode="md")

    async def _handle_admin_ban_toggle(self, event: NewMessage.Event, *, ban: bool) -> None:
        raw_parts = str(event.raw_text or "").split(maxsplit=1)
        args_text = raw_parts[1] if len(raw_parts) > 1 else ""
        target_user = await self._reply_target_user(event)
        target_id = int(target_user.id) if target_user is not None else None
        if target_id is None:
            ids = self._extract_integer_arguments(args_text)
            if ids:
                target_id = int(ids[0])
        if target_id is None:
            command_name = "bfb" if ban else "ufb"
            await event.respond(f"Reply to a trainer or use `/{command_name} <user_id>`.", parse_mode="md")
            return
        if target_id in ADMIN_USER_ID_SET:
            await event.respond("Configured admins cannot be banned.")
            return

        with db_session() as session:
            admin_repo = AdminRepository(session)
            changed = (
                admin_repo.ban_user(target_id, added_by_user_id=int(event.sender_id or 0))
                if ban
                else admin_repo.unban_user(target_id)
            )

        label = display_name(target_user, fallback=str(target_id))
        if ban:
            await event.respond(f"{'Banned' if changed else 'Already banned'} `{label}`.", parse_mode="md")
            return
        await event.respond(f"{'Unbanned' if changed else 'Not banned'} `{label}`.", parse_mode="md")

    async def _handle_admin_broadcast(self, event: NewMessage.Event) -> None:
        if not event.is_reply:
            await event.respond("Reply to the message you want to broadcast with `/broad`.", parse_mode="md")
            return
        reply_message = await event.get_reply_message()
        if reply_message is None:
            await event.respond("Reply to the message you want to broadcast with `/broad`.", parse_mode="md")
            return

        with db_session(read_only=True) as session:
            trainer_ids = TrainerRepository(session).list_telegram_user_ids()
            group_ids = AdminRepository(session).list_group_chat_ids()

        dm_targets = sorted({int(user_id) for user_id in trainer_ids})
        group_targets = sorted({int(chat_id) for chat_id in group_ids})
        if not dm_targets and not group_targets:
            await event.respond("No broadcast targets are registered yet.")
            return

        dm_success = dm_failed = 0
        group_success = group_failed = 0
        client = self.battle_service.client

        for index, chat_id in enumerate(dm_targets + group_targets, start=1):
            try:
                await client.forward_messages(chat_id, reply_message)
                if chat_id < 0:
                    group_success += 1
                else:
                    dm_success += 1
            except Exception:
                if chat_id < 0:
                    group_failed += 1
                else:
                    dm_failed += 1
            if index % 20 == 0:
                await asyncio.sleep(0.05)

        lines = [
            "**Broadcast Complete**",
            f"DMs sent: `{dm_success}`",
            f"DMs failed: `{dm_failed}`",
            f"Groups sent: `{group_success}`",
            f"Groups failed: `{group_failed}`",
        ]
        await event.respond("\n".join(lines), parse_mode="md")

    async def _handle_admin_id_transfer(self, event: NewMessage.Event) -> None:
        raw_parts = str(event.raw_text or "").split(maxsplit=1)
        args_text = raw_parts[1] if len(raw_parts) > 1 else ""
        ids = self._extract_integer_arguments(args_text)
        if len(ids) < 2:
            await event.respond("Usage: `/id_transfer <old_id>, <new_id>`", parse_mode="md")
            return
        old_id, new_id = int(ids[0]), int(ids[1])
        if old_id == new_id:
            await event.respond("Old and new IDs must be different.")
            return
        if old_id in ADMIN_USER_ID_SET or new_id in ADMIN_USER_ID_SET:
            await event.respond("Configured admins cannot be moved with `/id_transfer`.", parse_mode="md")
            return

        for user_id in {old_id, new_id}:
            reason = self._admin_runtime_lock_reason(user_id)
            if reason:
                await event.respond(f"Cannot transfer `{user_id}` right now: {reason}", parse_mode="md")
                return

        with db_session() as session:
            trainers = TrainerRepository(session)
            old_trainer = trainers.get_by_telegram_user_id(old_id)
            new_trainer = trainers.get_by_telegram_user_id(new_id)
            if old_trainer is None:
                await event.respond(f"No trainer data exists for `{old_id}`.", parse_mode="md")
                return

            if new_trainer is None:
                old_trainer.telegram_user_id = new_id
                action_text = f"Moved trainer data from `{old_id}` to `{new_id}`."
            else:
                temp_id = -(max(abs(old_id), abs(new_id)) + int(datetime.utcnow().timestamp()))
                old_trainer.telegram_user_id = temp_id
                session.flush()
                new_trainer.telegram_user_id = old_id
                session.flush()
                old_trainer.telegram_user_id = new_id
                action_text = f"Swapped trainer data between `{old_id}` and `{new_id}`."

        await event.respond(action_text, parse_mode="md")

    async def _handle_admin_reset(self, event: NewMessage.Event) -> None:
        raw_parts = str(event.raw_text or "").split(maxsplit=1)
        args_text = raw_parts[1] if len(raw_parts) > 1 else ""
        target_user = await self._reply_target_user(event)
        target_id = int(target_user.id) if target_user is not None else None
        if target_id is None:
            ids = self._extract_integer_arguments(args_text)
            if ids:
                target_id = int(ids[0])
        if target_id is None:
            await event.respond("Reply to a trainer or use `/reset <user_id>`.", parse_mode="md")
            return
        if target_id in ADMIN_USER_ID_SET:
            await event.respond("Configured admins cannot be reset.", parse_mode="md")
            return

        reason = self._admin_runtime_lock_reason(target_id)
        if reason:
            await event.respond(f"Cannot reset `{target_id}` right now: {reason}", parse_mode="md")
            return

        label = display_name(target_user, fallback=str(target_id))
        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.get_by_telegram_user_id(target_id)
            if trainer is None:
                await event.respond(f"No trainer data exists for `{label}`.", parse_mode="md")
                return
            trainers.delete_trainer(trainer)

        self.training_sessions.pop(target_id, None)
        self.box_selection_by_user.pop(target_id, None)
        self._clear_command_use_sessions_for_user(target_id)
        self._clear_relearner_sessions_for_user(target_id)
        trade_id = self.trade_by_user.get(target_id)
        if trade_id is not None:
            trade = self.trade_sessions.get(trade_id)
            if trade is not None:
                self._release_trade(trade)
        self.encounters.active_by_user.pop(target_id, None)

        await event.respond(f"Reset all trainer data for `{label}`.", parse_mode="md")

    async def _handle_admin_rank_reset_all(self, event: NewMessage.Event) -> None:
        from sqlalchemy import select as sa_select
        from bot.db.models import Trainer

        count = 0
        with db_session() as session:
            trainers = TrainerRepository(session)
            for trainer in list(session.scalars(sa_select(Trainer))):
                if int(trainer.trainer_level or 1) != 1 or int(trainer.trainer_exp or 0) != 0:
                    count += 1
                trainers.reset_trainer_level(trainer)
        await event.respond(f"Reset trainer ranks for `{count}` user(s).", parse_mode="md")

    async def _handle_admin_weekend_boost(self, event: NewMessage.Event) -> None:
        parts = str(event.raw_text or "").strip().split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action in {"status", "state"}:
            await event.respond(weekend_boost_status_text())
            return
        if action in {"on", "enable"}:
            try:
                set_weekend_boost_enabled(True, actor_user_id=int(event.sender_id or 0))
            except ValueError as exc:
                await event.respond(f"{exc}\n\n{weekend_boost_status_text()}")
                return
            await event.respond(f"Weekend boost enabled.\n\n{weekend_boost_status_text()}")
            return
        if action in {"off", "disable"}:
            set_weekend_boost_enabled(False, actor_user_id=int(event.sender_id or 0))
            await event.respond(f"Weekend boost disabled.\n\n{weekend_boost_status_text()}")
            return
        await event.respond("Usage: /weekendboost <status|on|off>")

    async def on_admin_legacy(self, event: NewMessage.Event) -> None:
        if not await self._require_admin(event):
            return

        command = str(event.raw_text or "").strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/fdc":
            await self.on_forcecomplete(event)
            return
        if command == "/reset_battle":
            await self.battle_service.on_exit_command(event)
            return
        if command == "/additem":
            await self._handle_admin_item_command(event, action="add")
            return
        if command in {"/remove", "/removeitem"}:
            await self._handle_admin_item_command(event, action="remove")
            return
        if command == "/addexp":
            await self._handle_admin_addexp(event)
            return
        if command == "/bfb":
            await self._handle_admin_ban_toggle(event, ban=True)
            return
        if command == "/ufb":
            await self._handle_admin_ban_toggle(event, ban=False)
            return
        if command == "/broad":
            await self._handle_admin_broadcast(event)
            return
        if command == "/id_transfer":
            await self._handle_admin_id_transfer(event)
            return
        if command == "/reset":
            await self._handle_admin_reset(event)
            return
        if command == "/treset":
            await self._handle_admin_rank_reset_all(event)
            return
        if command in {"/weekendboost", "/weekendmode"}:
            await self._handle_admin_weekend_boost(event)
            return
        if command == "/lock":
            await self._handle_admin_command_lock(event, lock=True)
            return
        if command == "/unlock":
            await self._handle_admin_command_lock(event, lock=False)
            return
        if command == "/locked":
            await self._handle_admin_locked_commands(event)
            return
        if command == "/makeredeem":
            await self._handle_admin_makeredeem(event)
            return
        if command in {"/synccommands", "/setcommands"}:
            await self._handle_admin_sync_commands(event)
            return
        if command == "/record":
            await self.on_record(event)
            return
        if command == "/status":
            await self.on_status(event)
            return
        if command == "/top":
            await self.on_top(event)
            return

        await event.respond(
            f"`{command}` is recognized but not implemented in this Python build yet.",
            parse_mode="md",
        )

    async def on_rankup(self, event: NewMessage.Event) -> None:
        if int(event.sender_id or 0) not in RANKUP_ADMIN_IDS:
            await event.respond("Access denied.")
            return
        parts = event.raw_text.split()
        if len(parts) < 2 or not parts[1].lstrip("+-").isdigit():
            await event.respond("Usage: /rankup <+level> (reply to a user or run on yourself)")
            return
        amount = int(parts[1].lstrip("+"))
        if amount <= 0:
            await event.respond("Rankup amount must be positive.")
            return

        target_id = int(event.sender_id or 0)
        target_sender = await event.get_sender()
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg is not None and reply_msg.sender_id:
                target_id = int(reply_msg.sender_id)
                target_sender = reply_msg.sender if isinstance(reply_msg.sender, User) else await reply_msg.get_sender()

        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=target_id,
                username=getattr(target_sender, "username", None) if target_sender else None,
                display_name=display_name(target_sender, fallback=str(target_id)),
            )
            result = trainers.rank_up_trainer_levels(trainer, levels=amount)

        lines = [
            "✅ Rankup applied.",
            f"Trainer: `{display_name(target_sender, fallback=str(target_id))}`",
            f"Level: `{result['old_level']}` -> `{result['new_level']}`",
            f"Levels gained: `{result['levels_gained']}`",
        ]
        reward_lines = list(result.get("level_reward_lines") or [])
        if reward_lines:
            lines.extend(["", "**Level Rewards**"])
            for line in reward_lines:
                lines.append(f"• {line}")
        await event.respond("\n".join(lines), parse_mode="md")

    async def on_resetrank(self, event: NewMessage.Event) -> None:
        if int(event.sender_id or 0) not in RANKUP_ADMIN_IDS:
            await event.respond("Access denied.")
            return

        target_id = int(event.sender_id or 0)
        target_sender = await event.get_sender()
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg is not None and reply_msg.sender_id:
                target_id = int(reply_msg.sender_id)
                target_sender = reply_msg.sender if isinstance(reply_msg.sender, User) else await reply_msg.get_sender()

        with db_session() as session:
            trainers = TrainerRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=target_id,
                username=getattr(target_sender, "username", None) if target_sender else None,
                display_name=display_name(target_sender, fallback=str(target_id)),
            )
            result = trainers.reset_trainer_level(trainer)

        lines = [
            "Rank reset applied.",
            f"Trainer: `{display_name(target_sender, fallback=str(target_id))}`",
            f"Level: `{result['old_level']}` -> `{result['new_level']}`",
            f"EXP: `{result['old_exp']}` -> `{result['new_exp']}`",
        ]
        await event.respond("\n".join(lines), parse_mode="md")

    async def on_send(self, event: NewMessage.Event) -> None:
        if not event.is_reply:
            await event.respond("Reply to a trainer with /send <amount>.")
            return
            
        parts = event.raw_text.split()
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
            await event.respond("Use /send <amount> with a valid positive number.")
            return
            
        amount = int(parts[1])
        reply_message = await event.get_reply_message()
        if reply_message is None:
            await event.respond("Reply to a trainer with /send <amount>.")
            return
            
        target_user = await reply_message.get_sender()
        if not isinstance(target_user, User) or getattr(target_user, "bot", False):
            await event.respond("You can only send Victory Points to another player.")
            return

        if target_user.id == event.sender_id:
            await event.respond("You cannot send Victory Points to yourself.")
            return

        sender_user = await event.get_sender()
        success = False
        target_name = display_name(target_user)
        
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            
            sender_trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender_user, "username", None),
                display_name=display_name(sender_user),
            )
            
            target_trainer = trainers.get_by_telegram_user_id(target_user.id)
            if target_trainer is None:
                await event.respond(f"{target_name} hasn't started their adventure yet!")
                return
                
            if inventories.consume_victory_points(sender_trainer, amount):
                inventories.add_victory_points(target_trainer, amount)
                success = True

        if success:
            await event.respond(f"Successfully sent {amount} VP to {target_name}!")
        else:
            await event.respond("You do not have enough Victory Points (VP) for this transfer.")

    def _trade_lock_reason(self, user_id: int) -> str | None:
        if user_id in self.trade_by_user:
            return "You already have another active trade."
        reason = self.battle_service.pvp_lock_reason(user_id)
        if reason:
            return reason
        if self.encounters.active_by_user.get(user_id) is not None:
            return "Finish your current encounter before trading Pokemon."
        return None

    def _register_trade(self, trade: TradeSession) -> None:
        self.trade_sessions[trade.trade_id] = trade
        self.trade_by_user[trade.requester_id] = trade.trade_id
        self.trade_by_user[trade.target_id] = trade.trade_id

    def _release_trade(self, trade: TradeSession) -> None:
        self.trade_sessions.pop(trade.trade_id, None)
        if self.trade_by_user.get(trade.requester_id) == trade.trade_id:
            self.trade_by_user.pop(trade.requester_id, None)
        if self.trade_by_user.get(trade.target_id) == trade.trade_id:
            self.trade_by_user.pop(trade.target_id, None)

    async def _clear_trade_state_on_exit(self, user_id: int, *, actor_name: str) -> list[str]:
        trade_id = self.trade_by_user.get(user_id)
        if not trade_id:
            return []
        trade = self.trade_sessions.get(trade_id)
        if trade is None:
            self.trade_by_user.pop(user_id, None)
            return ["Cleared a stale trade reservation."]

        async with trade.lock:
            current_trade = self.trade_sessions.get(trade.trade_id)
            if current_trade is None:
                return []
            trade.state = "cancelled"
            self._release_trade(trade)
            if trade.chat_id and trade.public_message_id:
                await safe_client_edit(
                    self.battle_service.client,
                    trade.chat_id,
                    trade.public_message_id,
                    f"Trade cancelled because {actor_name} used /exit.",
                    buttons=None,
                )
        return ["Cancelled your active trade."]

    def trade_status_text(self, trade: TradeSession) -> str:
        return {
            "pending": "Waiting for accept.",
            "selecting": "Selecting Pokemon.",
            "confirming": "Waiting for both players to accept.",
            "completed": "Completed.",
            "declined": "Declined.",
            "cancelled": "Cancelled.",
        }.get(trade.state, trade.state.title())

    def trade_request_text(self, trade: TradeSession) -> str:
        return "\n".join([
            "<b>TRADE CENTER</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"<b>Request From:</b> {html.escape(trade.requester_name)}",
            f"<b>Target:</b> {html.escape(trade.target_name)}",
            f"<b>Status:</b> {html.escape(self.trade_status_text(trade))}",
            "",
            f"{html.escape(trade.target_name)} can accept or decline below.",
        ])

    def trade_request_buttons(self, trade: TradeSession) -> list[list[Button]] | None:
        if trade.state != "pending":
            return None
        return [[
            Button.inline("Accept Trade", data=f"trade:{trade.trade_id}:accept".encode("utf-8")),
            Button.inline("Decline", data=f"trade:{trade.trade_id}:decline".encode("utf-8")),
        ]]

    def tradable_owned_pokemon(self, trainer, pokemons: PokemonRepository) -> list:
        return [
            pokemon
            for pokemon in self.sorted_owned_pokemon(trainer, pokemons)
            if not pokemon.untradeable and not has_form_state(pokemon)
        ]

    def trade_pokemon_page(self, trainer, pokemons: PokemonRepository, *, page: int) -> tuple[list, int, int]:
        pokemon_list = self.tradable_owned_pokemon(trainer, pokemons)
        total = len(pokemon_list)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // POKEMON_LIST_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * POKEMON_LIST_PAGE_SIZE
        end = start + POKEMON_LIST_PAGE_SIZE
        return pokemon_list[start:end], total, current_page

    def trade_current_picker_id(self, trade: TradeSession) -> int | None:
        if trade.selected_by.get(trade.requester_id) is None:
            return trade.requester_id
        if trade.selected_by.get(trade.target_id) is None:
            return trade.target_id
        return None

    def trade_other_name(self, trade: TradeSession, user_id: int) -> str:
        return trade.target_name if user_id == trade.requester_id else trade.requester_name

    def trade_offer_status_text(self, trade: TradeSession) -> str:
        requester_status = "Selected" if trade.selected_by.get(trade.requester_id) is not None else "Waiting"
        target_status = "Selected" if trade.selected_by.get(trade.target_id) is not None else "Waiting"
        return (
            f"<b>{html.escape(trade.requester_name)}</b>: {requester_status}\n"
            f"<b>{html.escape(trade.target_name)}</b>: {target_status}"
        )

    def trade_picker_text(
        self,
        trade: TradeSession,
        trainer,
        *,
        user_id: int,
        target_name: str,
        page: int,
        total: int,
        items: list,
    ) -> str:
        max_page = max(1, ((max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE) + 1)
        lines = [
            "<b>TRADE CENTER</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            self.trade_offer_status_text(trade),
            "",
            f"<b>Now Choosing:</b> {html.escape(trainer.display_name)}",
            f"<b>Page:</b> {page + 1}/{max_page}",
            "",
        ]
        if not items:
            lines.append("No tradable Pokemon available.")
        else:
            start = page * POKEMON_LIST_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"{index}. {html.escape(self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode))}")
        lines.extend(["", f"Choose the Pokemon you want to offer to <b>{html.escape(target_name)}</b>."])
        return "\n".join(lines)

    def trade_picker_buttons(
        self,
        trade: TradeSession,
        *,
        user_id: int,
        page: int,
        total: int,
        items: list,
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        number_buttons = [
            Button.inline(
                str(index),
                data=f"trade:{trade.trade_id}:pick:{user_id}:{page}:{pokemon.id}".encode("utf-8"),
            )
            for index, pokemon in enumerate(items, start=(page * POKEMON_LIST_PAGE_SIZE) + 1)
        ]
        rows.extend(chunk_buttons(number_buttons, per_row=5))

        max_page = max(0, (max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE)
        nav_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<-", data=f"trade:{trade.trade_id}:page:{user_id}:{page - 1}".encode("utf-8")))
        nav_row.append(Button.inline(f"{page + 1}/{max_page + 1}", data=f"trade:{trade.trade_id}:noop".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline("->", data=f"trade:{trade.trade_id}:page:{user_id}:{page + 1}".encode("utf-8")))
        rows.append(nav_row)
        rows.append([Button.inline("Cancel Trade", data=f"trade:{trade.trade_id}:decline".encode("utf-8"))])
        return rows

    def trade_pokemon_summary_block(self, owner_name: str, pokemon) -> str:
        rows = [
            ("HP ",  pokemon.iv_hp,  pokemon.ev_hp),
            ("Atk", pokemon.iv_atk, pokemon.ev_atk),
            ("Def", pokemon.iv_def, pokemon.ev_def),
            ("SpA", pokemon.iv_spa, pokemon.ev_spa),
            ("SpD", pokemon.iv_spd, pokemon.ev_spd),
            ("Spe", pokemon.iv_spe, pokemon.ev_spe),
        ]
        total_ev = sum(row[2] for row in rows)

        detail_bits = [f"Lv. {int(pokemon.level)}", str(pokemon.nature)]
        if getattr(pokemon, "item", ""):
            detail_bits.append(f"Item: {pokemon.item}")
            
        # Determine if the sparkle emoji should be shown
        shiny_icon = " ✨" if pokemon.shiny else ""
            
        lines = [
            f"<b>{html.escape(owner_name)}</b> is offering",
            # Inject the shiny icon right after the species name
            f"<b>{html.escape(effective_species(pokemon))}{shiny_icon}</b>",
            html.escape(" • ".join(detail_bits)),
            "",
            "<code>Stat    IV   EV</code>",
            f"<code>{'━' * 18}</code>",
        ]
        
        for label, iv_value, ev_value in rows:
            lines.append(f"<code>{label}    {iv_value:>3}  {ev_value:>3}</code>")
            
        lines.extend([
            f"<code>{'━' * 18}</code>",
            f"<code>{'Tot'}   {pokemon.total_iv:>3}  {total_ev:>3}/510</code>",
        ])
        
        return "\n".join(lines)

    def trade_confirm_text(self, trade: TradeSession, requester_pokemon, target_pokemon) -> str:
        lines = [
            "<b>TRADE CONFIRMATION</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"<b>Status:</b> {html.escape(self.trade_status_text(trade))}",
            "",
            self.trade_pokemon_summary_block(trade.target_name, target_pokemon),
            "",
            self.trade_pokemon_summary_block(trade.requester_name, requester_pokemon),
            "",
            "<b>Acceptance</b>",
            f"{html.escape(trade.requester_name)}: {'Accepted' if trade.requester_id in trade.accepted_by else 'Waiting'}",
            f"{html.escape(trade.target_name)}: {'Accepted' if trade.target_id in trade.accepted_by else 'Waiting'}",
            "",
            "Use Change Offer if you want to pick a different Pokemon.",
        ]
        return "\n".join(lines)

    def trade_confirm_buttons(self, trade: TradeSession) -> list[list[Button]]:
        return [
            [
                Button.inline("Accept", data=f"trade:{trade.trade_id}:confirm".encode("utf-8")),
                Button.inline("Change Offer", data=f"trade:{trade.trade_id}:revise".encode("utf-8")),
            ],
            [Button.inline("Cancel Trade", data=f"trade:{trade.trade_id}:decline".encode("utf-8"))],
        ]

    async def _edit_or_respond_trade(self, event: CallbackQuery.Event, text: str, *, buttons=None) -> None:
        edited = await safe_event_edit(event, text, buttons=buttons, parse_mode="html")
        if edited:
            return
        await event.respond(text, buttons=buttons, parse_mode="html")

    async def _show_trade_picker(
        self,
        event: CallbackQuery.Event,
        trade: TradeSession,
        *,
        user_id: int,
        page: int,
    ) -> bool:
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.get_by_telegram_user_id(user_id)
            if trainer is None:
                return False
            items, total, current_page = self.trade_pokemon_page(trainer, pokemons, page=page)
            if total <= 0:
                return False
            await self._edit_or_respond_trade(
                event,
                self.trade_picker_text(
                    trade,
                    trainer,
                    user_id=user_id,
                    target_name=self.trade_other_name(trade, user_id),
                    page=current_page,
                    total=total,
                    items=items,
                ),
                buttons=self.trade_picker_buttons(
                    trade,
                    user_id=user_id,
                    page=current_page,
                    total=total,
                    items=items,
                ),
            )
        return True

    async def _show_trade_confirm(self, event: CallbackQuery.Event, trade: TradeSession) -> bool:
        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            requester = trainers.get_by_telegram_user_id(trade.requester_id)
            target = trainers.get_by_telegram_user_id(trade.target_id)
            if requester is None or target is None:
                return False
            requester_pokemon_id = trade.selected_by.get(trade.requester_id)
            target_pokemon_id = trade.selected_by.get(trade.target_id)
            if requester_pokemon_id is None or target_pokemon_id is None:
                return False
            requester_pokemon = pokemons.get_owned_pokemon(requester, requester_pokemon_id)
            target_pokemon = pokemons.get_owned_pokemon(target, target_pokemon_id)
            if requester_pokemon is None or target_pokemon is None:
                return False
            await self._edit_or_respond_trade(
                event,
                self.trade_confirm_text(trade, requester_pokemon, target_pokemon),
                buttons=self.trade_confirm_buttons(trade),
            )
        return True

    async def on_trade(self, event: NewMessage.Event) -> None:
        if event.is_private:
            await event.respond("Use /trade in a group chat as a reply to another player.")
            return
        if not event.is_reply:
            await event.respond("Reply to another player's message with /trade.")
            return

        reply_message = await event.get_reply_message()
        if reply_message is None:
            await event.respond("Reply to another player's message with /trade.")
            return

        target_user = await reply_message.get_sender()
        if not isinstance(target_user, User) or getattr(target_user, "bot", False):
            await event.respond("You can only trade with another player.")
            return
        if target_user.id == event.sender_id:
            await event.respond("You cannot trade with yourself.")
            return

        requester_reason = self._trade_lock_reason(event.sender_id)
        if requester_reason:
            await event.respond(requester_reason)
            return
        target_reason = self._trade_lock_reason(target_user.id)
        if target_reason:
            await event.respond(f"{display_name(target_user)} is busy right now. {target_reason}")
            return

        requester_user = await event.get_sender()
        requester_name = display_name(requester_user)
        target_name = display_name(target_user)

        with db_session() as session:
            trainers = TrainerRepository(session)
            pokemons = PokemonRepository(session)
            requester = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(requester_user, "username", None),
                display_name=requester_name,
            )
            target = trainers.ensure_trainer(
                telegram_user_id=target_user.id,
                username=getattr(target_user, "username", None),
                display_name=target_name,
            )
            requester_pool = self.tradable_owned_pokemon(requester, pokemons)
            target_pool = self.tradable_owned_pokemon(target, pokemons)
            if not requester_pool:
                await event.respond("You do not have any tradable Pokemon right now.")
                return
            if not target_pool:
                await event.respond(f"{target_name} does not have any tradable Pokemon right now.")
                return

        trade = TradeSession(
            trade_id=secrets.token_hex(4),
            chat_id=event.chat_id,
            public_message_id=0,
            requester_id=event.sender_id,
            requester_name=requester_name,
            target_id=target_user.id,
            target_name=target_name,
            selected_by={
                event.sender_id: None,
                target_user.id: None,
            },
        )
        message = await event.respond(
            self.trade_request_text(trade),
            buttons=self.trade_request_buttons(trade),
            parse_mode="html",
        )
        trade.public_message_id = message.id
        self._register_trade(trade)

    async def handle_trade_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Unknown trade action.", alert=True)
            return

        trade = self.trade_sessions.get(parts[1])
        if trade is None:
            await event.answer("That trade is no longer active.", alert=True)
            return
        if event.sender_id not in {trade.requester_id, trade.target_id}:
            await event.answer("That trade belongs to another pair of players.", alert=True)
            return
        async with trade.lock:
            action = parts[2]
            if action == "noop":
                await event.answer("Choose a button below.")
                return

            if action == "decline":
                trade.state = "cancelled" if event.sender_id == trade.requester_id else "declined"
                self._release_trade(trade)
                result_text = "Trade cancelled." if event.sender_id == trade.requester_id else "Trade declined."
                await self._edit_or_respond_trade(event, result_text, buttons=None)
                await event.answer(result_text)
                return

            if action == "accept":
                if trade.state != "pending":
                    await event.answer("That trade is no longer waiting for accept.", alert=True)
                    return
                if event.sender_id != trade.target_id:
                    await event.answer("Only the invited player can accept this trade.", alert=True)
                    return
                trade.state = "selecting"
                next_picker = self.trade_current_picker_id(trade)
                if next_picker is None or not await self._show_trade_picker(event, trade, user_id=next_picker, page=0):
                    trade.state = "cancelled"
                    self._release_trade(trade)
                    await self._edit_or_respond_trade(event, "Trade cancelled. One player no longer has a tradable Pokemon.", buttons=None)
                    await event.answer("Trade cancelled.", alert=True)
                    return
                await event.answer("Trade accepted.")
                return

            if action == "page":
                if trade.state != "selecting" or len(parts) != 5:
                    await event.answer("That trade is not on the Pokemon picker right now.", alert=True)
                    return
                user_id = int(parts[3])
                page = int(parts[4])
                current_picker = self.trade_current_picker_id(trade)
                if event.sender_id != user_id or current_picker != user_id:
                    await event.answer(f"Waiting for {self.trade_other_name(trade, event.sender_id)} to choose.", alert=True)
                    return
                if not await self._show_trade_picker(event, trade, user_id=user_id, page=page):
                    trade.state = "cancelled"
                    self._release_trade(trade)
                    await self._edit_or_respond_trade(event, "Trade cancelled. No tradable Pokemon are available anymore.", buttons=None)
                    await event.answer("Trade cancelled.", alert=True)
                    return
                await event.answer()
                return

            if action == "pick":
                if trade.state != "selecting" or len(parts) != 6:
                    await event.answer("That trade is not on the Pokemon picker right now.", alert=True)
                    return
                user_id = int(parts[3])
                page = int(parts[4])
                pokemon_id = int(parts[5])
                current_picker = self.trade_current_picker_id(trade)
                if event.sender_id != user_id or current_picker != user_id:
                    await event.answer(f"Waiting for {self.trade_other_name(trade, event.sender_id)} to choose.", alert=True)
                    return

                with db_session() as session:
                    trainers = TrainerRepository(session)
                    pokemons = PokemonRepository(session)
                    trainer = trainers.get_by_telegram_user_id(user_id)
                    if trainer is None:
                        await event.answer("That player could not be loaded.", alert=True)
                        return
                    pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                    if pokemon is None or pokemon.untradeable or has_form_state(pokemon):
                        await event.answer("That Pokemon is no longer tradable.", alert=True)
                        return
                    trade.selected_by[user_id] = pokemon.id
                    trade.accepted_by.clear()

                next_picker = self.trade_current_picker_id(trade)
                if next_picker is not None:
                    if not await self._show_trade_picker(event, trade, user_id=next_picker, page=0):
                        trade.state = "cancelled"
                        self._release_trade(trade)
                        await self._edit_or_respond_trade(event, "Trade cancelled. One player no longer has a tradable Pokemon.", buttons=None)
                        await event.answer("Trade cancelled.", alert=True)
                        return
                    await event.answer("Pokemon selected.")
                    return

                trade.state = "confirming"
                if not await self._show_trade_confirm(event, trade):
                    trade.state = "cancelled"
                    self._release_trade(trade)
                    await self._edit_or_respond_trade(event, "Trade cancelled. One of the selected Pokemon is no longer available.", buttons=None)
                    await event.answer("Trade cancelled.", alert=True)
                    return
                await event.answer("Both offers are ready.")
                return

            if action == "revise":
                if trade.state != "confirming":
                    await event.answer("That trade is not ready to revise yet.", alert=True)
                    return
                trade.selected_by[event.sender_id] = None
                trade.accepted_by.clear()
                trade.state = "selecting"
                if not await self._show_trade_picker(event, trade, user_id=event.sender_id, page=0):
                    trade.state = "cancelled"
                    self._release_trade(trade)
                    await self._edit_or_respond_trade(event, "Trade cancelled. No tradable Pokemon are available anymore.", buttons=None)
                    await event.answer("Trade cancelled.", alert=True)
                    return
                await event.answer("Pick a new Pokemon.")
                return

            if action == "confirm":
                if trade.state != "confirming":
                    await event.answer("That trade is not waiting for confirmation.", alert=True)
                    return
                trade.accepted_by.add(event.sender_id)
                if len(trade.accepted_by) < 2:
                    if not await self._show_trade_confirm(event, trade):
                        trade.state = "cancelled"
                        self._release_trade(trade)
                        await self._edit_or_respond_trade(event, "Trade cancelled. One of the selected Pokemon is no longer available.", buttons=None)
                        await event.answer("Trade cancelled.", alert=True)
                        return
                    await event.answer("Trade accepted. Waiting for the other player.")
                    return

                with db_session() as session:
                    trainers = TrainerRepository(session)
                    pokemons = PokemonRepository(session)
                    requester = trainers.get_by_telegram_user_id(trade.requester_id)
                    target = trainers.get_by_telegram_user_id(trade.target_id)
                    if requester is None or target is None:
                        trade.state = "cancelled"
                        self._release_trade(trade)
                        await self._edit_or_respond_trade(event, "Trade cancelled. One trainer could not be loaded.", buttons=None)
                        await event.answer("Trade cancelled.", alert=True)
                        return

                    requester_pokemon_id = trade.selected_by.get(trade.requester_id)
                    target_pokemon_id = trade.selected_by.get(trade.target_id)
                    requester_pokemon = pokemons.get_owned_pokemon(requester, int(requester_pokemon_id or 0))
                    target_pokemon = pokemons.get_owned_pokemon(target, int(target_pokemon_id or 0))
                    if (
                        requester_pokemon is None
                        or target_pokemon is None
                        or requester_pokemon.untradeable
                        or target_pokemon.untradeable
                        or has_form_state(requester_pokemon)
                        or has_form_state(target_pokemon)
                    ):
                        trade.state = "cancelled"
                        self._release_trade(trade)
                        await self._edit_or_respond_trade(event, "Trade cancelled. One selected Pokemon is no longer tradable.", buttons=None)
                        await event.answer("Trade cancelled.", alert=True)
                        return

                    requester_species = requester_pokemon.species
                    target_species = target_pokemon.species
                    pokemons.swap_trade_ownership(
                        requester_pokemon,
                        target_pokemon,
                        first_new_trainer=target,
                        second_new_trainer=requester,
                        trainers=trainers,
                    )

                trade.state = "completed"
                self._release_trade(trade)
                await self._edit_or_respond_trade(
                    event,
                    (
                        "Trade complete.\n"
                        f"{trade.requester_name} traded {requester_species} for {target_species} from {trade.target_name}."
                    ),
                    buttons=None,
                )
                await event.answer("Trade completed.")
                return

            await event.answer("Unknown trade action.", alert=True)

    async def on_clear_db(self, event: NewMessage.Event) -> None:
        if self.battle_service.battles_by_id or self.battle_service.pending_by_id or self.encounters.active_by_user:
            await event.respond("Finish active battles and encounters before clearing the database.")
            return
        await event.respond(
            "Clear the local test database?\nThis will remove trainers, teams, and owned Pokemon.",
            buttons=[
                [
                    Button.inline("Confirm Clear", data="cleardb:confirm".encode("utf-8")),
                    Button.inline("Cancel", data="cleardb:cancel".encode("utf-8")),
                ]
            ],
        )

    async def on_private_text(self, event: NewMessage.Event) -> None:
        if await self._is_banned_user_id(event.sender_id):
            return
        await self.factions.track_sender(event)
        nickname_session = self._get_nickname_session(int(event.sender_id or 0))
        if nickname_session is not None:
            sender = await resolve_event_user(event)
            success, text = await run_db_work_async(lambda session: self._apply_nickname_from_session(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                nickname_session=nickname_session,
                nickname=str(event.raw_text or ""),
            ))
            if success:
                self._clear_nickname_session(int(event.sender_id or 0))
            await event.respond(text, parse_mode="md")
            return
        await self.team_manager.on_private_text(event)

    async def on_any_message(self, event: NewMessage.Event) -> None:
        await self.factions.track_sender(event)

    async def on_create(self, event: NewMessage.Event) -> None:
        await self.factions.on_create(event)

    async def on_deletefac(self, event: NewMessage.Event) -> None:
        await self.factions.on_deletefac(event)

    async def on_setgc(self, event: NewMessage.Event) -> None:
        await self.factions.on_setgc(event)

    async def on_myfac(self, event: NewMessage.Event) -> None:
        await self.factions.on_myfac(event)

    async def on_faclb(self, event: NewMessage.Event) -> None:
        await self.factions.on_faclb(event)

    async def on_join(self, event: NewMessage.Event) -> None:
        await self.factions.on_join(event)

    async def on_leave(self, event: NewMessage.Event) -> None:
        await self.factions.on_leave(event)

    async def on_fac_link(self, event: NewMessage.Event) -> None:
        await self.factions.on_fac_link(event)

    async def on_kick_member(self, event: NewMessage.Event) -> None:
        await self.factions.on_kick_member(event)

    async def on_facpromote(self, event: NewMessage.Event) -> None:
        await self.factions.on_facpromote(event)

    async def on_facdemote(self, event: NewMessage.Event) -> None:
        await self.factions.on_facdemote(event)

    async def on_setpfp(self, event: NewMessage.Event) -> None:
        await self.factions.on_setpfp(event)

    async def on_setname(self, event: NewMessage.Event) -> None:
        await self.factions.on_setname(event)

    async def on_fac_deposit(self, event: NewMessage.Event) -> None:
        await self.factions.on_fac_deposit(event)

    async def on_group_text(self, event: NewMessage.Event) -> None:
        if await self._is_banned_user_id(event.sender_id):
            return
        await self.factions.track_sender(event)
        chat = getattr(event, "chat", None)
        title = str(getattr(chat, "title", "") or "").strip() or None
        await self._track_group_chat(event.chat_id, title=title)
        if event.is_private or not event.raw_text or event.raw_text.startswith("/"):
            return
        chat_id = int(event.chat_id or 0)
        if chat_id == 0:
            return
        game = self.pokechain_games.get(chat_id)
        if game is None or game.status != "active":
            return

        async with game.lock:
            current = self.pokechain_games.get(chat_id)
            if current is None or current is not game or game.status != "active" or not game.players:
                return
            current_player = game.players[game.turn_index]
            if int(event.sender_id or 0) != int(current_player):
                return

            guess = species_key(event.raw_text.strip())
            if not guess:
                return

            if guess not in self._pokechain_allowed_names:
                await event.respond("Invalid Pokemon name. Try again.", parse_mode="html", link_preview=False)
                return
            if guess in game.used_names:
                await event.respond("Already used. Try another.", parse_mode="html", link_preview=False)
                return

            line_id = self._pokechain_line_id(guess)
            if line_id in game.used_lines:
                await event.respond("Evolution line already used. Try another.", parse_mode="html", link_preview=False)
                return

            game.used_names.add(guess)
            game.used_lines.add(line_id)
            game.guess_count += 1
            game.time_per_turn = self._pokechain_turn_seconds(game.guess_count)
            game.turn_index = (game.turn_index + 1) % len(game.players)

            guessed_name = self._pokechain_display_names.get(guess, guess.replace("-", " ").title())
            next_id = game.players[game.turn_index]
            next_name = self._pokechain_mention(game, next_id)
            await self._pokechain_set_turn_timer(game)
            await self._pokechain_send(
                game.chat_id,
                "• <b>Pokechain</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Correct: <b>{html.escape(guessed_name)}</b>\n"
                f"Next: {next_name}\n"
                f"Time: <b>{game.time_per_turn}s</b>",
            )

    async def on_pokechain(self, event: NewMessage.Event) -> None:
        if event.is_private:
            await event.respond("Use this in a group chat.")
            return
        chat_id = int(event.chat_id or 0)
        if chat_id == 0:
            return

        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        sender_id = int(event.sender_id or 0)
        sender_name = display_name(sender)
        args = event.raw_text.split(maxsplit=1)
        action = args[1].strip().lower() if len(args) > 1 else ""

        game = self.pokechain_games.get(chat_id)

        if action in {"end", "stop"}:
            if game is None:
                await event.respond("No active Pokechain game.")
                return
            async with game.lock:
                if sender_id != game.host_id:
                    await event.respond("Only the host can end this Pokechain game.")
                    return
                await self._pokechain_finish_and_cleanup(game)
            await event.respond("Pokechain game ended.")
            return

        if action == "start":
            if game is None:
                await event.respond("No lobby found. Use /pokechain first.")
                return
            async with game.lock:
                if game.status == "active":
                    await event.respond("Pokechain already running.")
                    return
                if sender_id != game.host_id:
                    await event.respond("Only the host can start the game.")
                    return
                await self._pokechain_start_game_locked(game)
            return

        if game is not None:
            await event.respond("Pokechain already running in this chat.")
            return

        game = PokechainSession(
            chat_id=chat_id,
            host_id=sender_id,
            players=[sender_id],
            player_names={sender_id: sender_name},
        )
        self.pokechain_games[chat_id] = game
        await self._pokechain_set_lobby_timer(game)
        await event.respond(
            "• <b>Pokechain Lobby</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Host: {self._pokechain_mention(game, sender_id)}\n"
            "Join: <code>/joinpc</code>\n"
            "Start: <code>/pokechain start</code>\n"
            "Auto-start: <b>120s</b>",
            parse_mode="html",
            link_preview=False,
        )

    async def on_joinpc(self, event: NewMessage.Event) -> None:
        if event.is_private:
            await event.respond("Use this in a group chat.")
            return
        chat_id = int(event.chat_id or 0)
        if chat_id == 0:
            return
        game = self.pokechain_games.get(chat_id)
        if game is None:
            await event.respond("No Pokechain lobby. Use /pokechain first.")
            return

        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        sender_id = int(event.sender_id or 0)
        sender_name = display_name(sender)

        async with game.lock:
            current = self.pokechain_games.get(chat_id)
            if current is None or current is not game:
                await event.respond("No Pokechain lobby. Use /pokechain first.")
                return
            if game.status != "lobby":
                await event.respond("Pokechain already started.")
                return
            if sender_id in game.players:
                await event.respond("You already joined.")
                return
            game.players.append(sender_id)
            game.player_names[sender_id] = sender_name
            player_count = len(game.players)
        await event.respond(
            "• <b>Pokechain Lobby</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{self._pokechain_mention(game, sender_id)} joined.\n"
            f"Players: <b>{player_count}</b>",
            parse_mode="html",
            link_preview=False,
        )

    async def handle_cardsetup_callback(self, event: CallbackQuery.Event, data: str) -> None:
        action = data.split(":")[1]
        
        if action == "start":
            from telethon import Button
            from bot.config import PROJECT_DIR
            img_path = PROJECT_DIR / "assets" / "lookselection.webp"
            buttons = [
                [Button.inline("1", b"cardsetup:gloria"), Button.inline("2", b"cardsetup:victor")]
            ]
            try:
                await event.delete()
            except Exception:
                pass
            await event.respond(
                "How do you look yourself",
                file=img_path,
                buttons=buttons
            )
            return
            
        if action in ("gloria", "victor"):
            with db_session() as session:
                trainers = TrainerRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(await event.get_sender(), "username", None) if event.sender_id else None,
                    display_name=display_name(await event.get_sender()) if event.sender_id else "Trainer",
                )
                trainer.gender = action
            try:
                await event.delete()
            except Exception:
                pass
            await event.respond("Look updated! You can now use /mycard.")
            return

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        if await self.factions.handle_callback(event):
            return True
        if await self.team_manager.handle_callback(event):
            return True
        if await self.encounters.handle_callback(event):
            return True
        if await self.stats.handle_callback(event):
            return True

        data = event.data.decode("utf-8")
        if data.startswith("help:cat:"):
            category_key = data.split(":", 2)[2]
            await self._show_help_menu(event, category_key=category_key, edit=True)
            await event.answer()
            return True
        if data.startswith("bp:"):
            await self.handle_battlepass_cb(event, data)
            return True
        if data.startswith("cardsetup:"):
            await self.handle_cardsetup_callback(event, data)
            return True
        if data.startswith("cleardb:"):
            await self.handle_clear_db_callback(event, data)
            return True
        if data.startswith("bag:"):
            await self.handle_bag_callback(event, data)
            return True
        if data.startswith("box:"):
            await self.handle_box_callback(event, data)
            return True
        if data.startswith("shop:"):
            await self.handle_shop_callback(event, data)
            return True
        if data.startswith("form:"):
            await self.handle_formchange_callback(event, data)
            return True
        if data.startswith("train:"):
            await self.handle_train_callback(event, data)
            return True
        if data.startswith("dmcmd:"):
            await self._handle_dm_command_callback(event, data)
            return True
        if await self.daycare.handle_callback(event):
            return True
        if data.startswith("equip:"):
            await self.handle_equip_callback(event, data)
            return True
        if data.startswith("itemuse:"):
            await self.handle_item_use_callback(event, data)
            return True
        if data.startswith("useact:"):
            await self.handle_use_action_callback(event, data)
            return True
        if data.startswith("relearn:"):
            await self.handle_relearner_callback(event, data)
            return True
        if data.startswith("cmduse:"):
            await self.handle_command_use_callback(event, data)
            return True
        if data.startswith("nick:"):
            await self.handle_nickname_callback(event, data)
            return True
        if data.startswith("tmuse:"):
            await self.handle_tm_callback(event, data)
            return True
        if data.startswith("plist:"):
            await self.handle_pokemon_list_callback(event, data)
            return True
        if data.startswith("displaypref:"):
            await self.handle_display_callback(event, data)
            return True
        if data.startswith("sortpref:"):
            await self.handle_sort_callback(event, data)
            return True
        if data.startswith("trade:"):
            await self.handle_trade_callback(event, data)
            return True
        if not data.startswith("starter:"):
            return False
        if not await event.get_sender():
            await event.answer("Missing sender.", alert=True)
            return True
        sender = await event.get_sender()

        if data == "starter:regions":
            await safe_event_edit(
                event,
                "Choose a region first.",
                buttons=self.region_buttons(),
            )
            await event.answer()
            return True

        parts = data.split(":")
        action = parts[1]

        if action == "region" and len(parts) == 3:
            region_id = parts[2]
            await safe_event_edit(
                event,
                f"🌍 **{self._region_label(region_id)} Region Starters**\n\n"
                "Excellent! Here are the rare Pokémon available in this region.\n"
                "Choose one to view its details.",
                buttons=self.starter_buttons(region_id),
                parse_mode="md",  # <-- Add this!
            )
            await event.answer()
            return True

        if action == "pick" and len(parts) == 4:
            region_id = parts[2]
            species = parts[3]
            
            # Fetch the typing and emojis using your existing data service!
            types = self.pokemon_data.formatted_types(species)
            
            preview_text = (
                f"🔍 **Starter Preview: {species}**\n"
                f"**Type:** {types}\n\n"
                f"🌍 **Origin:** {self._region_label(region_id)} Region\n\n"
                "⚠️ **Think carefully! This choice is permanent, and your starter cannot be traded or released.**"
            )
            
            await safe_event_edit(
                event,
                preview_text,
                buttons=self.starter_confirm_buttons(region_id, species),
                parse_mode="md",
            )
            await event.answer()
            return True

        if action == "confirm" and len(parts) == 4:
            region_id = parts[2]
            species = parts[3]
            with db_session() as session:
                trainers = TrainerRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                if trainer.starter_species:
                    await event.answer("You already selected your starter.", alert=True)
                    return True

            generated = await self.generator.generate_starter(species=species, region=region_id)
            with db_session() as session:
                trainers = TrainerRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                if trainer.starter_species:
                    await event.answer("You already selected your starter.", alert=True)
                    return True
                trainers.set_region(trainer, region_id)
                trainers.set_starter_species(trainer, species)
                owned = pokemons.create_owned_pokemon(trainer=trainer, data=generated)
                trainers.place_in_first_party_slot(trainer, owned)

            await safe_event_edit(
                event,
                f"🎉 **Starter Claimed!** 🎉\n\n"
                f"**{species}** has joined your party!\n"
                f"• Level: {generated['level']}\n"
                f"• Nature: {generated['nature']}\n"
                f"• Ability: {generated['ability']}\n"
                f"• Moves: {', '.join(generated['moves'])}\n"
                f"• IV Total: {generated['total_iv']}\n\n"
                "Use `/mypokemons` and `/myteam` in DM to manage your collection. "
                "When you are ready, use `/travel` to pick a region and area, or `/dexnav <pokemon>` to search for a species!",
                buttons=None,
                parse_mode="md",
            )
            await event.answer("Starter selected.")
            return True

        await event.answer("Unknown starter action.", alert=True)
        return True

    async def handle_bag_callback(self, event: CallbackQuery.Event, data: str) -> None:
        if data == "bag:noop":
            await event.answer()
            return
            
        parts = data.split(":")
        if len(parts) < 2:
            return
            
        category = parts[1]
        # Safely get the page number from the callback data
        page = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
        
        sender = await event.get_sender()
        bag_text, current_page, max_page = await run_db_work_async(lambda session: self._bag_category_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            category=category,
            page=page,
        ))
            
        await safe_event_edit(
            event,
            bag_text,
            buttons=self.bag_buttons(category, page=current_page, max_page=max_page),
            parse_mode="md"
        )
        await event.answer()

    async def handle_box_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 2:
            await event.answer("Unknown box action.", alert=True)
            return
        action = parts[1]
        # Callback data format: box:<action>:<owner_id>[:<value>]
        owner_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        if owner_id and int(event.sender_id or 0) != owner_id:
            await event.answer("This box menu is not yours.", alert=True)
            return
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            owned = inventories.key_item_count(trainer, KEY_ITEM_TRAINER_BOX)

            if action == "cancel":
                self.box_selection_by_user.pop(event.sender_id, None)
                await safe_event_edit(event, "Trainer Box menu closed.", buttons=None)
                await event.answer("Closed.")
                return

            if action == "set":
                if len(parts) < 4 or not parts[3].lstrip("+-").isdigit():
                    await event.answer("Invalid amount.", alert=True)
                    return
                selected = max(1, int(parts[3]))
                selected = min(selected, max(1, owned))
                self.box_selection_by_user[event.sender_id] = selected
                if owned <= 0:
                    await safe_event_edit(event, "You don't have any Trainer Box right now.", buttons=None)
                    await event.answer("No boxes.")
                    return
                await safe_event_edit(
                    event,
                    self._box_menu_text(owned=owned, selected=selected),
                    buttons=self._box_menu_buttons(owner_id=owner_id or event.sender_id, selected=selected, owned=owned),
                    parse_mode="md",
                )
                await event.answer()
                return

            if action == "open":
                if owned <= 0:
                    await safe_event_edit(event, "You don't have any Trainer Box right now.", buttons=None)
                    await event.answer("No boxes.")
                    return
                requested = self.box_selection_by_user.get(event.sender_id, 1)
                if len(parts) >= 4 and parts[3].isdigit():
                    requested = int(parts[3])
                qty = min(max(1, requested), owned)
                opened, rewards = await self._open_trainer_boxes(trainer, inventories, qty)
                remaining = inventories.key_item_count(trainer, KEY_ITEM_TRAINER_BOX)
                self.box_selection_by_user.pop(event.sender_id, None)

        if action != "open":
            await event.answer("Unknown box action.", alert=True)
            return
        if opened <= 0:
            await safe_event_edit(event, "No Trainer Box were opened.", buttons=None)
            await event.answer("No boxes opened.")
            return

        lines = [f"📦 Opened `{opened}` Trainer Box", "━━━━━━━━━━━━━━━━━━━━━━", "**Rewards**"]
        for item_name in sorted(rewards.keys()):
            amount = rewards[item_name]
            if amount == 1:
                lines.append(f"• {item_name}")
            else:
                lines.append(f"• {item_name} x{amount}")
        lines.append("")
        lines.append(f"Remaining Trainer Box: `{remaining}`")
        await safe_event_edit(event, "\n".join(lines), buttons=None, parse_mode="md")
        await event.answer("Opened.")

    async def handle_shop_callback(self, event: CallbackQuery.Event, data: str) -> None:
        sender = await event.get_sender()
        if data == "shop:noop":
            await event.answer()
            return
        parts = data.split(":")
        if len(parts) == 4 and parts[1] == "buy" and parts[2] == "battle":
            await self._buy_battle_shop_callback(event, parts[3])
            return
        if len(parts) not in {3, 4} or parts[1] != "page":
            await event.answer("Unknown shop action.", alert=True)
            return
        category = parts[2]
        try:
            page = int(parts[3]) if len(parts) == 4 else 0
        except (TypeError, ValueError):
            await event.answer("Unknown shop page.", alert=True)
            return
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            shop_text, current_page, max_page = await self.shop_text(trainer, inventories, category=category, page=page)
        await safe_event_edit(
            event,
            shop_text,
            buttons=self.shop_buttons(category, page=current_page, max_page=max_page, trainer=trainer),
            parse_mode="md",
        )
        await event.answer()

    async def handle_formchange_callback(self, event: CallbackQuery.Event, data: str) -> None:
        sender = await event.get_sender()
        if data == "form:noop":
            await event.answer()
            return
        if data == "form:items":
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                owned_items = self._formchange_owned_items(inventories, trainer)
            await safe_event_edit(
                event,
                "Form Change\n\nChoose a key item to use.",
                buttons=self._formchange_item_buttons(owned_items),
            )
            await event.answer()
            return

        parts = data.split(":")
        if len(parts) >= 3 and parts[1] == "item":
            await self._show_formchange_host_menu(event, item_name_key=parts[2], page=0)
            await event.answer()
            return
        if len(parts) == 5 and parts[1] == "page" and parts[2] == "pickhost":
            await self._show_formchange_host_menu(event, item_name_key=parts[3], page=int(parts[4]))
            await event.answer()
            return
        if len(parts) == 6 and parts[1] == "page" and parts[2] == "pickpartner":
            await self._show_formchange_partner_menu(event, item_name_key=parts[3], host_id=int(parts[4]), page=int(parts[5]))
            await event.answer()
            return
        if len(parts) == 4 and parts[1] == "pickhost":
            item_name_key = parts[2]
            pokemon_id = int(parts[3])
            with db_session() as session:
                trainers = TrainerRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=event.sender_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                current_item = active_item_key(pokemon) if pokemon is not None else None
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if item_requires_partner(item_name_key):
                if current_item == item_name_key and has_form_state(pokemon):
                    await self._remove_fusion(event, item_name_key=item_name_key, pokemon_id=pokemon_id)
                    return
                await self._show_formchange_partner_menu(event, item_name_key=item_name_key, host_id=pokemon_id, page=0)
                await event.answer()
                return
            if item_form_targets(item_name_key):
                await self._show_formchange_form_menu(event, item_name_key=item_name_key, pokemon_id=pokemon_id)
                await event.answer()
                return
            target_species = toggle_result_species(item_name_key, effective_species(pokemon))
            if target_species is None:
                await event.answer("That item cannot change this Pokemon right now.", alert=True)
                return
            await self._apply_form_species_change(
                event,
                item_name_key=item_name_key,
                pokemon_id=pokemon_id,
                target_species=target_species,
            )
            return
        if len(parts) == 5 and parts[1] == "pickpartner":
            await self._apply_fusion(
                event,
                item_name_key=parts[2],
                host_id=int(parts[3]),
                partner_id=int(parts[4]),
            )
            return
        if len(parts) == 5 and parts[1] == "choose":
            targets = item_form_targets(parts[2])
            try:
                target_species = targets[int(parts[4])]
            except (IndexError, TypeError, ValueError):
                await event.answer("That form option is no longer available.", alert=True)
                return
            await self._apply_form_species_change(
                event,
                item_name_key=parts[2],
                pokemon_id=int(parts[3]),
                target_species=target_species,
            )
            return

        await event.answer("Unknown form-change action.", alert=True)

    async def _buy_battle_shop_callback(self, event: CallbackQuery.Event, purchase_key: str) -> None:
        sender = await event.get_sender()
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=event.sender_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            offer = self._battle_shop_offer_by_purchase_key(int(trainer.telegram_user_id), purchase_key)
            if offer is None:
                await event.answer("That weekly battle item is no longer available.", alert=True)
                return
            if self._battle_shop_is_purchased(trainer, offer):
                await event.answer("You already bought that battle item this week.", alert=True)
                return
            cost = int(offer.get("price") or 0)
            if not inventories.consume_league_points(trainer, cost):
                await event.answer(
                    f"Not enough LP. You need {cost:,} LP but only have {getattr(trainer.inventory, 'league_points', 0):,} LP.",
                    alert=True,
                )
                return

            kind = str(offer.get("kind") or "")
            if kind == "tm":
                add_action = ("tm", str(offer["name"]), int(offer.get("amount") or 1))
            else:
                add_action = ("item", str(offer["name"]), int(offer.get("amount") or 1))
            purchased_qty = self._apply_shop_add_action(inventories, trainer, add_action, 1)
            self._mark_battle_shop_purchased(trainer, offer)
            shop_text, current_page, max_page = await self.shop_text(trainer, inventories, category="battle", page=0)
            remaining_lp = int(getattr(trainer.inventory, "league_points", 0) or 0)

        await safe_event_edit(
            event,
            shop_text,
            buttons=self.shop_buttons("battle", page=current_page, max_page=max_page, trainer=trainer),
            parse_mode="md",
        )
        await event.answer(f"Purchased {purchased_qty}x {offer['display_name']}. LP left: {remaining_lp:,}")

    async def handle_equip_callback(self, event: CallbackQuery.Event, data: str) -> None:
        if not event.is_private:
            await event.answer("Use item equip in private chat.", alert=True)
            return
        parts = data.split(":")
        if len(parts) < 2:
            await event.answer("Unknown equip action.", alert=True)
            return

        action = parts[1]
        if action == "noop":
            await event.answer("Choose a button below.")
            return
        if action == "list" and len(parts) == 2:
            await self._show_equip_item_menu(event, category_key=None, page=0, edit=True)
            await event.answer()
            return
        if action == "list" and len(parts) == 4:
            await self._show_equip_item_menu(event, category_key=parts[2], page=int(parts[3]), edit=True)
            await event.answer()
            return
        if action == "item" and len(parts) == 5:
            await self._show_equip_pokemon_menu(
                event,
                category_key=parts[2],
                item_key=parts[4],
                item_page=int(parts[3]),
                page=0,
                edit=True,
            )
            await event.answer()
            return
        if action == "pokemon" and len(parts) == 6:
            await self._show_equip_pokemon_menu(
                event,
                category_key=parts[2],
                item_key=parts[4],
                item_page=int(parts[3]),
                page=int(parts[5]),
                edit=True,
            )
            await event.answer()
            return
        if action == "pick" and len(parts) == 7:
            await self._show_equip_confirm_menu(
                event,
                category_key=parts[2],
                item_key=parts[4],
                item_page=int(parts[3]),
                pokemon_page=int(parts[5]),
                pokemon_id=int(parts[6]),
            )
            await event.answer()
            return
        if action == "confirm" and len(parts) == 7:
            await self._confirm_equip_item(
                event,
                item_key=parts[4],
                pokemon_id=int(parts[6]),
            )
            return
        await event.answer("Unknown equip action.", alert=True)

    async def handle_item_use_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 4:
            await event.answer("Unknown item action.", alert=True)
            return

        action = parts[1]
        if action == "pickpokemon" and len(parts) == 4:
            await self._show_item_use_menu(event, action=parts[2], pokemon_id=int(parts[3]), edit=True)
            await event.answer()
            return
        if action == "item" and len(parts) == 5:
            use_kind = parts[2]
            pokemon_id = int(parts[3])
            medicine_key = parts[4]
            if medicine_key not in self._medicine_keys_for_action(use_kind):
                await event.answer("That item is no longer available.", alert=True)
                return
            if self._item_requires_stat_choice(medicine_key):
                await self._show_item_use_stat_menu(
                    event,
                    action=use_kind,
                    pokemon_id=pokemon_id,
                    medicine_key=medicine_key,
                    edit=True,
                )
            else:
                await self._show_item_use_amount_menu(
                    event,
                    action=use_kind,
                    pokemon_id=pokemon_id,
                    medicine_key=medicine_key,
                    edit=True,
                )
            await event.answer()
            return
        if action == "stat" and len(parts) == 6:
            use_kind = parts[2]
            pokemon_id = int(parts[3])
            medicine_key = parts[4]
            stat_key = parts[5]
            if medicine_key not in self._medicine_keys_for_action(use_kind) or stat_key not in EV_STAT_LABELS:
                await event.answer("That item is no longer available.", alert=True)
                return
            await self._show_item_use_amount_menu(
                event,
                action=use_kind,
                pokemon_id=pokemon_id,
                medicine_key=medicine_key,
                stat_key=stat_key,
                edit=True,
            )
            await event.answer()
            return
        if action == "apply" and len(parts) in {6, 7}:
            use_kind = parts[2]
            pokemon_id = int(parts[3])
            medicine_key = parts[4]
            if medicine_key not in self._medicine_keys_for_action(use_kind):
                await event.answer("That item is no longer available.", alert=True)
                return
            stat_key: str | None = None
            amount_text = parts[5]
            if len(parts) == 7:
                stat_key = parts[5]
                amount_text = parts[6]
                if stat_key not in EV_STAT_LABELS:
                    await event.answer("Choose a valid stat.", alert=True)
                    return
            if not amount_text.isdigit() or int(amount_text) <= 0:
                await event.answer("Choose a valid amount.", alert=True)
                return
            amount = int(amount_text)
            if use_kind == "mochi":
                await self._apply_mochi(event, pokemon_id, medicine_key, amount=amount, stat_key=stat_key)
                return
            if use_kind == "feather":
                await self._apply_feather(event, pokemon_id, medicine_key, amount=amount, stat_key=stat_key)
                return
            if use_kind == "candy":
                await self._apply_candy(event, pokemon_id, medicine_key, amount=amount)
                return
        await event.answer("Unknown item action.", alert=True)

    async def handle_command_use_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 2:
            await event.answer("Unknown action.", alert=True)
            return
        action = parts[1]
        if action == "page":
            if len(parts) != 4:
                await event.answer("Unknown action.", alert=True)
                return
            command_session = self._get_command_use_session(
                owner_id=int(event.sender_id or 0),
                session_id=parts[2],
            )
            if command_session is None:
                await event.answer("This menu expired. Use the command again.", alert=True)
                return
            page = int(parts[3]) if parts[3].lstrip("+-").isdigit() else 0
            await self._show_command_use_picker_menu(
                event,
                command_session=command_session,
                page=page,
                edit=True,
            )
            await event.answer()
            return
        if action == "cancel":
            if len(parts) != 3:
                await event.answer("Unknown action.", alert=True)
                return
            command_session = self._get_command_use_session(
                owner_id=int(event.sender_id or 0),
                session_id=parts[2],
            )
            self.command_use_sessions.pop(parts[2], None)
            closed_text = (
                f"{self._command_use_item_label(command_session.action, requested_nature=command_session.requested_nature)} menu closed."
                if command_session is not None
                else "Menu expired."
            )
            edited = await safe_event_edit(event, closed_text, buttons=None)
            if not edited:
                await event.respond(closed_text)
            await event.answer("Closed.")
            return
        if action == "pick":
            if len(parts) != 4 or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            command_session = self._get_command_use_session(
                owner_id=int(event.sender_id or 0),
                session_id=parts[2],
            )
            if command_session is None:
                await event.answer("This menu expired. Use the command again.", alert=True)
                return
            pokemon_id = int(parts[3])
            self.command_use_sessions.pop(command_session.session_id, None)
            if command_session.action == "mint" and command_session.requested_nature is not None:
                busy_reason = self._pokemon_change_lock_reason(event.sender_id)
                if busy_reason:
                    await event.answer(busy_reason, alert=True)
                    return
                sender = await resolve_event_user(event)
                with db_session() as session:
                    trainers = TrainerRepository(session)
                    inventories = InventoryRepository(session)
                    pokemons = PokemonRepository(session)
                    trainer = trainers.ensure_trainer(
                        telegram_user_id=event.sender_id,
                        username=getattr(sender, "username", None),
                        display_name=display_name(sender),
                    )
                    pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                    if pokemon is None:
                        await event.answer("That Pokemon is no longer available.", alert=True)
                        return
                    options = self._mint_options_for_pokemon(
                        inventories,
                        trainer,
                        pokemon,
                        requested_nature=command_session.requested_nature,
                    )
                    if not options:
                        await event.answer(
                            self._command_use_unavailable_text(
                                "mint",
                                requested_nature=command_session.requested_nature,
                                pokemon_name=pokemon.species,
                            ),
                            alert=True,
                        )
                        return
                    success, result_text = self._apply_mint_option(
                        inventories,
                        pokemons,
                        trainer,
                        pokemon,
                        options[0],
                    )
                if not success:
                    await event.answer(result_text, alert=True)
                    return
                edited = await safe_event_edit(event, result_text, buttons=None)
                if not edited:
                    await event.respond(result_text)
                await event.answer("Mint used.")
                return
            await self._show_command_use_target_menu(
                event,
                action=command_session.action,
                pokemon_id=pokemon_id,
                edit=True,
            )
            await event.answer()
            return
        if action == "close":
            if len(parts) != 3 or not parts[2].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            edited = await safe_event_edit(event, "Menu closed.", buttons=None)
            if not edited:
                await event.respond("Menu closed.")
            await event.answer("Closed.")
            return
        if action == "mint":
            if len(parts) != 5 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            pokemon_id = int(parts[3])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            busy_reason = self._pokemon_change_lock_reason(event.sender_id)
            if busy_reason:
                await event.answer(busy_reason, alert=True)
                return
            nature_key = parts[4]
            sender = await resolve_event_user(event)
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                options = [
                    entry
                    for entry in self._mint_options_for_pokemon(inventories, trainer, pokemon)
                    if normalize_lookup(str(entry.get("nature") or "")) == nature_key
                ]
                if not options:
                    await event.answer(
                        self._command_use_unavailable_text("mint", pokemon_name=pokemon.species),
                        alert=True,
                    )
                    return
                success, result_text = self._apply_mint_option(
                    inventories,
                    pokemons,
                    trainer,
                    pokemon,
                    options[0],
                )
            if not success:
                await event.answer(result_text, alert=True)
                return
            edited = await safe_event_edit(event, result_text, buttons=None)
            if not edited:
                await event.respond(result_text)
            await event.answer("Mint used.")
            return
        if action == "abil":
            if len(parts) != 6 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            pokemon_id = int(parts[3])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            busy_reason = self._pokemon_change_lock_reason(event.sender_id)
            if busy_reason:
                await event.answer(busy_reason, alert=True)
                return
            required_item_key = parts[4]
            ability_key = parts[5]
            required_item = ABILITY_PATCH_ITEM if required_item_key == normalize_lookup(ABILITY_PATCH_ITEM) else ABILITY_CAPSULE_ITEM
            sender = await resolve_event_user(event)
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                options = await self._ability_options_for_pokemon(
                    inventories,
                    trainer,
                    pokemon,
                    required_item=required_item,
                )
                selected = next(
                    (
                        entry
                        for entry in options
                        if normalize_lookup(str(entry.get("name") or "")) == ability_key
                    ),
                    None,
                )
                if selected is None:
                    await event.answer(
                        self._command_use_unavailable_text(
                            "abilitypatch" if required_item == ABILITY_PATCH_ITEM else "abilitycapsule",
                            pokemon_name=pokemon.species,
                        ),
                        alert=True,
                    )
                    return
                success, result_text = self._apply_ability_item_option(
                    inventories,
                    pokemons,
                    trainer,
                    pokemon,
                    selected,
                )
            if not success:
                await event.answer(result_text, alert=True)
                return
            edited = await safe_event_edit(event, result_text, buttons=None)
            if not edited:
                await event.respond(result_text)
            await event.answer("Ability updated.")
            return
        if action == "bcap":
            if len(parts) != 5 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            pokemon_id = int(parts[3])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            busy_reason = self._pokemon_change_lock_reason(event.sender_id)
            if busy_reason:
                await event.answer(busy_reason, alert=True)
                return
            stat_key = parts[4]
            sender = await resolve_event_user(event)
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                success, result_text = self._apply_bottlecap_to_stat(
                    inventories,
                    pokemons,
                    trainer,
                    pokemon,
                    stat_key,
                )
            if not success:
                await event.answer(result_text, alert=True)
                return
            edited = await safe_event_edit(event, result_text, buttons=None)
            if not edited:
                await event.respond(result_text)
            await event.answer("Bottle Cap used.")
            return
        if action == "gbcap":
            if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
                await event.answer("Unknown action.", alert=True)
                return
            owner_id = int(parts[2])
            pokemon_id = int(parts[3])
            if int(event.sender_id or 0) != owner_id:
                await event.answer("This menu belongs to another trainer.", alert=True)
                return
            busy_reason = self._pokemon_change_lock_reason(event.sender_id)
            if busy_reason:
                await event.answer(busy_reason, alert=True)
                return
            sender = await resolve_event_user(event)
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                success, result_text = self._apply_goldbottlecap(
                    inventories,
                    pokemons,
                    trainer,
                    pokemon,
                )
            if not success:
                await event.answer(result_text, alert=True)
                return
            edited = await safe_event_edit(event, result_text, buttons=None)
            if not edited:
                await event.respond(result_text)
            await event.answer("Gold Bottle Cap used.")
            return
        await event.answer("Unknown action.", alert=True)

    async def handle_use_action_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 2:
            await event.answer("Unknown action.", alert=True)
            return
        action = parts[1]
        if action == "noop":
            await event.answer()
            return
        if len(parts) < 4:
            await event.answer("Unknown action.", alert=True)
            return
        if not parts[2].isdigit():
            await event.answer("Invalid owner.", alert=True)
            return
        owner_id = int(parts[2])
        if int(event.sender_id or 0) != owner_id:
            await event.answer("This menu belongs to another trainer.", alert=True)
            return
        use_kind = parts[3]
        if use_kind not in {"bottlecap", "goldbottlecap", "maxsoup"}:
            await event.answer("Unknown action.", alert=True)
            return
        if action == "cancel":
            closed_text = f"{self._use_action_item_name(use_kind)} menu closed."
            edited = await safe_event_edit(event, closed_text, buttons=None)
            if not edited:
                await event.respond(closed_text)
            await event.answer("Closed.")
            return
        if action in {"start", "page"}:
            page = int(parts[4]) if action == "page" and len(parts) >= 5 and parts[4].lstrip("+-").isdigit() else 0
            sender = await resolve_event_user(event)
            context = await run_db_work_async(lambda session: self._use_action_callback_context_payload(
                session,
                owner_id=owner_id,
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                action=use_kind,
            ))
            count = int(context.get("count") or 0)
            if count <= 0:
                text = f"You no longer own {self._use_action_item_name(use_kind)}."
                edited = await safe_event_edit(event, text, buttons=None)
                if not edited:
                    await event.respond(text)
                await event.answer("No item found.", alert=True)
                return
            compatible = self._use_action_compatible_pokemon(list(context.get("owned") or []), use_kind)
            page_items, total, current_page = paginate_items(compatible, page=page, per_page=ITEM_USE_PICKER_PAGE_SIZE)
            text = self._use_action_picker_text(
                action=use_kind,
                count=count,
                items=page_items,
                page=current_page,
                total=total,
                display_mode=str(context.get("display_mode") or "none"),
            )
            buttons = self._use_action_picker_buttons(
                owner_id=owner_id,
                action=use_kind,
                page=current_page,
                total=total,
                items=page_items,
            )
            edited = await safe_event_edit(event, text, buttons=buttons)
            if not edited:
                await event.respond(text, buttons=buttons)
            await event.answer()
            return
        if action != "pick" or len(parts) < 5 or not parts[4].isdigit():
            await event.answer("Unknown action.", alert=True)
            return
        busy_reason = self._pokemon_change_lock_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return
        pokemon_id = int(parts[4])
        sender = await resolve_event_user(event)

        if use_kind in {"bottlecap", "goldbottlecap"}:
            result_text: str | None = None
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                if use_kind == "bottlecap":
                    if inventories.held_item_count(trainer, BOTTLE_CAP_ITEM) <= 0:
                        await event.answer("You do not have any Bottle Cap.", alert=True)
                        return
                    candidates = [
                        (int(getattr(pokemon, f"iv_{stat_key}")), stat_key)
                        for stat_key in EV_STAT_ORDER
                        if int(getattr(pokemon, f"iv_{stat_key}")) < 31
                    ]
                    if not candidates:
                        await event.answer(f"{pokemon.species} already has all IVs maxed.", alert=True)
                        return
                    old_value, stat_key = min(candidates, key=lambda entry: entry[0])
                    if not inventories.consume_item(trainer, BOTTLE_CAP_ITEM):
                        await event.answer("You do not have any Bottle Cap anymore.", alert=True)
                        return
                    old_stats = self.pokemon_data.calculate_stats(pokemon)
                    setattr(pokemon, f"iv_{stat_key}", 31)
                    new_stats = self.pokemon_data.calculate_stats(pokemon)
                    self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
                    pokemons.sync_packed_set(pokemon, self.pokemon_data)
                    result_text = (
                        f"{pokemon.species}: {EV_STAT_LABELS[stat_key]} IV {old_value} -> 31 "
                        f"using {BOTTLE_CAP_ITEM}."
                    )
                else:
                    if inventories.held_item_count(trainer, GOLD_BOTTLE_CAP_ITEM) <= 0:
                        await event.answer("You do not have any Gold Bottle Cap.", alert=True)
                        return
                    if all(int(getattr(pokemon, f"iv_{stat_key}")) >= 31 for stat_key in EV_STAT_ORDER):
                        await event.answer(f"{pokemon.species} already has all IVs maxed.", alert=True)
                        return
                    if not inventories.consume_item(trainer, GOLD_BOTTLE_CAP_ITEM):
                        await event.answer("You do not have any Gold Bottle Cap anymore.", alert=True)
                        return
                    old_stats = self.pokemon_data.calculate_stats(pokemon)
                    for stat_key in EV_STAT_ORDER:
                        setattr(pokemon, f"iv_{stat_key}", 31)
                    new_stats = self.pokemon_data.calculate_stats(pokemon)
                    self._refresh_pokemon_hp_after_stat_change(pokemon, old_stats, new_stats)
                    pokemons.sync_packed_set(pokemon, self.pokemon_data)
                    result_text = f"{pokemon.species}: all IVs were maxed using {GOLD_BOTTLE_CAP_ITEM}."
            if not result_text:
                await event.answer("That item use failed.", alert=True)
                return
            edited = await safe_event_edit(event, result_text, buttons=None)
            if not edited:
                await event.respond(result_text)
            await event.answer("Item used.")
            return

        updated_pokemon = None
        with db_session() as session:
            trainers = TrainerRepository(session)
            inventories = InventoryRepository(session)
            pokemons = PokemonRepository(session)
            trainer = trainers.ensure_trainer(
                telegram_user_id=owner_id,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
            )
            if inventories.key_item_count(trainer, KEY_ITEM_MAX_SOUP) <= 0:
                await event.answer("You do not have any Max Soup.", alert=True)
                return
            pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if has_form_state(pokemon):
                await event.answer("Unfuse or reset this Pokemon's form before using Max Soup.", alert=True)
                return
            target_species = self._max_soup_target_species(pokemon.species)
            if species_key(pokemon.species).endswith("-gmax"):
                await event.answer(f"{pokemon.species} is already in its Gmax form.", alert=True)
                return
            if target_species is None:
                await event.answer(f"{pokemon.species} cannot use Max Soup.", alert=True)
                return
            if not inventories.consume_key_item(trainer, KEY_ITEM_MAX_SOUP):
                await event.answer("You do not have any Max Soup.", alert=True)
                return

            generated = await self.generator.generate_pokemon(
                species=target_species,
                level=int(pokemon.level),
                region=str(trainer.current_region),
                source_kind=str(pokemon.source_kind),
                friendship=int(pokemon.friendship),
                shiny=bool(pokemon.shiny),
                item=str(pokemon.item or ""),
                untradeable=bool(pokemon.untradeable),
                unreleasable=bool(pokemon.unreleasable),
                ivs={
                    "hp": int(pokemon.iv_hp),
                    "atk": int(pokemon.iv_atk),
                    "def": int(pokemon.iv_def),
                    "spa": int(pokemon.iv_spa),
                    "spd": int(pokemon.iv_spd),
                    "spe": int(pokemon.iv_spe),
                },
                evs={
                    "hp": int(pokemon.ev_hp),
                    "atk": int(pokemon.ev_atk),
                    "def": int(pokemon.ev_def),
                    "spa": int(pokemon.ev_spa),
                    "spd": int(pokemon.ev_spd),
                    "spe": int(pokemon.ev_spe),
                },
                moves=list(json.loads(pokemon.moves_json)),
                nature=str(pokemon.nature),
                ability=str(pokemon.ability),
                gender=str(pokemon.gender or ""),
                tera_type=str(pokemon.tera_type or ""),
            )
            pokemons.evolve_owned_pokemon(pokemon, generated)
            session.expunge(pokemon)
            updated_pokemon = pokemon

        if updated_pokemon is None:
            await event.answer("Max Soup failed.", alert=True)
            return
        result_text = f"{effective_species(updated_pokemon)} is ready."
        edited = await safe_event_edit(event, result_text, buttons=None)
        if not edited:
            await event.respond(result_text)
        await self.stats.send_stats_card(event, updated_pokemon, page="summary")
        await event.answer("Max Soup used.")

    async def handle_tm_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Unknown TM action.", alert=True)
            return
        action = parts[1]
        if action == "noop":
            await event.answer()
            return
        if not parts[2].isdigit():
            await event.answer("Invalid TM owner.", alert=True)
            return
        owner_id = int(parts[2])
        if int(event.sender_id or 0) != owner_id:
            await event.answer("This TM menu belongs to another trainer.", alert=True)
            return
        if action == "cancel":
            await safe_event_edit(event, "TM menu closed.", buttons=None)
            await event.answer("Closed.")
            return
        if action in {"start", "page"}:
            if len(parts) < 4 or not parts[3].isdigit():
                await event.answer("Invalid TM.", alert=True)
                return
            tm_number = int(parts[3])
            page = int(parts[4]) if action == "page" and len(parts) >= 5 and parts[4].lstrip("+-").isdigit() else 0
            details = self._tm_details(tm_number)
            if details is None:
                await safe_event_edit(event, "Invalid TM. TM does not exist.", buttons=None)
                await event.answer("Invalid TM.", alert=True)
                return
            sender = await resolve_event_user(event)
            context = await run_db_work_async(lambda session: self._tm_callback_context_payload(
                session,
                owner_id=owner_id,
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                tm_number=tm_number,
            ))
            count = int(context.get("count") or 0)
            if count <= 0:
                await safe_event_edit(event, "You dont have this TM.", buttons=None)
                await event.answer("No TM found.", alert=True)
                return
            owned = list(context.get("owned") or [])
            compatible = await self._tm_compatible_pokemon(owned, str(details["move_name"]))
            page_items, total, current_page = paginate_items(compatible, page=page, per_page=TM_COMPAT_PAGE_SIZE)
            text = self._tm_picker_text(
                details=details,
                count=count,
                items=page_items,
                page=current_page,
                total=total,
                display_mode=str(context.get("display_mode") or "none"),
            )
            buttons = self._tm_picker_buttons(
                owner_id=owner_id,
                tm_number=tm_number,
                page=current_page,
                total=total,
                items=page_items,
            )
            edited = await safe_event_edit(event, text, buttons=buttons,parse_mode="md")
            if not edited:
                await event.respond(text, buttons=buttons, parse_mode="md")
            await event.answer()
            return
        if action == "pick":
            if len(parts) < 5 or not parts[3].isdigit() or not parts[4].isdigit():
                await event.answer("Unknown TM action.", alert=True)
                return
            tm_number = int(parts[3])
            pokemon_id = int(parts[4])
            details = self._tm_details(tm_number)
            if details is None:
                await event.answer("Invalid TM.", alert=True)
                return
            sender = await resolve_event_user(event)
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                count = self._tm_count_for_number(inventories, trainer, tm_number)
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is not None:
                    session.expunge(pokemon)
            if count <= 0:
                await event.answer("You dont have this TM.", alert=True)
                return
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not await self._species_can_learn_tm_move(str(pokemon.species), str(details["move_name"])):
                await event.answer(f"{pokemon.species} cannot learn {details['move_name']}.", alert=True)
                return
            moves = [str(move) for move in json.loads(pokemon.moves_json)]
            if any(normalize_lookup(move) == normalize_lookup(str(details["move_name"])) for move in moves):
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.respond(f"{pokemon.species} already knows {details['move_name']}.")
                await event.answer()
                return
            if len(moves) < 4:
                with db_session() as session:
                    trainers = TrainerRepository(session)
                    inventories = InventoryRepository(session)
                    pokemons = PokemonRepository(session)
                    trainer = trainers.ensure_trainer(
                        telegram_user_id=owner_id,
                        username=getattr(sender, "username", None),
                        display_name=display_name(sender),
                    )
                    live_pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                    if live_pokemon is None:
                        await event.answer("That Pokemon is no longer available.", alert=True)
                        return
                    await self._prime_pokemon_move_history(live_pokemon)
                    if not self._consume_tm_by_number(inventories, trainer, tm_number, amount=1):
                        await event.answer("You dont have this TM anymore.", alert=True)
                        return
                    live_moves = [str(move) for move in json.loads(live_pokemon.moves_json)]
                    live_moves.append(str(details["move_name"]))
                    live_pokemon.moves_json = json.dumps(live_moves)
                    record_move_history(live_pokemon, categories=["tm"], move_names=[str(details["move_name"])])
                    pokemons.sync_packed_set(live_pokemon, self.pokemon_data)
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.respond(f"{pokemon.species} learned {details['move_name']}.")
                await event.answer()
                return
            try:
                await event.delete()
            except Exception:
                pass
            await event.respond(
                self._tm_replace_text(pokemon, str(details["move_name"])),
                buttons=self._tm_replace_buttons(owner_id=owner_id, tm_number=tm_number, pokemon_id=pokemon_id),
            )
            await event.answer()
            return
        if action == "replace":
            if len(parts) < 6 or not parts[3].isdigit() or not parts[4].isdigit() or not parts[5].isdigit():
                await event.answer("Unknown TM action.", alert=True)
                return
            tm_number = int(parts[3])
            pokemon_id = int(parts[4])
            slot = int(parts[5])
            if slot < 1 or slot > 4:
                await event.answer("Choose a move slot from 1 to 4.", alert=True)
                return
            details = self._tm_details(tm_number)
            if details is None:
                await event.answer("Invalid TM.", alert=True)
                return
            sender = await resolve_event_user(event)
            with db_session() as session:
                trainers = TrainerRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is not None:
                    session.expunge(pokemon)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if not await self._species_can_learn_tm_move(str(pokemon.species), str(details["move_name"])):
                await event.answer(f"{pokemon.species} cannot learn {details['move_name']}.", alert=True)
                return
            with db_session() as session:
                trainers = TrainerRepository(session)
                inventories = InventoryRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                live_pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if live_pokemon is None:
                    await event.answer("That Pokemon is no longer available.", alert=True)
                    return
                await self._prime_pokemon_move_history(live_pokemon)
                moves = [str(move) for move in json.loads(live_pokemon.moves_json)]
                if len(moves) < 4:
                    await event.answer(f"{live_pokemon.species} has fewer than 4 moves. Use TM again.", alert=True)
                    return
                if not self._consume_tm_by_number(inventories, trainer, tm_number, amount=1):
                    await event.answer("You dont have this TM anymore.", alert=True)
                    return
                old_move = self.pokemon_data._display_move_name(moves[slot - 1])
                moves[slot - 1] = str(details["move_name"])
                live_pokemon.moves_json = json.dumps(moves)
                record_move_history(live_pokemon, categories=["tm"], move_names=[str(details["move_name"])])
                pokemons.sync_packed_set(live_pokemon, self.pokemon_data)
                success_text = f"{live_pokemon.species} forgot {old_move} and learned {details['move_name']}."
            await safe_event_edit(event, success_text, buttons=None)
            await event.answer("TM used.")
            return
        await event.answer("Unknown TM action.", alert=True)

    async def handle_clear_db_callback(self, event: CallbackQuery.Event, data: str) -> None:
        if data == "cleardb:cancel":
            await safe_event_edit(event, "Database clear cancelled.", buttons=None)
            await event.answer()
            return
        if data != "cleardb:confirm":
            await event.answer("Unknown clear-db action.", alert=True)
            return
        if self.battle_service.battles_by_id or self.battle_service.pending_by_id or self.encounters.active_by_user:
            await event.answer("Finish active battles and encounters first.", alert=True)
            return
        clear_database()
        self.team_manager.pending_rename_team.clear()
        self.encounters.active_by_user.clear()
        self.command_use_sessions.clear()
        self.relearner_sessions.clear()
        await safe_event_edit(event, "Local test database cleared.", buttons=None)
        await event.answer("Database cleared.")

    def sorted_owned_pokemon(self, trainer, pokemons: PokemonRepository) -> list:
        pokemon_list = pokemons.list_owned_pokemon(trainer)
        return self.pokemon_data.sort_owned_pokemon(
            pokemon_list,
            sort_mode=trainer.sort_mode,
            descending=trainer.sort_descending,
        )

    def pokemon_page(self, trainer, pokemons: PokemonRepository, *, page: int) -> tuple[list, int, int]:
        pokemon_list = self.sorted_owned_pokemon(trainer, pokemons)
        total = len(pokemon_list)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // POKEMON_LIST_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * POKEMON_LIST_PAGE_SIZE
        end = start + POKEMON_LIST_PAGE_SIZE
        return pokemon_list[start:end], total, current_page

    def pokemon_list_text(self, trainer, *, items: list, total: int, page: int) -> str:
        lines = ["✦ Your Pokemon List", ""]
        if not items:
            lines.append("You do not own any Pokemon yet.")
        else:
            start = page * POKEMON_LIST_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"{index}. {self.pokemon_data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.append("")
        lines.append(f"• Total Pokemons : {total}")
        lines.append(f"• Displaying : {display_mode_label(trainer.display_mode)}")
        lines.append(f"• Sorting Method : {sort_mode_label(trainer.sort_mode)}")
        return "\n".join(lines)

    def pokemon_list_buttons(self, *, owner_id: int, page: int, total: int) -> list[list[Button]] | None:
        max_page = (max(total, 1) - 1) // POKEMON_LIST_PAGE_SIZE
        if max_page <= 0:
            return None
        buttons: list[Button] = []
        if page > 0:
            buttons.append(Button.inline("<", data=f"plist:page:{owner_id}:{page - 1}".encode("utf-8")))
        if page < max_page:
            buttons.append(Button.inline(">", data=f"plist:page:{owner_id}:{page + 1}".encode("utf-8")))
        return [buttons] if buttons else None

    def display_menu_text(self, trainer) -> str:
        lines = [
            "**Display Settings**",
            "━━━━━━━━━━━━━━━━━━━━━",
            "Choose what information to display next to your Pokémon.",
            ""
        ]
        for index, (_, label) in enumerate(DISPLAY_OPTIONS, start=1):
            lines.append(f"`[{index:<2}]` {label}")
        
        lines.extend([
            "",
            f"**Currently Displaying:** `{display_mode_label(trainer.display_mode)}`"
        ])
        return "\n".join(lines)

    def display_menu_buttons(self, current_mode: str) -> list[list[Button]]:
        buttons = [
            Button.inline(str(index), data=f"displaypref:set:{mode}".encode("utf-8"))
            for index, (mode, _) in enumerate(DISPLAY_OPTIONS, start=1)
        ]
        return chunk_buttons(buttons, per_row=5)

    def sort_menu_text(self, trainer) -> str:
        lines = [
            "**Sort Settings**",
            "━━━━━━━━━━━━━━━━━━━━━",
            "Choose how your Pokémon collection should be ordered.",
            ""
        ]
        for index, (_, label) in enumerate(SORT_OPTIONS, start=1):
            lines.append(f"`[{index:<2}]` {label}")
        
        lines.extend([
            "",
            f"**Current Sort:** `{sort_mode_label(trainer.sort_mode)}`",
            f"**Current Order:** `{'Descending' if trainer.sort_descending else 'Ascending'}`"
        ])
        return "\n".join(lines)

    def sort_menu_buttons(self, current_mode: str, sort_descending: bool) -> list[list[Button]]:
        buttons = [
            Button.inline(str(index), data=f"sortpref:set:{mode}".encode("utf-8"))
            for index, (mode, _) in enumerate(SORT_OPTIONS, start=1)
        ]
        rows = chunk_buttons(buttons, per_row=5)
        
        # Dynamic label based on the player's current choice
        order_label = "Descending" if sort_descending else "Ascending"
        rows.append([Button.inline(order_label, data="sortpref:toggle".encode("utf-8"))])
        
        return rows

    async def handle_pokemon_list_callback(self, event: CallbackQuery.Event, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 4 or parts[1] != "page":
            if len(parts) == 3:
                await event.answer("That Pokemon list is stale. Run /mypokemons again.", alert=True)
                return
            await event.answer("Unknown Pokemon list action.", alert=True)
            return
        if not parts[2].isdigit():
            await event.answer("Invalid Pokemon list owner.", alert=True)
            return
        owner_id = int(parts[2])
        if int(event.sender_id or 0) != owner_id:
            await event.answer("That Pokemon list belongs to another trainer.", alert=True)
            return
        if not parts[3].lstrip("+-").isdigit():
            await event.answer("Unknown Pokemon list action.", alert=True)
            return
        sender = await resolve_event_user(event)
        text, buttons = await run_db_work_async(lambda session: self._pokemon_list_page_payload(
            session,
            owner_id=owner_id,
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            page=int(parts[3]),
        ))
        await safe_event_edit(event, text, buttons=buttons)
        await event.answer()

    async def handle_nickname_callback(self, event: CallbackQuery.Event, data: str) -> None:
        if not event.is_private:
            await event.answer("Use nickname setup in private chat.", alert=True)
            return
        parts = data.split(":")
        if len(parts) < 3:
            await event.answer("Unknown nickname action.", alert=True)
            return
        action = parts[1]
        if not parts[2].isdigit():
            await event.answer("Invalid nickname owner.", alert=True)
            return
        owner_id = int(parts[2])
        if int(event.sender_id or 0) != owner_id:
            await event.answer("This nickname menu belongs to another trainer.", alert=True)
            return
        if action == "cancel":
            self._clear_nickname_session(owner_id)
            edited = await safe_event_edit(event, "Nickname menu closed.", buttons=None)
            if not edited:
                await event.respond("Nickname menu closed.")
            await event.answer("Closed.")
            return
        if action == "page":
            if len(parts) != 4 or not parts[3].lstrip("+-").isdigit():
                await event.answer("Unknown nickname action.", alert=True)
                return
            sender = await resolve_event_user(event)
            text, buttons = await run_db_work_async(lambda session: self._nickname_picker_payload(
                session,
                owner_id=owner_id,
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                page=int(parts[3]),
            ))
            await safe_event_edit(event, text, buttons=buttons, parse_mode="md")
            await event.answer()
            return
        if action == "pick":
            if len(parts) != 5 or not parts[3].lstrip("+-").isdigit() or not parts[4].isdigit():
                await event.answer("Unknown nickname action.", alert=True)
                return
            page = int(parts[3])
            pokemon_id = int(parts[4])
            sender = await resolve_event_user(event)
            self._set_nickname_session(owner_id=owner_id, pokemon_id=pokemon_id, page=page)
            with db_session(read_only=True) as session:
                trainers = TrainerRepository(session)
                pokemons = PokemonRepository(session)
                trainer = trainers.ensure_trainer(
                    telegram_user_id=owner_id,
                    username=getattr(sender, "username", None),
                    display_name=display_name(sender),
                )
                pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
                if pokemon is not None:
                    session.expunge(pokemon)
            if pokemon is None:
                self._clear_nickname_session(owner_id)
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            await safe_event_edit(
                event,
                self._nickname_prompt_text(pokemon, page=page),
                buttons=self._nickname_prompt_buttons(owner_id=owner_id, page=page),
                parse_mode="md",
            )
            await event.answer("Send the new nickname in DM.")
            return
        await event.answer("Unknown nickname action.", alert=True)

    async def handle_display_callback(self, event: CallbackQuery.Event, data: str) -> None:
        sender = await resolve_event_user(event)
        parts = data.split(":")
        if len(parts) != 3 or parts[1] != "set":
            await event.answer("Unknown display action.", alert=True)
            return
        payload = await run_db_work_async(lambda session: self._display_menu_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            mode=parts[2],
        ))
        if payload["status"] == "invalid":
            await event.answer("Unknown display action.", alert=True)
            return
        menu = await run_db_work_async(lambda session: self._display_menu_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            mode="",
        ))
        await safe_event_edit(event, str(menu["text"]), buttons=menu["buttons"], parse_mode="md")
        await event.answer(str(payload["text"]))

    async def handle_sort_callback(self, event: CallbackQuery.Event, data: str) -> None:
        sender = await resolve_event_user(event)
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "set":
            payload = await run_db_work_async(lambda session: self._sort_menu_payload(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                mode=parts[2],
            ))
            if payload["status"] == "invalid":
                await event.answer("Unknown sort action.", alert=True)
                return
            menu = await run_db_work_async(lambda session: self._sort_menu_payload(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                mode="",
            ))
            await safe_event_edit(event, str(menu["text"]), buttons=menu["buttons"], parse_mode="md")
            await event.answer(str(payload["text"]))
            return
        if len(parts) == 2 and parts[1] == "toggle":
            menu = await run_db_work_async(lambda session: self._toggle_sort_order_payload(
                session,
                owner_id=int(event.sender_id or 0),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            ))
            await safe_event_edit(event, str(menu["text"]), buttons=menu["buttons"], parse_mode="md")
            await event.answer(str(menu["answer"]))
            return
        await event.answer("Unknown sort action.", alert=True)
