from __future__ import annotations

import time
import asyncio
import sys
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
from collections import defaultdict, deque
from contextlib import contextmanager

logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("PokemonBot")
logging.getLogger("telethon.crypto.libssl").setLevel(logging.WARNING)

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient, events
from telethon.errors import QueryIdInvalidError

from bot.battle.service import BattleService
from bot.config import (
    ADMIN_USER_ID_SET,
    SESSION_PATH,
    ensure_runtime_dirs,
    load_api_hash,
    load_api_id,
    load_bot_token,
)
from bot.db.repositories import AdminRepository, TrainerRepository
from bot.db.session import init_database_async, run_db_work_async
from bot.game.services.generator import PokemonGeneratorService
from bot.game.services.trainer import TrainerGameService
from bot.cache.redis_client import get_redis_client, ping_redis
from bot.telegram_flood import install_telegram_flood_control
from telethon import Button
from telethon.events import StopPropagation
from telethon.sessions.sqlite import SQLiteSession


_ORIGINAL_CALLBACK_ANSWER = events.CallbackQuery.Event.answer


async def _safe_telethon_callback_answer(self, *args, **kwargs):
    try:
        return await _ORIGINAL_CALLBACK_ANSWER(self, *args, **kwargs)
    except QueryIdInvalidError:
        logger.debug("Skipped expired callback answer for data=%r", getattr(self, "data", None))
        return None


events.CallbackQuery.Event.answer = _safe_telethon_callback_answer


async def safe_callback_answer(event: events.CallbackQuery.Event, *args, **kwargs) -> bool:
    try:
        await event.answer(*args, **kwargs)
        return True
    except QueryIdInvalidError:
        logger.debug("Skipped expired callback answer for data=%r", getattr(event, "data", None))
        return False


async def start_client_with_retry(client: TelegramClient, *, bot_token: str, retries: int = 5) -> None:
    for attempt in range(1, retries + 1):
        try:
            await client.start(bot_token=bot_token)
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            if attempt >= retries:
                logger.error(
                    "Telethon session database stayed locked after %s attempts. "
                    "Close any duplicate bot process using %s and retry.",
                    retries,
                    SESSION_PATH,
                )
                raise
            wait_seconds = min(5, attempt)
            logger.warning(
                "Telethon session database is locked (attempt %s/%s). Retrying in %ss.",
                attempt,
                retries,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)


