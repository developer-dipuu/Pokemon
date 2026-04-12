from __future__ import annotations

from telethon.errors import MessageNotModifiedError, RPCError
from telethon.tl.types import User


async def safe_event_edit(event, text: str, *, buttons=None, parse_mode=None, link_preview: bool = False) -> bool:
    try:
        await event.edit(
            text,
            buttons=buttons,
            parse_mode=parse_mode,
            link_preview=link_preview,
        )
        return True
    except MessageNotModifiedError:
        return False
    except RPCError:
        return False


async def safe_client_edit(
    client,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    buttons=None,
    parse_mode=None,
    link_preview: bool = False,
) -> bool:
    try:
        await client.edit_message(
            chat_id,
            message_id,
            text,
            buttons=buttons,
            parse_mode=parse_mode,
            link_preview=link_preview,
        )
        return True
    except MessageNotModifiedError:
        return False
    except RPCError:
        return False


async def resolve_event_user(event) -> User | None:
    sender = getattr(event, "sender", None)
    if isinstance(sender, User):
        return sender
    try:
        sender = await event.get_sender()
    except Exception:
        return None
    return sender if isinstance(sender, User) else None
