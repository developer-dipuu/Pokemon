from __future__ import annotations

from datetime import datetime, timedelta
import json
import random
import re
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, asc, desc, select
from sqlalchemy.orm import Session, selectinload

from bot.db.models import (
    BannedUser,
    CommandLock,
    Inventory,
    KnownGroupChat,
    OwnedPokemon,
    PartySlot,
    RedeemCode,
    RedeemCodeClaim,
    TeamPreset,
    TeamPresetSlot,
    Trainer,
)
from bot.game.balls import (
    BALL_DEFINITIONS,
    BALL_FIELDS,
    BALL_GREAT,
    BALL_ORDER,
    BALL_POKE,
    BALL_ULTRA,
    ball_label,
    parse_extra_ball_counts,
    serialize_extra_ball_counts,
)
from bot.game.fusion import (
    build_effective_export_text,
    build_effective_packed_set,
    effective_species,
)
from bot.game.services.medicine import (
    medicine_name as medicine_display_name,
    normalize_medicine_key,
)


DISPLAY_NONE = "none"
DISPLAY_LEVEL = "level"
DISPLAY_NATURE = "nature"
DISPLAY_TYPE = "type"
DISPLAY_TYPE_SYMBOL = "typesym"
DISPLAY_CATEGORY = "category"
DISPLAY_IVS = "iv"
DISPLAY_EVS = "ev"
DISPLAY_HP = "hp"
DISPLAY_ATK = "atk"
DISPLAY_DEF = "def"
DISPLAY_SPA = "spa"
DISPLAY_SPD = "spd"
DISPLAY_SPE = "spe"
DISPLAY_TOTAL = "total"

DISPLAY_OPTIONS: list[tuple[str, str]] = [
    (DISPLAY_NONE, "None"),
    (DISPLAY_LEVEL, "Level"),
    (DISPLAY_NATURE, "Nature"),
    (DISPLAY_TYPE, "Type"),
    (DISPLAY_TYPE_SYMBOL, "Type Symbol"),
    (DISPLAY_CATEGORY, "Category"),
    (DISPLAY_IVS, "Iv Points"),
    (DISPLAY_EVS, "Ev Points"),
    (DISPLAY_HP, "Hp Points"),
    (DISPLAY_ATK, "Attack Points"),
    (DISPLAY_DEF, "Defense Points"),
    (DISPLAY_SPA, "Special Attack Points"),
    (DISPLAY_SPD, "Special Defense Points"),
    (DISPLAY_SPE, "Speed Points"),
    (DISPLAY_TOTAL, "Total Points"),
]

DISPLAY_LABELS = {mode: label for mode, label in DISPLAY_OPTIONS}

SORT_NONE = "none"
SORT_CAUGHT = "caught"
SORT_NAME = "name"
SORT_POKEDEX = "pokedex"
SORT_LEVEL = "level"
SORT_CATEGORY = "category"
SORT_IVS = "iv"
SORT_EVS = "ev"
SORT_HP = "hp"
SORT_ATK = "atk"
SORT_DEF = "def"
SORT_SPA = "spa"
SORT_SPD = "spd"
SORT_SPE = "spe"
SORT_TOTAL = "total"

SORT_OPTIONS: list[tuple[str, str]] = [
    (SORT_CAUGHT, "Order Caught"),
    (SORT_NAME, "Name"),
    (SORT_POKEDEX, "Pokedex Number"),
    (SORT_LEVEL, "Level"),
    (SORT_CATEGORY, "Category"),
    (SORT_IVS, "Iv Points"),
    (SORT_EVS, "Ev Points"),
    (SORT_HP, "Hp Points"),
    (SORT_ATK, "Attack Points"),
    (SORT_DEF, "Defense Points"),
    (SORT_SPA, "Special Attack Points"),
    (SORT_SPD, "Special Defense Points"),
    (SORT_SPE, "Speed Points"),
    (SORT_TOTAL, "Total Points"),
]

SORT_LABELS = {
    SORT_NONE: "None",
    **{mode: label for mode, label in SORT_OPTIONS},
}


def pokemon_total_ev(pokemon: OwnedPokemon) -> int:
    return (
        pokemon.ev_hp
        + pokemon.ev_atk
        + pokemon.ev_def
        + pokemon.ev_spa
        + pokemon.ev_spd
        + pokemon.ev_spe
    )


def display_mode_label(mode: str) -> str:
    return DISPLAY_LABELS.get(mode, "None")


def sort_mode_label(mode: str) -> str:
    return SORT_LABELS.get(mode, "None")