@contextmanager
def telethon_session_lock():
    lock_path = SESSION_PATH.with_name(f"{SESSION_PATH.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(
                    f"Another bot process is already using {SESSION_PATH}. "
                    f"Stop the duplicate process or remove stale lock {lock_path}."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(
                    f"Another bot process is already using {SESSION_PATH}. "
                    f"Stop the duplicate process or remove stale lock {lock_path}."
                ) from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _backup_path(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    index = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak{index}")
        index += 1
    return path.replace(backup)


def ensure_valid_telethon_session() -> None:
    session_file = SESSION_PATH.with_suffix(".session")
    journal_file = session_file.with_name(session_file.name + "-journal")
    if not session_file.exists():
        return
    try:
        SQLiteSession(str(SESSION_PATH))
    except Exception as exc:
        logger.warning(
            "Detected an invalid or incompatible Telethon session file %s: %s",
            session_file,
            exc,
        )
        backup = _backup_path(session_file)
        logger.warning("Backed up invalid session file to %s", backup)
        if journal_file.exists():
            backup_journal = _backup_path(journal_file)
            logger.warning("Backed up stale journal file to %s", backup_journal)


async def main() -> None:
    ensure_runtime_dirs()
    ensure_valid_telethon_session()
    await init_database_async()
    startup_cutoff_utc = datetime.now(timezone.utc)

    client = TelegramClient(str(SESSION_PATH), load_api_id(), load_api_hash())
    install_telegram_flood_control(client)
    battle_service = BattleService(client)
    game_service = TrainerGameService(PokemonGeneratorService(), battle_service)
    battle_service.attach_encounter_service(game_service.encounters)
    bot_dm_url: str | None = None
    bot_dm_url_lock = asyncio.Lock()

    try:
        redis_available = await ping_redis()
    except Exception as exc:
        redis_available = False
        logger.warning("Redis unavailable, falling back to in-memory state: %s", exc)
    if not redis_available:
        logger.info("Running without Redis caching or flood control fallback.")
    trainer_exists_cache: dict[int, float] = {}
    banned_cache: dict[int, tuple[bool, float]] = {}
    tracked_group_chats: set[int] = set()

    async def get_bot_dm_url() -> str | None:
        nonlocal bot_dm_url
        if bot_dm_url is not None:
            return bot_dm_url
        async with bot_dm_url_lock:
            if bot_dm_url is None:
                me = await client.get_me()
                username = str(getattr(me, "username", "") or "").strip()
                bot_dm_url = f"https://t.me/{username}?start=start" if username else None
        return bot_dm_url

    async def trainer_exists(user_id: int | None) -> bool:
        if user_id is None:
            return False
        trainer_id = int(user_id)
        local_expires = trainer_exists_cache.get(trainer_id)
        if local_expires is not None and local_expires > time.monotonic():
            return True

        if redis_available:
            try:
                redis_key = f"trainer_exists:{trainer_id}"
                value = await redis_client.get(redis_key)
                if value is not None:
                    trainer_exists_cache[trainer_id] = time.monotonic() + 60.0
                    return value == "1"
            except Exception:
                pass

        trainer = await run_db_work_async(
            lambda session: TrainerRepository(session).get_by_telegram_user_id(trainer_id),
            read_only=True,
        )
        if trainer is not None:
            trainer_exists_cache[trainer_id] = time.monotonic() + 60.0
            if redis_available:
                try:
                    await redis_client.set(redis_key, "1", ex=60)
                except Exception:
                    pass
            return True
        trainer_exists_cache.pop(trainer_id, None)
        if redis_available:
            try:
                await redis_client.set(redis_key, "0", ex=60)
            except Exception:
                pass
        return False

    async def is_banned_user(user_id: int | None) -> bool:
        if user_id is None:
            return False
        trainer_id = int(user_id)
        if trainer_id in ADMIN_USER_ID_SET:
            return False
        cached = banned_cache.get(trainer_id)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]

        if redis_available:
            try:
                redis_key = f"trainer_banned:{trainer_id}"
                value = await redis_client.get(redis_key)
                if value is not None:
                    banned = value == "1"
                    banned_cache[trainer_id] = (banned, now + 60.0)
                    return banned
            except Exception:
                pass

        banned = await run_db_work_async(
            lambda session: AdminRepository(session).is_banned_user(trainer_id),
            read_only=True,
        )
        result = bool(banned)
        banned_cache[trainer_id] = (result, now + 60.0)
        if redis_available:
            try:
                await redis_client.set(redis_key, "1" if result else "0", ex=60)
            except Exception:
                pass
        return result

    async def track_group_chat(chat_id: int | None, *, title: str | None = None) -> None:
        if chat_id is None:
            return
        value = int(chat_id)
        if value >= 0:
            return
        if value in tracked_group_chats:
            return
        await run_db_work_async(
            lambda session: AdminRepository(session).track_group_chat(value, title=title),
            read_only=False,
        )
        tracked_group_chats.add(value)

    # =================================================================
    # GLOBAL AUTH MIDDLEWARE (COMMAND INTERCEPTOR)
    # =================================================================
    @client.on(events.NewMessage(pattern=r"^/.*"))  # <-- FIXED REGEX HERE
    async def auth_interceptor(event):
        # Safety check to ignore messages without a sender
        if not event.sender_id:
            return
        if not event.is_private:
            chat = getattr(event, "chat", None)
            title = str(getattr(chat, "title", "") or "").strip() or None
            await track_group_chat(event.chat_id, title=title)
        msg = getattr(event, "message", None)
        msg_date = getattr(msg, "date", None)
        if msg_date is not None:
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < (startup_cutoff_utc - timedelta(seconds=1)):
                logger.debug("Ignoring stale command from %s: %s", event.sender_id, event.raw_text)
                raise StopPropagation

        if await is_banned_user(event.sender_id):
            await event.respond("You are banned from using this bot.")
            raise StopPropagation

        is_start_command = event.raw_text.lower().startswith("/start")
        if await trainer_exists(event.sender_id):
            return
        # If they have no profile, they MUST use /start AND it MUST be in a private DM.
        # If either of those is false, block them and send the DM link.
        if not (is_start_command and event.is_private):
            url = await get_bot_dm_url()
                    
            await event.respond(
                        "⚠️ **Trainer Profile Not Found**\n\n"
                        "Your Pokémon journey hasn't started yet! Please start the bot in my DMs to pick your first partner.",
                        buttons=[[Button.url("🚀 Start Adventure", url)]],
                        parse_mode="md"
            )
            # This completely halts execution so the real command never runs
            raise StopPropagation
    # =================================================================

    # --- ALL YOUR COMMAND HANDLERS ---
    client.add_event_handler(game_service.on_start, events.NewMessage(pattern=r"^/start(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_help, events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
    client.add_event_handler(game_service.on_starter, events.NewMessage(pattern=r"^/starter(?:@\w+)?$"))
    client.add_event_handler(game_service.on_my_pokemons, events.NewMessage(pattern=r"^/mypokemons(?:@\w+)?$"))
    client.add_event_handler(game_service.on_nickname, events.NewMessage(pattern=r"^/nickname(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_my_nicknames, events.NewMessage(pattern=r"^/mynicknames(?:@\w+)?$"))
    client.add_event_handler(game_service.on_myteam, events.NewMessage(pattern=r"^/myteam(?:@\w+)?$"))
    client.add_event_handler(game_service.on_display, events.NewMessage(pattern=r"^/display(?:@\w+)?(?:\s+\S+)?$"))
    client.add_event_handler(game_service.on_sort, events.NewMessage(pattern=r"^/sort(?:@\w+)?(?:\s+\S+)?$"))
    client.add_event_handler(game_service.on_mybag, events.NewMessage(pattern=r"^/mybag(?:@\w+)?$"))
    client.add_event_handler(game_service.on_box, events.NewMessage(pattern=r"^/box(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_equip_items, events.NewMessage(pattern=r"^/equip_items(?:@\w+)?$"))
    client.add_event_handler(game_service.on_equip_items, events.NewMessage(pattern=r"^/equip_item(?:@\w+)?$"))
    client.add_event_handler(game_service.on_train, events.NewMessage(pattern=r"^/train(?:@\w+)?$"))
    client.add_event_handler(game_service.on_breed, events.NewMessage(pattern=r"^/breed(?:@\w+)?$"))
    client.add_event_handler(game_service.on_breeddata, events.NewMessage(pattern=r"^/breeddata(?:@\w+)?$"))
    client.add_event_handler(game_service.on_incubate, events.NewMessage(pattern=r"^/incubate(?:@\w+)?$"))
    client.add_event_handler(game_service.on_incubator, events.NewMessage(pattern=r"^/incubator(?:@\w+)?$"))
    client.add_event_handler(game_service.on_forcecomplete, events.NewMessage(pattern=r"^/forcecomplete(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_mycard, events.NewMessage(pattern=r"^/mycard(?:@\w+)?$"))
    client.add_event_handler(game_service.on_mochi, events.NewMessage(pattern=r"^/mochi(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_candy, events.NewMessage(pattern=r"^/candy(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_feather, events.NewMessage(pattern=r"^/feather(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_mint, events.NewMessage(pattern=r"^/mint(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_abilitypatch, events.NewMessage(pattern=r"^/abilitypatch(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_abilitypatch, events.NewMessage(pattern=r"^/ability_patch(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_abilitycapsule, events.NewMessage(pattern=r"^/abilitycapsule(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_abilitycapsule, events.NewMessage(pattern=r"^/ability_capsule(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_relearner, events.NewMessage(pattern=r"^/relearner(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_bottlecap, events.NewMessage(pattern=r"^/bottlecap(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_bottlecap, events.NewMessage(pattern=r"^/bottle_cap(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_goldbottlecap, events.NewMessage(pattern=r"^/goldbottlecap(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_goldbottlecap, events.NewMessage(pattern=r"^/gold_bottle_cap(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_maxsoup, events.NewMessage(pattern=r"^/maxsoup(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_formchange, events.NewMessage(pattern=r"^/formchange(?:@\w+)?$"))
    client.add_event_handler(game_service.on_travel, events.NewMessage(pattern=r"^/travel(?:@\w+)?$"))
    client.add_event_handler(game_service.on_dexnav, events.NewMessage(pattern=r"^/dexnav(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_create, events.NewMessage(pattern=r"^/create(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_deletefac, events.NewMessage(pattern=r"^/deletefac(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_setgc, events.NewMessage(pattern=r"^/setgc(?:@\w+)?$"))
    client.add_event_handler(game_service.on_myfac, events.NewMessage(pattern=r"^/myfac(?:@\w+)?$"))
    client.add_event_handler(game_service.on_faclb, events.NewMessage(pattern=r"^/faclb(?:@\w+)?$"))
    client.add_event_handler(game_service.on_join, events.NewMessage(pattern=r"^/join(?:@\w+)?$"))
    client.add_event_handler(game_service.on_leave, events.NewMessage(pattern=r"^/leave(?:@\w+)?$"))
    client.add_event_handler(game_service.on_fac_link, events.NewMessage(pattern=r"^/fac_link(?:@\w+)?$"))
    client.add_event_handler(game_service.on_kick_member, events.NewMessage(pattern=r"^/kick_member(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_facpromote, events.NewMessage(pattern=r"^/facpromote(?:@\w+)?$"))
    client.add_event_handler(game_service.on_facdemote, events.NewMessage(pattern=r"^/facdemote(?:@\w+)?$"))
    client.add_event_handler(game_service.on_setpfp, events.NewMessage(pattern=r"^/setpfp(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_setname, events.NewMessage(pattern=r"^/setname(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_fac_deposit, events.NewMessage(pattern=r"^/fac_deposit(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_fac_deposit, events.NewMessage(pattern=r"^/facdeposit(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_pokechain, events.NewMessage(pattern=r"^/pokechain(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_joinpc, events.NewMessage(pattern=r"^/joinpc(?:@\w+)?$"))
    client.add_event_handler(game_service.on_open, events.NewMessage(pattern=r"^/open(?:@\w+)?$"))
    client.add_event_handler(game_service.on_close, events.NewMessage(pattern=r"^/close(?:@\w+)?$"))
    client.add_event_handler(game_service.on_hunt, events.NewMessage(pattern=r"^/hunt(?:@\w+)?$"))
    client.add_event_handler(game_service.on_autohunt, events.NewMessage(pattern=r"^/autohunt(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_safari, events.NewMessage(pattern=r"^/safari(?:@\w+)?$"))
    client.add_event_handler(game_service.on_sreset, events.NewMessage(pattern=r"^/sreset(?:@\w+)?$"))
    client.add_event_handler(battle_service.on_gym_command, events.NewMessage(pattern=r"^/gym(?:@\w+)?$"))
    client.add_event_handler(game_service.on_stats, events.NewMessage(pattern=r"^/stats(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_tm, events.NewMessage(pattern=r"^/tm(?:\d+)?(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_stones, events.NewMessage(pattern=r"^/stones(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_megastone, events.NewMessage(pattern=r"^/megastone(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_megastone_alias, events.NewMessage(pattern=r"(?i)^/[a-z0-9-]*ite(?:[xyz]|-[xyz])?(?:@\w+)?$"))
    client.add_event_handler(game_service.on_zstone, events.NewMessage(pattern=r"^/zstone(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_add, events.NewMessage(pattern=r"^/add(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_listmoveid, events.NewMessage(pattern=r"^/listmoveid(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_addballs, events.NewMessage(pattern=r"^/addballs(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_addallitem, events.NewMessage(pattern=r"^/addallitem(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_clear_db, events.NewMessage(pattern=r"^/cleardb(?:@\w+)?$"))
    client.add_event_handler(game_service.on_shop, events.NewMessage(pattern=r"^/shop(?:@\w+)?$"))
    client.add_event_handler(game_service.o_buy, events.NewMessage(pattern=r"^/buy(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_send, events.NewMessage(pattern=r"^/send(?:@\w+)?(?:\s+\d+)?$"))
    client.add_event_handler(game_service.on_trade, events.NewMessage(pattern=r"^/trade(?:@\w+)?$"))
    client.add_event_handler(game_service.on_sell, events.NewMessage(pattern=r"^/sell(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_addvp, events.NewMessage(pattern=r"^/addvp(?:@\w+)?$"))
    client.add_event_handler(game_service.on_addsp, events.NewMessage(pattern=r"^/sp(?:@\w+)?$"))
    client.add_event_handler(game_service.on_addlp, events.NewMessage(pattern=r"^/addlp(?:@\w+)?$"))
    client.add_event_handler(game_service.on_rankup, events.NewMessage(pattern=r"^/rankup(?:@\w+)?(?:\s+[+-]?\d+)?$"))
    client.add_event_handler(game_service.on_resetrank, events.NewMessage(pattern=r"^/resetrank(?:@\w+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/additem(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/removeitem(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/weekendboost(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/weekendmode(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/fdc(?:@\w+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/makeredeem(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/synccommands(?:@\w+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/setcommands(?:@\w+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/lock(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/unlock(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/locked(?:@\w+)?$"))
    client.add_event_handler(game_service.on_status, events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
    client.add_event_handler(game_service.on_record, events.NewMessage(pattern=r"^/record(?:@\w+)?$"))
    client.add_event_handler(game_service.on_top, events.NewMessage(pattern=r"^/top(?:@\w+)?$"))
    client.add_event_handler(game_service.on_redeem, events.NewMessage(pattern=r"^/redeem(?:@\w+)?(?:\s+.+)?$"))
    client.add_event_handler(game_service.on_admin_legacy, events.NewMessage(pattern=r"^/reset_battle(?:@\w+)?$"))
    client.add_event_handler(
        game_service.on_admin_legacy,
        events.NewMessage(
            pattern=r"^/(?:addexp|add_band|bfb|broad|change|id_transfer|reloadcache|remove|reset|set|setability|setmove|treset|ufb|dailyreset|assignability|assignmove|assignitems|convert|reverseconvert|reset_all)(?:@\w+)?(?:\s+.+)?$"
        ),
    )
    client.add_event_handler(game_service.on_battlepass, events.NewMessage(pattern=r"^/battlepass(?:@\w+)?$"))
    client.add_event_handler(battle_service.on_exit_command, events.NewMessage(pattern=r"^/exit(?:@\w+)?$"))
    client.add_event_handler(
        game_service.on_private_text,
        events.NewMessage(func=lambda e: bool(e.is_private and e.raw_text and not e.raw_text.startswith("/"))),
    )
    client.add_event_handler(
        game_service.on_group_text,
        events.NewMessage(func=lambda e: bool((not e.is_private) and e.raw_text and not e.raw_text.startswith("/"))),
    )
    client.add_event_handler(battle_service.on_challenge_command, events.NewMessage(pattern=r"^/challenge(?:@\w+)?$"))
    client.add_event_handler(battle_service.on_battle_stats_command, events.NewMessage(pattern=r"^/battle_stats(?:@\w+)?$"))
    client.add_event_handler(battle_service.on_test_battle_image_command, events.NewMessage(pattern=r"^/trstbimage(?:@\w+)?(?:\s+.+)?$"))

    # =================================================================
    # MASTER INLINE BUTTON PROTECTOR AND DISPATCHER
    # =================================================================
    callback_click_history: dict[int, deque[float]] = defaultdict(deque)
    callback_block_until: dict[int, float] = {}
    FLOOD_WINDOW_SECONDS = 1.0
    FLOOD_BURST_LIMIT = 5
    FLOOD_BLOCK_SECONDS = 1.0
    redis_client = get_redis_client()

    async def on_callback(event: events.CallbackQuery.Event) -> None:
        if event.sender_id is not None:
            now = asyncio.get_running_loop().time()
            if not event.is_private:
                await track_group_chat(event.chat_id)

            if await is_banned_user(event.sender_id):
                await safe_callback_answer(event, "You are banned from using this bot.", alert=True)
                return

            history_key = f"callback_history:{event.sender_id}"
            block_key = f"callback_block:{event.sender_id}"
            use_redis = redis_available

            if use_redis:
                try:
                    if await redis_client.exists(block_key):
                        await safe_callback_answer(event, "Loading... Please wait.", alert=False)
                        return

                    count = await redis_client.incr(history_key)
                    if count == 1:
                        await redis_client.expire(history_key, int(FLOOD_WINDOW_SECONDS))

                    if count >= FLOOD_BURST_LIMIT:
                        await redis_client.set(block_key, 1, ex=int(FLOOD_BLOCK_SECONDS))
                        await redis_client.delete(history_key)
                        await safe_callback_answer(event, "Loading... Please wait.", alert=False)
                        return
                except Exception:
                    use_redis = False

            if not use_redis:
                # 1. Apply temporary block only after real burst/flood activity.
                if now < callback_block_until.get(event.sender_id, 0.0):
                    await safe_callback_answer(event, "Loading... Please wait.", alert=False)
                    return

                # 2. Track clicks in a short rolling window.
                history = callback_click_history[event.sender_id]
                while history and (now - history[0]) > FLOOD_WINDOW_SECONDS:
                    history.popleft()
                history.append(now)

                # 3. Trigger flood protection only when rapid spam is detected.
                if len(history) >= FLOOD_BURST_LIMIT:
                    callback_block_until[event.sender_id] = now + FLOOD_BLOCK_SECONDS
                    history.clear()
                    await safe_callback_answer(event, "Loading... Please wait.", alert=False)
                    return

            # 4. Global Auth Check for Buttons
            if not await trainer_exists(event.sender_id):
                url = await get_bot_dm_url()
                    
                # Send a toast alert telling them why it failed
                await safe_callback_answer(event, "Start the bot first!", alert=True)
                    
                # Optionally, drop a new message in the chat with the start button
                await client.send_message(
                    event.chat_id,
                    "You must create a Trainer Profile before interacting with this menu.",
                        buttons=[[Button.url("🚀 Start Bot", url)]]
                )
                return # Stop processing the button click

        # 5. Pass the event to your game services
        try:
            if await game_service.handle_callback(event):
                return
            if await battle_service.handle_callback(event):
                return
            await safe_callback_answer(event, "Unknown button.", alert=True)
        except QueryIdInvalidError:
            logger.debug("Ignoring expired callback query for data=%r", getattr(event, "data", None))
            return

    # Register the master dispatcher
    client.add_event_handler(on_callback, events.CallbackQuery)
    # =================================================================

    await start_client_with_retry(client, bot_token=load_bot_token())
    await battle_service.restore_recent_battles()
    game_service.start_background_tasks()
    me = await client.get_me()
    bot_dm_url = f"https://t.me/{me.username}?start=start" if getattr(me, "username", None) else None
    print(f"Bot is running as @{me.username or 'unknown_bot'}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        with telethon_session_lock():
            asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")
