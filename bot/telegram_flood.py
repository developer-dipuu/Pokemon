from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from types import MethodType
from typing import Any, Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SlowModeWaitError

logger = logging.getLogger("PokemonBot")


def _load_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    return max(0.0, value)


def _load_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    return max(1, value)


class TelegramFloodController:
    def __init__(
        self,
        *,
        global_interval: float,
        per_chat_interval: float,
        retry_limit: int,
        flood_backoff_seconds: float,
        cushion_seconds: float = 0.25,
    ) -> None:
        self.global_interval = global_interval
        self.per_chat_interval = per_chat_interval
        self.retry_limit = retry_limit
        self.flood_backoff_seconds = flood_backoff_seconds
        self.cushion_seconds = cushion_seconds
        self._global_lock = asyncio.Lock()
        self._chat_locks: defaultdict[Any, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_global_at = 0.0
        self._next_chat_at: dict[Any, float] = {}

    async def _wait_for_turn(self, chat_key: Any) -> None:
        loop = asyncio.get_running_loop()
        async with self._global_lock:
            now = loop.time()
            wait = max(0.0, self._next_global_at - now)
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_global_at = max(self._next_global_at, now) + self.global_interval

        if chat_key is None:
            return

        async with self._chat_locks[chat_key]:
            now = loop.time()
            wait = max(0.0, self._next_chat_at.get(chat_key, 0.0) - now)
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_chat_at[chat_key] = max(self._next_chat_at.get(chat_key, 0.0), now) + self.per_chat_interval

    async def _apply_wait(self, chat_key: Any, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        target = loop.time() + max(0.0, seconds) + self.cushion_seconds
        async with self._global_lock:
            self._next_global_at = max(self._next_global_at, target)
        if chat_key is not None:
            async with self._chat_locks[chat_key]:
                self._next_chat_at[chat_key] = max(self._next_chat_at.get(chat_key, 0.0), target)

    async def call(
        self,
        method_name: str,
        chat_key: Any,
        fn: Callable[..., Awaitable[Any]],
        *args,
        **kwargs,
    ) -> Any:
        for attempt in range(1, self.retry_limit + 1):
            await self._wait_for_turn(chat_key)
            try:
                return await fn(*args, **kwargs)
            except SlowModeWaitError as exc:
                wait_seconds = max(float(getattr(exc, "seconds", 1) or 1), self.flood_backoff_seconds)
                logger.warning(
                    "Telegram slow mode triggered during %s for %s. Waiting %.2fs before retry %s/%s.",
                    method_name,
                    chat_key,
                    wait_seconds,
                    attempt,
                    self.retry_limit,
                )
            except FloodWaitError as exc:
                wait_seconds = max(float(getattr(exc, "seconds", 1) or 1), self.flood_backoff_seconds)
                logger.warning(
                    "Telegram flood wait triggered during %s for %s. Waiting %.2fs before retry %s/%s.",
                    method_name,
                    chat_key,
                    wait_seconds,
                    attempt,
                    self.retry_limit,
                )
            await self._apply_wait(chat_key, wait_seconds)
            await asyncio.sleep(wait_seconds + self.cushion_seconds)
        return await fn(*args, **kwargs)


def _entity_key(entity: Any) -> Any:
    if entity is None:
        return None
    if isinstance(entity, (int, str)):
        return entity
    for attr in ("chat_id", "channel_id", "user_id", "id"):
        value = getattr(entity, attr, None)
        if value is not None:
            return value
    return None


def _chat_key_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if args:
        return _entity_key(args[0])
    for key in ("entity", "chat_id", "peer"):
        value = kwargs.get(key)
        if value is not None:
            return _entity_key(value)
    return None


def install_telegram_flood_control(client: TelegramClient) -> TelegramFloodController:
    if getattr(client, "_pokeplay_flood_controller", None) is not None:
        return client._pokeplay_flood_controller

    controller = TelegramFloodController(
        global_interval=_load_float("TELEGRAM_GLOBAL_WRITE_INTERVAL", 0.2),
        per_chat_interval=_load_float("TELEGRAM_PER_CHAT_WRITE_INTERVAL", 0.2),
        retry_limit=_load_int("TELEGRAM_FLOOD_RETRY_LIMIT", 5),
        flood_backoff_seconds=_load_float("TELEGRAM_FLOOD_BACKOFF_SECONDS", 2.0),
    )

    def wrap(method_name: str) -> None:
        original = getattr(client, method_name)

        async def wrapped(self, *args, **kwargs):
            chat_key = _chat_key_from_args(args, kwargs)
            return await controller.call(method_name, chat_key, original, *args, **kwargs)

        setattr(client, method_name, MethodType(wrapped, client))

    for method_name in ("send_message", "send_file", "edit_message", "delete_messages", "forward_messages"):
        wrap(method_name)

    client._pokeplay_flood_controller = controller
    logger.info(
        "Installed Telegram flood control: global_interval=%.2fs, per_chat_interval=%.2fs, retries=%s",
        controller.global_interval,
        controller.per_chat_interval,
        controller.retry_limit,
    )
    return controller