def normalize_preference(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


DISPLAY_LOOKUP = {
    normalize_preference(label): mode
    for mode, label in DISPLAY_OPTIONS
}
DISPLAY_LOOKUP.update({
    "ivs": DISPLAY_IVS,
    "iv": DISPLAY_IVS,
    "evs": DISPLAY_EVS,
    "ev": DISPLAY_EVS,
    "types": DISPLAY_TYPE,
    "typesymbol": DISPLAY_TYPE_SYMBOL,
    "typesymbols": DISPLAY_TYPE_SYMBOL,
})

SORT_LOOKUP = {
    normalize_preference(label): mode
    for mode, label in SORT_OPTIONS
}
SORT_LOOKUP.update({
    "none": SORT_NONE,
    "caught": SORT_CAUGHT,
    "ivs": SORT_IVS,
    "iv": SORT_IVS,
    "evs": SORT_EVS,
    "ev": SORT_EVS,
})


def normalize_display_mode(value: str) -> str | None:
    cleaned = normalize_preference(value)
    if not cleaned:
        return None
    if cleaned == "none":
        return DISPLAY_NONE
    return DISPLAY_LOOKUP.get(cleaned)


def normalize_sort_mode(value: str) -> str | None:
    cleaned = normalize_preference(value)
    if not cleaned:
        return None
    return SORT_LOOKUP.get(cleaned)


def pokemon_list_label(pokemon: OwnedPokemon, display_mode: str) -> str:
    egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
    shiny_icon = " ✨" if pokemon.shiny else ""
    base = f"{pokemon.species}{egg_icon}{shiny_icon}"
    
    if display_mode == DISPLAY_NONE:
        return base
    if display_mode == DISPLAY_LEVEL:
        return f"{base} lv {pokemon.level}"
    if display_mode == DISPLAY_NATURE:
        return f"{base} - {pokemon.nature}"
    if display_mode == DISPLAY_CATEGORY:
        return f"{base} - {pokemon.source_kind.replace('_', ' ').title()}"
    if display_mode == DISPLAY_IVS:
        return f"{base} - iv {pokemon.total_iv}"
    if display_mode == DISPLAY_EVS:
        return f"{base} - ev {pokemon_total_ev(pokemon)}"
    return f"{base} lv {pokemon.level}"


def normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def pokemon_display_label(pokemon: OwnedPokemon, display_mode: str) -> str:
    egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
    shiny_icon = " ✨" if pokemon.shiny else ""
    base = f"{effective_species(pokemon)}{egg_icon}{shiny_icon}"
    
    if display_mode == DISPLAY_NONE:
        return base
    if display_mode == DISPLAY_LEVEL:
        return f"{base} lv {pokemon.level}"
    if display_mode == DISPLAY_NATURE:
        return f"{base} - {pokemon.nature}"
    if display_mode == DISPLAY_CATEGORY:
        return f"{base} - {pokemon.source_kind.replace('_', ' ').title()}"
    if display_mode == DISPLAY_IVS:
        return f"{base} - iv {pokemon.total_iv}"
    if display_mode == DISPLAY_EVS:
        return f"{base} - ev {pokemon_total_ev(pokemon)}"
    return f"{base} lv {pokemon.level}"

def parse_item_counts(raw: str | None) -> dict[str, int]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in payload.items():
        name = str(key).strip()
        if not name:
            continue
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            counts[name] = amount
    return counts


def serialize_item_counts(counts: dict[str, int]) -> str:
    cleaned = {
        str(key).strip(): int(value)
        for key, value in counts.items()
        if str(key).strip() and int(value) > 0
    }
    return json.dumps(cleaned, sort_keys=True)


class TrainerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_trainer(self, *, telegram_user_id: int, username: str | None, display_name: str) -> Trainer:
        trainer = self.get_by_telegram_user_id(telegram_user_id)
        if trainer:
            trainer.username = username
            trainer.display_name = display_name
            if trainer.inventory is None:
                trainer.inventory = Inventory()
            self._ensure_party_slots(trainer)
            return trainer

        trainer = Trainer(
            telegram_user_id=telegram_user_id,
            username=username,
            display_name=display_name,
            inventory=Inventory(),
        )
        self.session.add(trainer)
        self.session.flush()
        self._ensure_party_slots(trainer)
        return trainer

    def get_by_telegram_user_id(self, telegram_user_id: int) -> Trainer | None:
        return self.session.scalar(select(Trainer).where(Trainer.telegram_user_id == telegram_user_id))

    def _ensure_party_slots(self, trainer: Trainer) -> None:
        existing = {
            slot.slot_index
            for slot in self.session.scalars(
                select(PartySlot).where(PartySlot.trainer_id == trainer.id)
            )
        }
        for slot_index in range(1, 7):
            if slot_index not in existing:
                self.session.add(PartySlot(trainer_id=trainer.id, slot_index=slot_index))
        self.session.flush()
        self.session.refresh(trainer)

    def set_preferences(
        self,
        trainer: Trainer,
        *,
        sort_mode: str | None = None,
        display_mode: str | None = None,
        sort_descending: bool | None = None,
        challenge_mode: str | None = None,
        challenge_generation: int | None = None,
        battle_visuals: bool | None = None,
    ) -> None:
        if sort_mode:
            trainer.sort_mode = sort_mode
        if display_mode:
            trainer.display_mode = display_mode
        if sort_descending is not None:
            trainer.sort_descending = bool(sort_descending)
        if challenge_mode:
            trainer.challenge_mode = challenge_mode
        if challenge_generation is not None:
            trainer.challenge_generation = int(challenge_generation)
        if battle_visuals is not None:
            trainer.battle_visuals = bool(battle_visuals)

    def set_region(self, trainer: Trainer, region: str) -> None:
        trainer.current_region = region
        trainer.current_location = None

    def set_location(self, trainer: Trainer, location: str | None) -> None:
        trainer.current_location = location

    def set_starter_species(self, trainer: Trainer, species: str) -> None:
        trainer.starter_species = species

    def safari_reset_at(self, trainer: Trainer) -> datetime | None:
        entered_at = trainer.last_safari_entered_at
        if entered_at is None:
            return None
        reset_at = datetime(
            year=entered_at.year,
            month=entered_at.month,
            day=entered_at.day,
        ) + timedelta(days=1)
        return reset_at

    def safari_available_now(self, trainer: Trainer, *, now_utc: datetime | None = None) -> bool:
        entered_at = trainer.last_safari_entered_at
        if entered_at is None:
            return True
        current = now_utc or datetime.utcnow()
        return entered_at.date() < current.date()

    def mark_safari_entry(self, trainer: Trainer, *, entered_at: datetime | None = None) -> None:
        trainer.last_safari_entered_at = entered_at or datetime.utcnow()

    def reset_safari_cooldown(self, trainer: Trainer) -> None:
        trainer.last_safari_entered_at = None

    def place_in_first_party_slot(self, trainer: Trainer, pokemon: OwnedPokemon) -> None:
        self._ensure_party_slots(trainer)
        slots = list(
            self.session.scalars(
                select(PartySlot).where(PartySlot.trainer_id == trainer.id).order_by(PartySlot.slot_index)
            )
        )
        for slot in slots:
            if slot.pokemon_id is None:
                slot.pokemon = pokemon
                self.session.flush()
                return

    # ──────────────────────────────────────────────
    # Trainer XP / Level helpers  (level range 1–200)
    # ──────────────────────────────────────────────
    @staticmethod
    def exp_for_level(level: int) -> int:
        """Total EXP needed to reach `level` from level 1.
        Uses a quadratic curve: level 200 ~= 200,000 EXP.
        """
        level = max(1, min(level, 200))
        return int(5 * (level - 1) ** 2 + 50 * (level - 1))

    @staticmethod
    def exp_gained_from_wild(wild_level: int, *, caught: bool) -> int:
        """EXP gained from defeating or catching a wild Pokémon."""
        base = max(1, wild_level) * 3
        return base + (wild_level // 2) if caught else base

    @staticmethod
    def vp_gained_from_wild(wild_level: int, *, caught: bool) -> int:
        """VP gained from defeating or catching a wild Pokémon."""
        return random.randint(50, 150)

    @staticmethod
    def sp_gained_from_wild(wild_level: int, *, caught: bool) -> int:
        """SP gained from defeating or catching a wild Pokémon."""
        return random.randint(1, 2)

    def _apply_level_up_rewards(
        self,
        trainer: "Trainer",
        *,
        old_level: int,
        new_level: int,
    ) -> list[str]:
        if trainer.inventory is None:
            trainer.inventory = Inventory()
        if new_level <= old_level:
            return []

        inventories = InventoryRepository(self.session)
        reward_lp = 0
        reward_tickets = 0
        reward_boxes = 0

        for level in range(old_level + 1, new_level + 1):
            if 1 <= level <= 35:
                reward_lp += 250
            elif 36 <= level <= 70:
                reward_tickets += 100
            elif 71 <= level <= 200:
                reward_boxes += 1

        if reward_lp > 0:
            inventories.add_league_points(trainer, reward_lp)
        if reward_tickets > 0:
            inventories.add_key_item(trainer, "Holowear Ticket", reward_tickets)
        if reward_boxes > 0:
            inventories.add_key_item(trainer, "Trainer Box", reward_boxes)

        lines: list[str] = []
        if reward_lp > 0:
            lines.append(f"League Points x{reward_lp}")
        if reward_tickets > 0:
            lines.append(f"Holowear Tickets x{reward_tickets}")
        if reward_boxes > 0:
            lines.append(f"Trainer Box x{reward_boxes}")
        return lines

    def award_wild_outcome(
        self,
        trainer: "Trainer",
        *,
        wild_level: int,
        caught: bool,
    ) -> dict:
        """Grant EXP + VP, level up if needed. Returns a dict with gained amounts."""
        exp_gain = self.exp_gained_from_wild(wild_level, caught=caught)
        vp_gain = self.vp_gained_from_wild(wild_level, caught=caught)
        sp_gain = self.sp_gained_from_wild(wild_level, caught=caught)
        trainer.trainer_exp += exp_gain
        if trainer.inventory:
            trainer.inventory.victory_points += vp_gain
            trainer.inventory.season_points += sp_gain

        # Update win/catch counters
        if caught:
            trainer.total_caught = (trainer.total_caught or 0) + 1
        else:
            trainer.total_wins = (trainer.total_wins or 0) + 1

        # Level up loop (cap at 200)
        leveled_up = False
        old_level = int(trainer.trainer_level or 1)
        while trainer.trainer_level < 200:
            needed = self.exp_for_level(trainer.trainer_level + 1)
            if trainer.trainer_exp >= needed:
                trainer.trainer_level += 1
                leveled_up = True
            else:
                break
        level_reward_lines = self._apply_level_up_rewards(
            trainer,
            old_level=old_level,
            new_level=int(trainer.trainer_level or old_level),
        )

        return {
            "exp_gain": exp_gain,
            "vp_gain": vp_gain,
            "sp_gain": sp_gain,
            "leveled_up": leveled_up,
            "new_level": trainer.trainer_level,
            "level_reward_lines": level_reward_lines,
        }

    def rank_up_trainer_levels(self, trainer: "Trainer", *, levels: int) -> dict[str, Any]:
        gain = max(0, int(levels))
        if gain <= 0:
            return {
                "old_level": int(trainer.trainer_level or 1),
                "new_level": int(trainer.trainer_level or 1),
                "levels_gained": 0,
                "level_reward_lines": [],
            }
        old_level = int(trainer.trainer_level or 1)
        new_level = min(200, old_level + gain)
        trainer.trainer_level = new_level
        trainer.trainer_exp = max(int(trainer.trainer_exp or 0), self.exp_for_level(new_level))
        lines = self._apply_level_up_rewards(trainer, old_level=old_level, new_level=new_level)
        return {
            "old_level": old_level,
            "new_level": new_level,
            "levels_gained": max(0, new_level - old_level),
            "level_reward_lines": lines,
        }

    def reset_trainer_level(self, trainer: "Trainer") -> dict[str, Any]:
        old_level = int(trainer.trainer_level or 1)
        old_exp = int(trainer.trainer_exp or 0)
        trainer.trainer_level = 1
        trainer.trainer_exp = 0
        return {
            "old_level": old_level,
            "new_level": 1,
            "old_exp": old_exp,
            "new_exp": 0,
        }

    def add_trainer_exp(self, trainer: "Trainer", amount: int) -> dict[str, Any]:
        gain = max(0, int(amount))
        old_level = int(trainer.trainer_level or 1)
        old_exp = int(trainer.trainer_exp or 0)
        if gain <= 0:
            return {
                "old_level": old_level,
                "new_level": old_level,
                "old_exp": old_exp,
                "new_exp": old_exp,
                "exp_gained": 0,
                "level_reward_lines": [],
            }

        trainer.trainer_exp = old_exp + gain
        while trainer.trainer_level < 200:
            needed = self.exp_for_level(trainer.trainer_level + 1)
            if trainer.trainer_exp < needed:
                break
            trainer.trainer_level += 1

        level_reward_lines = self._apply_level_up_rewards(
            trainer,
            old_level=old_level,
            new_level=int(trainer.trainer_level or old_level),
        )
        return {
            "old_level": old_level,
            "new_level": int(trainer.trainer_level or old_level),
            "old_exp": old_exp,
            "new_exp": int(trainer.trainer_exp or old_exp),
            "exp_gained": gain,
            "level_reward_lines": level_reward_lines,
        }

    def list_telegram_user_ids(self) -> list[int]:
        return [int(user_id) for user_id in self.session.scalars(select(Trainer.telegram_user_id).order_by(Trainer.telegram_user_id))]

    def delete_trainer(self, trainer: Trainer) -> None:
        self.session.delete(trainer)
        self.session.flush()

    def record_battle_loss(self, trainer: "Trainer") -> None:
        trainer.total_losses = (trainer.total_losses or 0) + 1


class AdminRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def track_group_chat(self, chat_id: int, *, title: str | None = None) -> KnownGroupChat:
        value = int(chat_id)
        chat = self.session.get(KnownGroupChat, value)
        if chat is None:
            chat = KnownGroupChat(chat_id=value, title=(title or None))
            self.session.add(chat)
        elif title:
            chat.title = title
        self.session.flush()
        return chat

    def list_group_chat_ids(self) -> list[int]:
        return [int(chat_id) for chat_id in self.session.scalars(select(KnownGroupChat.chat_id).order_by(KnownGroupChat.chat_id))]

    def count_group_chats(self) -> int:
        return len(self.list_group_chat_ids())

    def is_banned_user(self, telegram_user_id: int) -> bool:
        return self.session.get(BannedUser, int(telegram_user_id)) is not None

    def ban_user(
        self,
        telegram_user_id: int,
        *,
        added_by_user_id: int | None = None,
        reason: str | None = None,
    ) -> bool:
        value = int(telegram_user_id)
        if self.session.get(BannedUser, value) is not None:
            return False
        self.session.add(
            BannedUser(
                telegram_user_id=value,
                added_by_user_id=int(added_by_user_id) if added_by_user_id else None,
                reason=reason,
            )
        )
        self.session.flush()
        return True

    def unban_user(self, telegram_user_id: int) -> bool:
        entry = self.session.get(BannedUser, int(telegram_user_id))
        if entry is None:
            return False
        self.session.delete(entry)
        self.session.flush()
        return True

    def count_banned_users(self) -> int:
        return len([*self.session.scalars(select(BannedUser.telegram_user_id))])


class CommandLockRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def normalize_name(self, command_name: str) -> str:
        value = str(command_name or "").strip().lower()
        return value[1:] if value.startswith("/") else value

    def get(self, command_name: str) -> CommandLock | None:
        return self.session.get(CommandLock, self.normalize_name(command_name))

    def is_locked(self, command_name: str) -> bool:
        return self.get(command_name) is not None

    def lock(self, command_name: str, *, locked_by_user_id: int | None = None) -> CommandLock:
        normalized = self.normalize_name(command_name)
        entry = self.session.get(CommandLock, normalized)
        if entry is None:
            entry = CommandLock(
                command_name=normalized,
                locked_by_user_id=int(locked_by_user_id) if locked_by_user_id is not None else None,
            )
            self.session.add(entry)
        else:
            entry.locked_by_user_id = int(locked_by_user_id) if locked_by_user_id is not None else entry.locked_by_user_id
        self.session.flush()
        return entry

    def unlock(self, command_name: str) -> bool:
        entry = self.get(command_name)
        if entry is None:
            return False
        self.session.delete(entry)
        self.session.flush()
        return True

    def list_locked(self) -> list[str]:
        return [str(name) for name in self.session.scalars(select(CommandLock.command_name).order_by(CommandLock.command_name))]


class RedeemCodeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def normalize_code(code: str) -> str:
        return str(code or "").strip().upper()

    def create_code(
        self,
        *,
        code: str,
        rewards_payload: dict[str, Any],
        max_redemptions: int | None,
        created_by_user_id: int | None = None,
    ) -> RedeemCode:
        normalized = self.normalize_code(code)
        entry = RedeemCode(
            code=normalized,
            rewards_json=json.dumps(rewards_payload, sort_keys=True),
            max_redemptions=int(max_redemptions) if max_redemptions is not None else None,
            redeemed_count=0,
            created_by_user_id=int(created_by_user_id) if created_by_user_id is not None else None,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def get_code(self, code: str) -> RedeemCode | None:
        return self.session.get(RedeemCode, self.normalize_code(code))

    def parse_rewards(self, entry: RedeemCode) -> dict[str, Any]:
        try:
            payload = json.loads(entry.rewards_json or "{}")
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def has_user_redeemed(self, entry: RedeemCode, telegram_user_id: int) -> bool:
        claim = self.session.scalar(
            select(RedeemCodeClaim).where(
                RedeemCodeClaim.code == entry.code,
                RedeemCodeClaim.telegram_user_id == int(telegram_user_id),
            )
        )
        return claim is not None

    def remaining_redemptions(self, entry: RedeemCode) -> int | None:
        if entry.max_redemptions is None:
            return None
        return max(0, int(entry.max_redemptions) - int(entry.redeemed_count or 0))

    def can_redeem(self, entry: RedeemCode) -> bool:
        remaining = self.remaining_redemptions(entry)
        return remaining is None or remaining > 0

    def record_redemption(self, entry: RedeemCode, *, telegram_user_id: int) -> RedeemCodeClaim:
        if self.has_user_redeemed(entry, telegram_user_id):
            raise ValueError("already_redeemed")
        if not self.can_redeem(entry):
            raise ValueError("redeem_limit_reached")
        claim = RedeemCodeClaim(
            code=entry.code,
            telegram_user_id=int(telegram_user_id),
        )
        self.session.add(claim)
        entry.redeemed_count = int(entry.redeemed_count or 0) + 1
        self.session.flush()
        return claim


class InventoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _is_equippable_item_name(name: str) -> bool:
        lowered = str(name or "").strip().lower()
        normalized = normalize_lookup(lowered)
        if not lowered:
            return False
        if InventoryRepository._is_stone_item_name(name):
            return True
        if lowered.endswith(" mint"):
            return True
        return normalized in {
            "abilitycapsule",
            "abilitypatch",
            "bottlecap",
            "goldbottlecap",
        }

    def _raw_held_item_counts(self, trainer: Trainer) -> dict[str, int]:
        return parse_item_counts(getattr(trainer.inventory, "held_items_json", "{}"))

    def _raw_key_item_counts(self, trainer: Trainer) -> dict[str, int]:
        return parse_item_counts(getattr(trainer.inventory, "key_items_json", "{}"))

    def _extra_ball_counts(self, trainer: Trainer) -> dict[str, int]:
        return parse_extra_ball_counts(getattr(trainer.inventory, "special_balls_json", "{}"))

    def _store_extra_ball_counts(self, trainer: Trainer, counts: dict[str, int]) -> None:
        trainer.inventory.special_balls_json = serialize_extra_ball_counts(counts)

    def _held_item_counts(self, trainer: Trainer) -> dict[str, int]:
        counts = self._raw_held_item_counts(trainer)
        for name, amount in self._raw_key_item_counts(trainer).items():
            if int(amount) <= 0 or not self._is_equippable_item_name(name):
                continue
            counts[name] = int(counts.get(name, 0)) + int(amount)
        return counts

    def _store_held_item_counts(self, trainer: Trainer, counts: dict[str, int]) -> None:
        trainer.inventory.held_items_json = serialize_item_counts(counts)

    def _tm_counts(self, trainer: Trainer) -> dict[str, int]:
        return parse_item_counts(getattr(trainer.inventory, "tm_inventory_json", "{}"))

    def _store_tm_counts(self, trainer: Trainer, counts: dict[str, int]) -> None:
        trainer.inventory.tm_inventory_json = serialize_item_counts(counts)

    def _medicine_counts(self, trainer: Trainer) -> dict[str, int]:
        raw_counts = parse_item_counts(getattr(trainer.inventory, "medicine_inventory_json", "{}"))
        normalized: dict[str, int] = {}
        for name, count in raw_counts.items():
            canonical_name = self._canonical_medicine_name(name)
            normalized[canonical_name] = int(normalized.get(canonical_name, 0)) + int(count)
        return normalized

    def _store_medicine_counts(self, trainer: Trainer, counts: dict[str, int]) -> None:
        normalized: dict[str, int] = {}
        for name, count in counts.items():
            canonical_name = self._canonical_medicine_name(name)
            normalized[canonical_name] = int(normalized.get(canonical_name, 0)) + int(count)
        trainer.inventory.medicine_inventory_json = serialize_item_counts(normalized)

    @staticmethod
    def _canonical_medicine_name(name: str) -> str:
        medicine_key = normalize_medicine_key(str(name))
        if medicine_key is not None:
            return medicine_display_name(medicine_key)
        return str(name)

    def _key_item_counts(self, trainer: Trainer) -> dict[str, int]:
        counts = self._raw_key_item_counts(trainer)
        return {
            name: int(amount)
            for name, amount in counts.items()
            if int(amount) > 0 and not self._is_equippable_item_name(name)
        }

    def _store_key_item_counts(self, trainer: Trainer, counts: dict[str, int]) -> None:
        trainer.inventory.key_items_json = serialize_item_counts(counts)

    def ball_count(self, trainer: Trainer, ball_kind: str) -> int:
        field = BALL_FIELDS.get(ball_kind)
        if field:
            return int(getattr(trainer.inventory, field))
        return int(self._extra_ball_counts(trainer).get(ball_kind, 0))

    def ball_counts(self, trainer: Trainer, *, include_zero: bool = False) -> list[tuple[str, int]]:
        counts = [(ball_kind, self.ball_count(trainer, ball_kind)) for ball_kind in BALL_ORDER]
        if include_zero:
            return counts
        return [(ball_kind, count) for ball_kind, count in counts if count > 0]

    def consume_ball(self, trainer: Trainer, ball_kind: str) -> bool:
        field = BALL_FIELDS.get(ball_kind)
        if field:
            current = int(getattr(trainer.inventory, field))
            if current <= 0:
                return False
            setattr(trainer.inventory, field, current - 1)
            return True
        if ball_kind not in BALL_DEFINITIONS:
            raise ValueError(f"Unknown ball kind: {ball_kind}")
        counts = self._extra_ball_counts(trainer)
        current = int(counts.get(ball_kind, 0))
        if current <= 0:
            return False
        counts[ball_kind] = current - 1
        self._store_extra_ball_counts(trainer, counts)
        return True

    def add_ball(self, trainer: Trainer, ball_kind: str, amount: int) -> None:
        if int(amount) <= 0:
            raise ValueError("Ball amount must be positive.")
        field = BALL_FIELDS.get(ball_kind)
        if field:
            setattr(trainer.inventory, field, int(getattr(trainer.inventory, field)) + int(amount))
            return
        if ball_kind not in BALL_DEFINITIONS:
            raise ValueError(f"Unknown ball kind: {ball_kind}")
        counts = self._extra_ball_counts(trainer)
        counts[ball_kind] = int(counts.get(ball_kind, 0)) + int(amount)
        self._store_extra_ball_counts(trainer, counts)

    def consume_victory_points(self, trainer: Trainer, amount: int) -> bool:
        if amount <= 0:
            raise ValueError("VP amount must be positive.")
        if trainer.inventory.victory_points < amount:
            return False
        trainer.inventory.victory_points -= amount
        return True

    def add_victory_points(self, trainer: Trainer, amount: int) -> None:
        if amount <= 0:
            raise ValueError("VP amount must be positive.")
        trainer.inventory.victory_points += amount

    def add_season_points(self, trainer: Trainer, amount: int) -> None:
        if amount <= 0:
            raise ValueError("SP amount must be positive.")
        trainer.inventory.season_points += amount

    def consume_league_points(self, trainer: Trainer, amount: int) -> bool:
        if amount <= 0:
            raise ValueError("LP amount must be positive.")
        if trainer.inventory.league_points < amount:
            return False
        trainer.inventory.league_points -= amount
        return True

    def add_league_points(self, trainer: Trainer, amount: int) -> None:
        if amount <= 0:
            raise ValueError("LP amount must be positive.")
        trainer.inventory.league_points += amount

    def has_item(self, trainer: Trainer, item_name: str) -> bool:
        target = normalize_lookup(item_name)
        return any(
            normalize_lookup(name) == target and amount > 0
            for name, amount in self._held_item_counts(trainer).items()
        )

    def held_item_count(self, trainer: Trainer, item_name: str) -> int:
        target = normalize_lookup(item_name)
        for name, amount in self._held_item_counts(trainer).items():
            if normalize_lookup(name) == target:
                return int(amount)
        return 0

    def held_item_name(self, trainer: Trainer, item_key: str) -> str | None:
        target = normalize_lookup(item_key)
        for name, amount in self._held_item_counts(trainer).items():
            if amount > 0 and normalize_lookup(name) == target:
                return name
        return None

    def held_item_counts(self, trainer: Trainer) -> dict[str, int]:
        return self._held_item_counts(trainer)

    def add_item(self, trainer: Trainer, item_name: str, amount: int = 1) -> None:
        if int(amount) <= 0:
            raise ValueError("Item amount must be positive.")
        counts = self._held_item_counts(trainer)
        counts[item_name] = int(counts.get(item_name, 0)) + int(amount)
        self._store_held_item_counts(trainer, counts)

    def consume_item(self, trainer: Trainer, item_name: str, amount: int = 1) -> bool:
        if int(amount) <= 0:
            raise ValueError("Item amount must be positive.")
        counts = self._raw_held_item_counts(trainer)
        current_name = next(
            (name for name, count in counts.items() if count > 0 and normalize_lookup(name) == normalize_lookup(item_name)),
            None,
        )
        if current_name is None:
            key_counts = self._raw_key_item_counts(trainer)
            current_name = next(
                (
                    name
                    for name, count in key_counts.items()
                    if count > 0
                    and self._is_equippable_item_name(name)
                    and normalize_lookup(name) == normalize_lookup(item_name)
                ),
                None,
            )
            if current_name is None:
                return False
            current = int(key_counts.get(current_name, 0))
            if current < int(amount):
                return False
            remaining = current - int(amount)
            if remaining > 0:
                key_counts[current_name] = remaining
            else:
                key_counts.pop(current_name, None)
            trainer.inventory.key_items_json = serialize_item_counts(key_counts)
            return True
        current = int(counts.get(current_name, 0))
        if current < int(amount):
            return False
        remaining = current - int(amount)
        if remaining > 0:
            counts[current_name] = remaining
        else:
            counts.pop(current_name, None)
        self._store_held_item_counts(trainer, counts)
        return True

    def add_tm(self, trainer: Trainer, tm_name: str, amount: int = 1) -> None:
        if int(amount) <= 0:
            raise ValueError("TM amount must be positive.")
        counts = self._tm_counts(trainer)
        counts[tm_name] = int(counts.get(tm_name, 0)) + int(amount)
        self._store_tm_counts(trainer, counts)

    def consume_tm(self, trainer: Trainer, tm_name: str, amount: int = 1) -> bool:
        if int(amount) <= 0:
            raise ValueError("TM amount must be positive.")
        counts = self._tm_counts(trainer)
        current_name = next(
            (name for name, count in counts.items() if count > 0 and normalize_lookup(name) == normalize_lookup(tm_name)),
            None,
        )
        if current_name is None:
            return False
        current = int(counts.get(current_name, 0))
        if current < int(amount):
            return False
        remaining = current - int(amount)
        if remaining > 0:
            counts[current_name] = remaining
        else:
            counts.pop(current_name, None)
        self._store_tm_counts(trainer, counts)
        return True

    def tm_counts(self, trainer: Trainer) -> dict[str, int]:
        return self._tm_counts(trainer)

    def medicine_count(self, trainer: Trainer, medicine_name: str) -> int:
        canonical_name = self._canonical_medicine_name(medicine_name)
        return int(self._medicine_counts(trainer).get(canonical_name, 0))

    def medicine_counts(self, trainer: Trainer) -> dict[str, int]:
        return self._medicine_counts(trainer)

    def add_medicine(self, trainer: Trainer, medicine_name: str, amount: int = 1) -> None:
        if int(amount) <= 0:
            raise ValueError("Medicine amount must be positive.")
        counts = self._medicine_counts(trainer)
        canonical_name = self._canonical_medicine_name(medicine_name)
        counts[canonical_name] = int(counts.get(canonical_name, 0)) + int(amount)
        self._store_medicine_counts(trainer, counts)

    def key_item_count(self, trainer: Trainer, item_name: str) -> int:
        target = normalize_lookup(item_name)
        for name, amount in self._key_item_counts(trainer).items():
            if normalize_lookup(name) == target:
                return int(amount)
        return 0

    def key_item_counts(self, trainer: Trainer) -> dict[str, int]:
        return self._key_item_counts(trainer)

    def add_key_item(self, trainer: Trainer, item_name: str, amount: int = 1) -> None:
        if int(amount) <= 0:
            raise ValueError("Key Item amount must be positive.")
        counts = self._key_item_counts(trainer)
        counts[item_name] = int(counts.get(item_name, 0)) + int(amount)
        self._store_key_item_counts(trainer, counts)

    def consume_key_item(self, trainer: Trainer, item_name: str, amount: int = 1) -> bool:
        if int(amount) <= 0:
            raise ValueError("Key Item amount must be positive.")
        counts = self._key_item_counts(trainer)
        current_name = next(
            (name for name, count in counts.items() if count > 0 and normalize_lookup(name) == normalize_lookup(item_name)),
            None,
        )
        if current_name is None:
            return False
        current = int(counts.get(current_name, 0))
        if current < int(amount):
            return False
        remaining = current - int(amount)
        if remaining > 0:
            counts[current_name] = remaining
        else:
            counts.pop(current_name, None)
        self._store_key_item_counts(trainer, counts)
        return True

    def consume_medicine(self, trainer: Trainer, medicine_name: str, amount: int = 1) -> bool:
        if int(amount) <= 0:
            raise ValueError("Medicine amount must be positive.")
        counts = self._medicine_counts(trainer)
        canonical_name = self._canonical_medicine_name(medicine_name)
        current = int(counts.get(canonical_name, 0))
        if current < int(amount):
            return False
        remaining = current - int(amount)
        if remaining > 0:
            counts[canonical_name] = remaining
        else:
            counts.pop(canonical_name, None)
        self._store_medicine_counts(trainer, counts)
        return True

    def egg_energy(self, trainer: Trainer) -> int:
        return int(getattr(trainer.inventory, "egg_energy", 0) or 0)

    def add_egg_energy(self, trainer: Trainer, amount: int) -> None:
        if int(amount) <= 0:
            raise ValueError("Egg Energy amount must be positive.")
        trainer.inventory.egg_energy = self.egg_energy(trainer) + int(amount)

    def consume_egg_energy(self, trainer: Trainer, amount: int) -> bool:
        if int(amount) <= 0:
            raise ValueError("Egg Energy amount must be positive.")
        current = self.egg_energy(trainer)
        if current < int(amount):
            return False
        trainer.inventory.egg_energy = current - int(amount)
        return True

    @staticmethod
    def _tm_sort_key(name: str) -> tuple[int, str]:
        match = re.match(r"TM(\d+)", str(name))
        number = int(match.group(1)) if match else 9999
        return number, str(name).lower()

    @staticmethod
    def _is_stone_item_name(name: str) -> bool:
        lowered = str(name or "").strip().lower()
        if not lowered:
            return False
        normalized = normalize_lookup(lowered)
        if normalized in {"redorb", "blueorb"}:
            return True
        if (
            lowered.endswith("ite")
            or lowered.endswith("ite x")
            or lowered.endswith("ite y")
            or lowered.endswith("ite z")
        ):
            # Eviolite is a held item, not a stone command-style key item.
            return normalized != "eviolite"
        # Most Z-crystals end with " Z" (e.g. Pikanium Z, Firium Z).
        if lowered.endswith(" z"):
            return True
        return False

    def render_bag(self, trainer: Trainer, *, category: str = "overview", page: int = 0) -> tuple[str, int, int]:
        inventory = trainer.inventory
        ITEMS_PER_PAGE = 30
        
        # Helper function to handle paginating any dictionary of items
        def _paginate(item_dict: dict, title: str) -> tuple[str, int, int]:
            items = sorted(item_dict.items(), key=lambda x: str.lower(x[0]))
            total = len(items)
            if total == 0:
                return f"**Bag » {title}**\n━━━━━━━━━━━━━━━━━━━━━\n__Empty__", 0, 0
                
            max_page = max(0, (total - 1) // ITEMS_PER_PAGE)
            current_page = min(max(page, 0), max_page)
            start = current_page * ITEMS_PER_PAGE
            chunk = items[start:start + ITEMS_PER_PAGE]
            
            lines = [f"**Bag » {title}**", "━━━━━━━━━━━━━━━━━━━━━"]
            for name, count in chunk:
                lines.append(f"• `{name}`: x{count}")
                
            lines.extend(["", f"Page {current_page + 1} of {max_page + 1}"])
            return "\n".join(lines), current_page, max_page

        if category == "balls":
            counts = {
                ball_label(k): v 
                for k, v in self.ball_counts(trainer, include_zero=True) 
                if v > 0 or k in ("poke", "great", "ultra")
            }
            return _paginate(counts, "Poké Balls")
            
        if category == "held":
            return _paginate(self._held_item_counts(trainer), "Held Items")
            
        if category == "tms":
            counts = self._tm_counts(trainer)
            items = sorted(counts.items(), key=lambda x: self._tm_sort_key(x[0]))
            total = len(items)
            if total == 0:
                return "**Bag » TMs**\n━━━━━━━━━━━━━━━━━━━━━\n__Empty__", 0, 0
                
            max_page = max(0, (total - 1) // ITEMS_PER_PAGE)
            current_page = min(max(page, 0), max_page)
            start = current_page * ITEMS_PER_PAGE
            chunk = items[start:start + ITEMS_PER_PAGE]
            
            lines = ["**Bag » TMs**", "━━━━━━━━━━━━━━━━━━━━━"]
            for name, count in chunk:
                lines.append(f"• `{name}`: x{count}")
                
            lines.extend(["", f"Page {current_page + 1} of {max_page + 1}"])
            return "\n".join(lines), current_page, max_page
            
        if category == "medicine":
            return _paginate(self._medicine_counts(trainer), "Medicine")
            
        if category == "key":
            return _paginate(self._key_item_counts(trainer), "Key Items")
            
        # --- OVERVIEW PAGE ---
        held_total = sum(self._held_item_counts(trainer).values())
        tm_total = sum(self._tm_counts(trainer).values())
        medicine_total = sum(self._medicine_counts(trainer).values())
        key_item_total = sum(self._key_item_counts(trainer).values())
        trainer_box_count = self.key_item_count(trainer, "Trainer Box")
        holowear_ticket_count = self.key_item_count(trainer, "Holowear Ticket")
        balls_total = sum(count for _, count in self.ball_counts(trainer, include_zero=False))
        
        egg_total = 0
        try:
            eggs_payload = json.loads(getattr(trainer, "eggs_json", None) or "[]")
            if isinstance(eggs_payload, list):
                egg_total = len([entry for entry in eggs_payload if isinstance(entry, dict)])
        except (TypeError, ValueError):
            egg_total = 0
            
        text = (
            "**Trainer's Bag**\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "**Currencies**\n"
            f"• Victory Points: `{inventory.victory_points:,}` VP\n"
            f"• League Points: `{getattr(inventory, 'league_points', 0):,}` LP\n"
            f"• Season Points: `{getattr(inventory, 'season_points', 0):,}` SP\n\n"
            "**Pocket Summary**\n"
            f"• Poké Balls: `{balls_total:,}`\n"
            f"• Held Items: `{held_total:,}`\n"
            f"• TMs: `{tm_total:,}`\n"
            f"• Medicine: `{medicine_total:,}`\n"
            f"• Key Items: `{key_item_total:,}`\n\n"
            f"• Trainer Box: `{trainer_box_count:,}`\n"
            f"• Holowear Tickets: `{holowear_ticket_count:,}`\n\n"
            "**Daycare**\n"
            f"• Eggs: `{egg_total}`\n"
            f"• Egg Energy: `{self.egg_energy(trainer):,}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "__Select a pocket below to view its contents.__"
        )
        return text, 0, 0


class PokemonRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_owned_pokemon(self, *, trainer: Trainer, data: dict) -> OwnedPokemon:
        pokemon = OwnedPokemon(
            trainer_id=trainer.id,
            species=data["species"],
            nickname=data.get("nickname"),
            origin_region=data.get("origin_region", trainer.current_region),
            source_kind=data.get("source_kind", "starter"),
            level=int(data["level"]),
            experience=int(data.get("experience", 0)),
            friendship=int(data["friendship"]),
            ability=data["ability"],
            nature=data["nature"],
            gender=data.get("gender", ""),
            item=data.get("item", ""),
            status=data.get("status", ""),
            tera_type=data.get("tera_type", ""),
            current_hp=int(data["current_hp"]),
            max_hp=int(data["max_hp"]),
            shiny=bool(data.get("shiny", False)),
            untradeable=bool(data.get("untradeable", False)),
            unreleasable=bool(data.get("unreleasable", False)),
            iv_hp=int(data["ivs"]["hp"]),
            iv_atk=int(data["ivs"]["atk"]),
            iv_def=int(data["ivs"]["def"]),
            iv_spa=int(data["ivs"]["spa"]),
            iv_spd=int(data["ivs"]["spd"]),
            iv_spe=int(data["ivs"]["spe"]),
            ev_hp=int(data["evs"]["hp"]),
            ev_atk=int(data["evs"]["atk"]),
            ev_def=int(data["evs"]["def"]),
            ev_spa=int(data["evs"]["spa"]),
            ev_spd=int(data["evs"]["spd"]),
            ev_spe=int(data["evs"]["spe"]),
            moves_json=data["moves_json"] if "moves_json" in data else json.dumps(data["moves"]),
            move_history_json=str(data.get("move_history_json") or "{}"),
            export_text=data["export_text"],
            packed_set=data["packed_set"],
            form_state_json=data.get("form_state_json"),
        )
        self.session.add(pokemon)
        self.session.flush()
        return pokemon

    def get_owned_pokemon(self, trainer: Trainer, pokemon_id: int) -> OwnedPokemon | None:
        return self.session.scalar(
            select(OwnedPokemon).where(
                OwnedPokemon.id == pokemon_id,
                OwnedPokemon.trainer_id == trainer.id,
            )
        )

    def list_owned_pokemon_by_ids(self, trainer: Trainer, pokemon_ids: Sequence[int]) -> list[OwnedPokemon]:
        ordered_ids = [int(pokemon_id) for pokemon_id in pokemon_ids]
        if not ordered_ids:
            return []
        rows = list(
            self.session.scalars(
                select(OwnedPokemon).where(
                    OwnedPokemon.trainer_id == trainer.id,
                    OwnedPokemon.id.in_(ordered_ids),
                )
            )
        )
        by_id = {int(pokemon.id): pokemon for pokemon in rows}
        return [by_id[pokemon_id] for pokemon_id in ordered_ids if pokemon_id in by_id]

    def find_by_query(self, trainer: Trainer, query: str) -> list[OwnedPokemon]:
        cleaned = normalize_lookup(query)
        if not cleaned:
            return []
        pokemon_list = self.list_owned_pokemon(trainer)

        def tokens(pokemon: OwnedPokemon) -> list[str]:
            values = [pokemon.species, effective_species(pokemon)]
            if pokemon.nickname:
                values.append(pokemon.nickname)
            return [normalize_lookup(value) for value in values if value]

        exact = [pokemon for pokemon in pokemon_list if cleaned in tokens(pokemon)]
        if exact:
            return exact
        partial = [
            pokemon
            for pokemon in pokemon_list
            if any(cleaned in token for token in tokens(pokemon))
        ]
        return partial

    def delete_owned_pokemon(self, pokemon: OwnedPokemon) -> None:
        self.session.delete(pokemon)
        self.session.flush()

    def clear_slots_for_pokemon(self, pokemon: OwnedPokemon) -> None:
        for slot in list(pokemon.party_slots):
            slot.pokemon = None
        for slot in list(pokemon.team_slots):
            slot.pokemon = None
        self.session.flush()

    def swap_trade_ownership(
        self,
        first: OwnedPokemon,
        second: OwnedPokemon,
        *,
        first_new_trainer: Trainer,
        second_new_trainer: Trainer,
        trainers: TrainerRepository | None = None,
    ) -> None:
        if first.id == second.id:
            raise ValueError("A trade requires two different Pokemon.")

        self.clear_slots_for_pokemon(first)
        self.clear_slots_for_pokemon(second)

        first.trainer_id = first_new_trainer.id
        first.trainer = first_new_trainer
        second.trainer_id = second_new_trainer.id
        second.trainer = second_new_trainer
        self.session.flush()

        if trainers is not None:
            trainers.place_in_first_party_slot(first_new_trainer, first)
            trainers.place_in_first_party_slot(second_new_trainer, second)
            self.session.flush()

    def list_owned_pokemon(self, trainer: Trainer, *, exclude_ids: set[int] | None = None) -> list[OwnedPokemon]:
        statement: Select[tuple[OwnedPokemon]] = select(OwnedPokemon).where(OwnedPokemon.trainer_id == trainer.id)
        if exclude_ids:
            statement = statement.where(OwnedPokemon.id.not_in(sorted(exclude_ids)))

        if trainer.sort_mode == SORT_NAME:
            statement = statement.order_by(asc(OwnedPokemon.species), asc(OwnedPokemon.id))
        elif trainer.sort_mode == SORT_LEVEL:
            statement = statement.order_by(desc(OwnedPokemon.level), asc(OwnedPokemon.species), asc(OwnedPokemon.id))
        elif trainer.sort_mode == SORT_IVS:
            statement = statement.order_by(
                desc(
                    OwnedPokemon.iv_hp
                    + OwnedPokemon.iv_atk
                    + OwnedPokemon.iv_def
                    + OwnedPokemon.iv_spa
                    + OwnedPokemon.iv_spd
                    + OwnedPokemon.iv_spe
                ),
                asc(OwnedPokemon.species),
                asc(OwnedPokemon.id),
            )
        else:
            statement = statement.order_by(asc(OwnedPokemon.id))

        return list(self.session.scalars(statement))

    def list_owned_page(
        self,
        trainer: Trainer,
        *,
        page: int,
        per_page: int,
        exclude_ids: set[int] | None = None,
    ) -> tuple[list[OwnedPokemon], int]:
        pokemon_list = self.list_owned_pokemon(trainer, exclude_ids=exclude_ids)
        total = len(pokemon_list)
        if total == 0:
            return [], 0
        start = max(page, 0) * per_page
        if start >= total:
            start = max((total - 1) // per_page, 0) * per_page
        end = start + per_page
        return pokemon_list[start:end], total

    def render_collection(self, trainer: Trainer, pokemon_list: Iterable[OwnedPokemon]) -> str:
        lines = [f"{trainer.display_name}'s Pokemon"]
        lines.append(f"Region: {trainer.current_region.title()}")
        lines.append(f"Display: {trainer.display_mode} | Sort: {trainer.sort_mode}")
        lines.append("")

        pokemon_items = list(pokemon_list)
        if not pokemon_items:
            lines.append("You do not own any Pokemon yet.")
            return "\n".join(lines)

        for index, pokemon in enumerate(pokemon_items, start=1):
            lines.append(f"{index}. {pokemon_display_label(pokemon, trainer.display_mode)}")
        return "\n".join(lines)

    def apply_condition_snapshot(self, pokemon: OwnedPokemon, *, current_hp: int | None, max_hp: int | None, status: str | None) -> None:
        if current_hp is not None:
            pokemon.current_hp = max(int(current_hp), 0)
        if max_hp is not None:
            pokemon.max_hp = max(int(max_hp), 1)
        if status is not None:
            pokemon.status = status

    def gain_exp(self, pokemon: OwnedPokemon, amount: int, data_service: Any) -> list[dict[str, Any]]:
        """Adds EXP to pokemon and handles leveling up. Returns a list of level-up events."""
        if pokemon.level >= 100:
            return []

        growth_rate = data_service.growth_rate(pokemon.species)
        current_floor = data_service.level_curve_value(growth_rate, pokemon.level)
        if pokemon.experience < current_floor:
            pokemon.experience = current_floor

        pokemon.experience += amount
        events = []

        while pokemon.level < 100:
            # The curve values are the TOTAL exp needed to BE at that level.
            # So to level up to (pokemon.level + 1), we need the curve value for (pokemon.level + 1).
            needed_for_next = data_service.level_curve_value(growth_rate, pokemon.level + 1)

            if pokemon.experience >= needed_for_next:
                old_level = pokemon.level
                old_stats = data_service.calculate_stats(pokemon)
                pokemon.level += 1

                # Recalculate stats
                old_max_hp = pokemon.max_hp
                stats = data_service.calculate_stats(pokemon)
                pokemon.max_hp = stats["hp"]
                # Heal by the amount of HP gained
                hp_gain = pokemon.max_hp - old_max_hp
                if hp_gain > 0:
                    pokemon.current_hp = min(pokemon.current_hp + hp_gain, pokemon.max_hp)

                events.append({
                    "type": "level_up",
                    "old_level": old_level,
                    "level": pokemon.level,
                    "species": pokemon.species,
                    "old_stats": old_stats,
                    "new_stats": stats,
                })
            else:
                break

        return events

    def _calculate_base_exp(self, wild_pokemon: OwnedPokemon, data_service: Any) -> int:
        base_exp = data_service.base_experience(wild_pokemon.species)
        return int((base_exp * wild_pokemon.level) / 7)

    def evolve_owned_pokemon(self, pokemon: OwnedPokemon, data: dict) -> None:
        old_max_hp = max(int(pokemon.max_hp), 1)
        old_current_hp = max(int(pokemon.current_hp), 0)
        new_max_hp = max(int(data["max_hp"]), 1)

        if old_current_hp <= 0:
            new_current_hp = 0
        else:
            hp_ratio = min(max(old_current_hp / old_max_hp, 0.0), 1.0)
            new_current_hp = max(1, min(new_max_hp, int(round(new_max_hp * hp_ratio))))

        pokemon.species = data["species"]
        pokemon.level = int(data["level"])
        pokemon.experience = int(data.get("experience", pokemon.experience))
        pokemon.friendship = int(data.get("friendship", pokemon.friendship))
        pokemon.ability = data["ability"]
        pokemon.nature = data["nature"]
        pokemon.gender = data.get("gender", pokemon.gender)
        pokemon.item = data.get("item", pokemon.item)
        pokemon.status = data.get("status", pokemon.status)
        pokemon.tera_type = str(data.get("tera_type") or pokemon.tera_type)
        pokemon.max_hp = new_max_hp
        pokemon.current_hp = new_current_hp
        pokemon.shiny = bool(data.get("shiny", pokemon.shiny))

        ivs = data.get("ivs") or {}
        evs = data.get("evs") or {}
        for stat in ("hp", "atk", "def", "spa", "spd", "spe"):
            if stat in ivs:
                setattr(pokemon, f"iv_{stat}", int(ivs[stat]))
            if stat in evs:
                setattr(pokemon, f"ev_{stat}", int(evs[stat]))

        pokemon.moves_json = data["moves_json"] if "moves_json" in data else json.dumps(data["moves"])
        pokemon.export_text = data["export_text"]
        pokemon.packed_set = data["packed_set"]
        self.session.flush()

    def sync_packed_set(self, pokemon: OwnedPokemon, data_service: Any) -> None:
        """Updates the packed_set and export_text field based on current stats and moves."""
        pokemon.packed_set = build_effective_packed_set(pokemon)
        pokemon.export_text = build_effective_export_text(pokemon)
        self.session.flush()


class TeamRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_team_presets(self, trainer: Trainer) -> list[TeamPreset]:
        existing = {
            team.slot_number: team
            for team in self.session.scalars(
                select(TeamPreset).where(TeamPreset.trainer_id == trainer.id).order_by(TeamPreset.slot_number)
            )
        }
        for slot_number in range(1, 7):
            if slot_number not in existing:
                self.session.add(
                    TeamPreset(
                        trainer_id=trainer.id,
                        slot_number=slot_number,
                        name=f"Team {slot_number}",
                        is_active=slot_number == 1 and not existing,
                    )
                )
        self.session.flush()

        teams = list(
            self.session.scalars(
                select(TeamPreset).where(TeamPreset.trainer_id == trainer.id).order_by(TeamPreset.slot_number)
            )
        )
        if not any(team.is_active for team in teams) and teams:
            teams[0].is_active = True

        for team in teams:
            self._ensure_team_slots(team)

        self.session.flush()
        return teams

    def _ensure_team_slots(self, team: TeamPreset) -> None:
        existing = {
            slot.slot_index
            for slot in self.session.scalars(
                select(TeamPresetSlot).where(TeamPresetSlot.team_id == team.id)
            )
        }
        for slot_index in range(1, 7):
            if slot_index not in existing:
                self.session.add(TeamPresetSlot(team_id=team.id, slot_index=slot_index))
        self.session.flush()

    def _seed_first_team_from_party_slots(self, trainer: Trainer, teams: Sequence[TeamPreset]) -> None:
        if not teams:
            return
        team_one = teams[0]
        team_slots = list(
            self.session.scalars(
                select(TeamPresetSlot).where(TeamPresetSlot.team_id == team_one.id).order_by(TeamPresetSlot.slot_index)
            )
        )
        if any(slot.pokemon_id for slot in team_slots):
            return
        party_slots = list(
            self.session.scalars(
                select(PartySlot).where(PartySlot.trainer_id == trainer.id).order_by(PartySlot.slot_index)
            )
        )
        for team_slot, party_slot in zip(team_slots, party_slots, strict=False):
            if party_slot.pokemon_id:
                team_slot.pokemon_id = party_slot.pokemon_id

    def list_teams(self, trainer: Trainer) -> list[TeamPreset]:
        return self.ensure_team_presets(trainer)

    def get_team(self, trainer: Trainer, slot_number: int) -> TeamPreset | None:
        self.ensure_team_presets(trainer)
        return self.session.scalar(
            select(TeamPreset).where(
                TeamPreset.trainer_id == trainer.id,
                TeamPreset.slot_number == slot_number,
            )
        )

    def get_active_team(self, trainer: Trainer) -> TeamPreset:
        self.ensure_team_presets(trainer)
        active = self.session.scalar(
            select(TeamPreset).where(
                TeamPreset.trainer_id == trainer.id,
                TeamPreset.is_active.is_(True),
            )
        )
        if active is not None:
            return active
        fallback = self.get_team(trainer, 1)
        assert fallback is not None
        fallback.is_active = True
        self.session.flush()
        return fallback

    def set_active_team(self, trainer: Trainer, slot_number: int) -> TeamPreset:
        teams = self.ensure_team_presets(trainer)
        selected: TeamPreset | None = None
        for team in teams:
            is_selected = team.slot_number == slot_number
            team.is_active = is_selected
            if is_selected:
                selected = team
        if selected is None:
            raise ValueError(f"Unknown team slot: {slot_number}")
        self.session.flush()
        return selected

    def team_slots(self, team: TeamPreset) -> list[TeamPresetSlot]:
        self._ensure_team_slots(team)
        return list(
            self.session.scalars(
                select(TeamPresetSlot)
                .options(selectinload(TeamPresetSlot.pokemon))
                .where(TeamPresetSlot.team_id == team.id)
                .order_by(TeamPresetSlot.slot_index)
            )
        )

    def team_members(self, team: TeamPreset) -> list[OwnedPokemon | None]:
        return [slot.pokemon for slot in self.team_slots(team)]

    def team_member_ids(self, team: TeamPreset) -> set[int]:
        return {slot.pokemon_id for slot in self.team_slots(team) if slot.pokemon_id is not None}

    def assign_pokemon(self, team: TeamPreset, slot_index: int, pokemon: OwnedPokemon) -> None:
        slots = self.team_slots(team)
        target: TeamPresetSlot | None = None
        for slot in slots:
            if slot.slot_index == slot_index:
                target = slot
            if slot.pokemon_id == pokemon.id and slot.slot_index != slot_index:
                slot.pokemon = None
        if target is None:
            raise ValueError(f"Unknown team slot: {slot_index}")
        target.pokemon = pokemon
        self.session.flush()

    def place_in_first_open_slot(self, team: TeamPreset, pokemon: OwnedPokemon) -> None:
        for slot in self.team_slots(team):
            if slot.pokemon_id is None:
                self.assign_pokemon(team, slot.slot_index, pokemon)
                return
        self.assign_pokemon(team, 1, pokemon)

    def remove_slot(self, team: TeamPreset, slot_index: int) -> None:
        target = next((slot for slot in self.team_slots(team) if slot.slot_index == slot_index), None)
        if target is None:
            raise ValueError(f"Unknown team slot: {slot_index}")
        target.pokemon = None
        self.session.flush()

    def swap_slots(self, team: TeamPreset, first_slot: int, second_slot: int) -> None:
        if first_slot == second_slot:
            return
        slots = {slot.slot_index: slot for slot in self.team_slots(team)}
        left = slots.get(first_slot)
        right = slots.get(second_slot)
        if left is None or right is None:
            raise ValueError("Invalid swap slot.")
        left.pokemon_id, right.pokemon_id = right.pokemon_id, left.pokemon_id
        self.session.flush()

    def clear_team(self, team: TeamPreset) -> None:
        for slot in self.team_slots(team):
            slot.pokemon = None
        self.session.flush()

    def randomize_team(self, team: TeamPreset, pokemon_pool: Sequence[OwnedPokemon]) -> None:
        chosen = random.sample(list(pokemon_pool), k=min(6, len(pokemon_pool))) if pokemon_pool else []
        slots = self.team_slots(team)
        for index, slot in enumerate(slots):
            slot.pokemon = chosen[index] if index < len(chosen) else None
        self.session.flush()

    def rename_team(self, team: TeamPreset, name: str) -> None:
        cleaned = " ".join(name.strip().split())
        if not cleaned:
            raise ValueError("Team name cannot be empty.")
        team.name = cleaned[:64]
        self.session.flush()

    def render_team_overview(self, trainer: Trainer, teams: Sequence[TeamPreset]) -> str:
        active_team = next((team for team in teams if team.is_active), None)
        lines = ["Your Teams"]
        lines.append(f"Active team: {active_team.slot_number if active_team else 1}")
        lines.append("")
        if active_team is not None:
            lines.extend(self.render_team_lines(active_team))
        return "\n".join(lines)

    def render_team_detail(self, team: TeamPreset) -> str:
        header = f"{team.name}"
        if team.is_active:
            header += " [ ACTIVE ]"
        lines = [header, ""]
        lines.extend(self.render_team_lines(team))
        return "\n".join(lines)

    def render_team_lines(self, team: TeamPreset) -> list[str]:
        lines: list[str] = []
        for slot in self.team_slots(team):
            if slot.pokemon is None:
                lines.append(f"{slot.slot_index}. -")
            else:
                lines.append(f"{slot.slot_index}. {effective_species(slot.pokemon)} Lv. {slot.pokemon.level}")
        return lines

    def build_packed_team(self, team: TeamPreset) -> str:
        members = [slot.pokemon for slot in self.team_slots(team) if slot.pokemon is not None]
        return "]".join(build_effective_packed_set(pokemon) for pokemon in members)


def pokemon_list_label(pokemon: OwnedPokemon, display_mode: str) -> str:
    base = f"{pokemon.species}{' ✨' if pokemon.shiny else ''}"
    if display_mode == DISPLAY_NONE:
        return base
    if display_mode == DISPLAY_LEVEL:
        return f"{base} lv {pokemon.level}"
    if display_mode == DISPLAY_NATURE:
        return f"{base} - {pokemon.nature}"
    if display_mode == DISPLAY_CATEGORY:
        return f"{base} - {pokemon.source_kind.replace('_', ' ').title()}"
    if display_mode == DISPLAY_IVS:
        return f"{base} - iv {pokemon.total_iv}"
    if display_mode == DISPLAY_EVS:
        return f"{base} - ev {pokemon_total_ev(pokemon)}"
    return f"{base} lv {pokemon.level}"


def pokemon_list_label(pokemon: OwnedPokemon, display_mode: str) -> str:
    egg_icon = " 🥚" if str(pokemon.source_kind or "").strip().lower() == "egg" else ""
    shiny_icon = " ✨" if pokemon.shiny else ""
    base = f"{pokemon.species}{egg_icon}{shiny_icon}"
    if display_mode == DISPLAY_NONE:
        return base
    if display_mode == DISPLAY_LEVEL:
        return f"{base} lv {pokemon.level}"
    if display_mode == DISPLAY_NATURE:
        return f"{base} - {pokemon.nature}"
    if display_mode == DISPLAY_CATEGORY:
        return f"{base} - {pokemon.source_kind.replace('_', ' ').title()}"
    if display_mode == DISPLAY_IVS:
        return f"{base} - iv {pokemon.total_iv}"
    if display_mode == DISPLAY_EVS:
        return f"{base} - ev {pokemon_total_ev(pokemon)}"
    return f"{base} lv {pokemon.level}"
