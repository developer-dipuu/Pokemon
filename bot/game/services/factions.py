from __future__ import annotations

import asyncio
from datetime import datetime
import html
import json
import os
import re
from pathlib import Path
from typing import Any

from telethon import Button
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.types import User

from bot.config import BOT_DIR
from bot.db.repositories import InventoryRepository, TrainerRepository
from bot.db.session import run_db_work_async

DEFAULT_PFP = "https://files.catbox.moe/5rg0pw.jpg"
DEFAULT_ADMIN_IDS = {6265981509, 7577674783, 6856118779}
DEFAULT_CHANNEL_ID = -1003707195144
DIVIDER = "---------------------"


def _name_from_user(user: User | None, fallback: str = "Trainer") -> str:
    if user is None:
        return fallback
    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip()
    combined = " ".join(part for part in (first, last) if part).strip()
    if combined:
        return combined
    if username:
        return username
    return fallback


def _esc(text: Any) -> str:
    return html.escape(str(text or ""))


def _mention(user_id: int | str, name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{_esc(name)}</a>'


class FactionService:
    def __init__(self, client) -> None:
        self.client = client
        self._lock = asyncio.Lock()
        self.data_path = BOT_DIR / "factions.json"
        self.admin_ids = self._load_admin_ids()
        self.channel_id = self._load_channel_id()
        self._bot_dm_url: str | None = None
        self._data_cache: dict[str, Any] | None = None
        self._ensure_file()

    def _load_admin_ids(self) -> set[int]:
        raw = str(os.getenv("FACTION_ADMIN_IDS", "")).strip()
        if not raw:
            return set(DEFAULT_ADMIN_IDS)
        values: set[int] = set()
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                values.add(int(token))
            except ValueError:
                continue
        return values or set(DEFAULT_ADMIN_IDS)

    def _load_channel_id(self) -> int | None:
        raw = str(os.getenv("FACTION_CHANNEL_ID", "")).strip()
        if not raw:
            return DEFAULT_CHANNEL_ID
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_CHANNEL_ID

    def _ensure_file(self) -> None:
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.data_path.exists():
            self.data_path.write_text(json.dumps({"factions": []}, indent=2) + "\n", encoding="utf-8")

    def _load_data(self, *, force_reload: bool = False) -> dict[str, Any]:
        if not force_reload and self._data_cache is not None:
            return self._data_cache
        self._ensure_file()
        try:
            payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"factions": []}
        factions = payload.get("factions", [])
        if not isinstance(factions, list):
            factions = []
        payload = {"factions": factions}
        for faction in payload["factions"]:
            if isinstance(faction, dict):
                self._ensure_defaults(faction)
        self._data_cache = payload
        return payload

    def _save_data(self, data: dict[str, Any]) -> None:
        factions = data.get("factions", [])
        if not isinstance(factions, list):
            factions = []
        payload = {"factions": [faction for faction in factions if isinstance(faction, dict)]}
        for faction in payload["factions"]:
            self._ensure_defaults(faction)
        self.data_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self._data_cache = payload

    def _ensure_defaults(self, faction: dict[str, Any]) -> None:
        if not faction.get("pfp"):
            faction["pfp"] = DEFAULT_PFP
        if not isinstance(faction.get("members"), list):
            faction["members"] = []
        if not isinstance(faction.get("admins"), list):
            faction["admins"] = []
        if not isinstance(faction.get("memberNames"), dict):
            faction["memberNames"] = {}
        inv = faction.get("inv")
        if not isinstance(inv, dict):
            inv = {}
        vp = int(inv.get("vp") or 0)
        pc = inv.get("pc")
        if isinstance(pc, (int, float)):
            vp += int(pc)
        inv["vp"] = vp
        inv.pop("pc", None)
        faction["inv"] = inv

    def _find_by_id(self, data: dict[str, Any], faction_id: str) -> dict[str, Any] | None:
        for faction in data["factions"]:
            if str(faction.get("id")) == str(faction_id):
                return faction
        return None

    def _find_by_name(self, data: dict[str, Any], name: str) -> dict[str, Any] | None:
        target = str(name).strip().lower()
        for faction in data["factions"]:
            if str(faction.get("name") or "").strip().lower() == target:
                return faction
        return None

    def _find_by_captain(self, data: dict[str, Any], user_id: int) -> dict[str, Any] | None:
        for faction in data["factions"]:
            if str(faction.get("captain")) == str(user_id):
                return faction
        return None

    def _find_by_member(self, data: dict[str, Any], user_id: int | str) -> dict[str, Any] | None:
        token = str(user_id)
        for faction in data["factions"]:
            members = [str(x) for x in faction.get("members", [])]
            if token in members:
                return faction
        return None

    def _in_any_faction(self, data: dict[str, Any], user_id: int | str) -> dict[str, Any] | None:
        return self._find_by_member(data, user_id) or self._find_by_captain(data, int(user_id))

    def _is_captain(self, faction: dict[str, Any], user_id: int | str) -> bool:
        return str(faction.get("captain")) == str(user_id)

    def _is_admin(self, faction: dict[str, Any], user_id: int | str) -> bool:
        token = str(user_id)
        for row in faction.get("admins", []):
            if str((row or {}).get("userId")) == token:
                return True
        return False

    def _is_officer(self, faction: dict[str, Any], user_id: int | str) -> bool:
        return self._is_captain(faction, user_id) or self._is_admin(faction, user_id)

    def _touch_name(self, faction: dict[str, Any], user_id: int, name: str) -> None:
        self._ensure_defaults(faction)
        faction["memberNames"][str(user_id)] = str(name).strip() or str(user_id)

    def _name_for(self, faction: dict[str, Any], user_id: int | str) -> str:
        return str((faction.get("memberNames") or {}).get(str(user_id)) or user_id)

    def _build_channel_post(self, faction: dict[str, Any]) -> str:
        self._ensure_defaults(faction)
        members = [str(x) for x in faction.get("members", [])]
        admins = list(faction.get("admins", []))
        captain_id = str(faction.get("captain") or "")
        captain_name = self._name_for(faction, captain_id)
        admin_links = ", ".join(
            _mention(str(row.get("userId") or ""), str(row.get("name") or row.get("userId") or ""))
            for row in admins
            if isinstance(row, dict)
        ) or "None"
        member_rows = "\n".join(
            f"{index}. {_mention(member_id, self._name_for(faction, member_id))}"
            for index, member_id in enumerate(members, start=1)
        ) or "No members yet."

        return "\n".join(
            [
                f"<b>{_esc(faction.get('name'))}</b>",
                DIVIDER,
                f"Captain: {_mention(captain_id, captain_name)}",
                f"Admins: {admin_links}",
                f"Members: {len(members)}",
                DIVIDER,
                "<b>Member List</b>",
                member_rows,
            ]
        )

    async def _sync_channel(self, data: dict[str, Any], faction: dict[str, Any]) -> None:
        if self.channel_id is None:
            return
        text = self._build_channel_post(faction)
        message_id = faction.get("channelMessageId")
        if message_id:
            try:
                await self.client.edit_message(self.channel_id, int(message_id), text, parse_mode="html", link_preview=False)
                return
            except Exception:
                pass
        try:
            sent = await self.client.send_message(self.channel_id, text, parse_mode="html", link_preview=False)
            faction["channelMessageId"] = int(getattr(sent, "id", 0) or 0) or None
            self._save_data(data)
        except Exception:
            return

    async def _create_invite_link(self, chat_id: int) -> str | None:
        try:
            from telethon.tl import functions

            exported = await self.client(functions.messages.ExportChatInviteRequest(peer=chat_id))
            link = str(getattr(exported, "link", "") or "").strip()
            return link or None
        except Exception:
            return None

    async def _bot_dm_url_value(self) -> str | None:
        if self._bot_dm_url:
            return self._bot_dm_url
        try:
            me = await self.client.get_me()
        except Exception:
            return None
        username = str(getattr(me, "username", "") or "").strip()
        if not username:
            return None
        self._bot_dm_url = f"https://t.me/{username}"
        return self._bot_dm_url

    async def _dm_redirect(
        self,
        event: NewMessage.Event,
        *,
        title: str,
        reason: str,
    ) -> None:
        url = await self._bot_dm_url_value()
        buttons = [[Button.url("Open Bot DM", url)]] if url else None
        await event.respond(
            (
                f"<b>{_esc(title)}</b>\n"
                f"{DIVIDER}\n"
                f"{_esc(reason)}"
            ),
            parse_mode="html",
            buttons=buttons,
            link_preview=False,
        )

    async def track_sender(self, event: NewMessage.Event) -> None:
        user_id = int(event.sender_id or 0)
        if user_id == 0:
            return
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        name = str(_name_from_user(sender, str(user_id))).strip() or str(user_id)
        async with self._lock:
            data = self._load_data()
            faction = self._in_any_faction(data, user_id)
            if faction is None:
                return
            existing_name = str((faction.get("memberNames") or {}).get(str(user_id)) or "").strip()
            if existing_name == name:
                return
            self._touch_name(faction, user_id, name)
            self._save_data(data)

    async def on_create(self, event: NewMessage.Event) -> None:
        if int(event.sender_id or 0) not in self.admin_ids:
            await event.respond("Access denied. Admin only.")
            return
        tokens = event.raw_text.split()
        if len(tokens) < 3:
            await event.respond("Usage: /create <Faction Name> <CaptainUID>")
            return
        captain_id_raw = tokens[-1]
        if not captain_id_raw.isdigit():
            await event.respond("Invalid captain UID.")
            return
        captain_id = int(captain_id_raw)
        name = " ".join(tokens[1:-1]).strip()
        if not name:
            await event.respond("Missing faction name.")
            return

        async with self._lock:
            data = self._load_data()
            if self._find_by_name(data, name) is not None:
                await event.respond(f"Faction '{name}' already exists.")
                return
            faction = {
                "id": str(int(datetime.utcnow().timestamp() * 1000)),
                "name": name,
                "captain": captain_id,
                "captain_name": "",
                "members": [str(captain_id)],
                "memberNames": {},
                "admins": [],
                "group": None,
                "pfp": DEFAULT_PFP,
                "channelMessageId": None,
                "inv": {"vp": 0},
            }
            data["factions"].append(faction)
            self._save_data(data)
            await self._sync_channel(data, faction)
        await event.respond(f"Faction created: {name}\nCaptain UID: {captain_id}")

    async def on_deletefac(self, event: NewMessage.Event) -> None:
        if int(event.sender_id or 0) not in self.admin_ids:
            await event.respond("Access denied. Admin only.")
            return
        name = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""
        if not name:
            await event.respond("Usage: /deletefac <Faction Name>")
            return
        async with self._lock:
            data = self._load_data()
            faction = self._find_by_name(data, name)
            if faction is None:
                await event.respond(f"No faction named {name}.")
                return
            cb = f"facdel:{faction['id']}:{int(event.sender_id)}"
        await event.respond(
            f"Confirm deletion of {faction['name']}?",
            buttons=[[Button.inline("Confirm Delete", data=cb.encode("utf-8"))]],
        )

    async def on_setgc(self, event: NewMessage.Event) -> None:
        if event.is_private:
            await event.respond("Use this command inside the faction group.")
            return
        user_id = int(event.sender_id or 0)
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        async with self._lock:
            data = self._load_data()
            faction = self._find_by_captain(data, user_id)
            if faction is None:
                await event.respond("Only the faction captain can use /setgc.")
                return
            chat_id = int(event.chat_id or 0)
            faction["group"] = chat_id
            self._touch_name(faction, user_id, _name_from_user(sender, str(user_id)))
            self._save_data(data)
            await self._sync_channel(data, faction)
        await event.respond(f"Faction group set for {faction['name']}.")

    async def on_myfac(self, event: NewMessage.Event) -> None:
        user_id = int(event.sender_id or 0)
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        async with self._lock:
            data = self._load_data()
            faction = self._in_any_faction(data, user_id)
            if faction is None:
                await event.respond(
                    (
                        "<b>Faction Profile</b>\n"
                        f"{DIVIDER}\n"
                        "You are not in any faction.\n"
                        "Use the button below to join."
                    ),
                    parse_mode="html",
                    buttons=[[Button.inline("Join Faction", data=b"faccmd:join")]],
                    link_preview=False,
                )
                return
            self._ensure_defaults(faction)
            self._touch_name(faction, user_id, _name_from_user(sender, str(user_id)))
            self._save_data(data)
            captain_id = str(faction.get("captain") or "")
            captain_name = self._name_for(faction, captain_id)
            admins = ", ".join(
                _mention(str(row.get("userId") or ""), str(row.get("name") or row.get("userId") or ""))
                for row in faction.get("admins", [])
                if isinstance(row, dict)
            ) or "None"
            role = "Member"
            if self._is_captain(faction, user_id):
                role = "Captain"
            elif self._is_admin(faction, user_id):
                role = "Admin"
            vp = int((faction.get("inv") or {}).get("vp") or 0)
            caption = "\n".join(
                [
                    f"<b>{_esc(faction['name'])}</b>",
                    DIVIDER,
                    f"Your role: {role}",
                    f"Captain: {_mention(captain_id, captain_name)}",
                    f"Admins: {admins}",
                    f"Members: {len(faction.get('members', []))}",
                    f"Faction Victory Points: {vp}",
                ]
            )
            photo = str(faction.get("pfp") or DEFAULT_PFP)
        try:
            await event.respond(file=photo, message=caption, parse_mode="html", link_preview=False)
        except Exception:
            await event.respond(caption, parse_mode="html", link_preview=False)

    async def on_faclb(self, event: NewMessage.Event) -> None:
        async with self._lock:
            data = self._load_data()
            factions = [f for f in data["factions"] if isinstance(f, dict)]
            if not factions:
                await event.respond("No factions found.")
                return
            for faction in factions:
                self._ensure_defaults(faction)
            factions.sort(key=lambda row: (-int((row.get("inv") or {}).get("vp") or 0), str(row.get("name") or "").lower()))
            lines = ["<b>Faction Leaderboard</b>", DIVIDER]
            for index, faction in enumerate(factions[:15], start=1):
                lines.append(f"{index}. <b>{_esc(faction.get('name'))}</b> - <code>{int((faction.get('inv') or {}).get('vp') or 0)}</code> VP")
        await event.respond("\n".join(lines), parse_mode="html", link_preview=False)

    async def on_join(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._dm_redirect(
                event,
                title="Faction Join",
                reason="Use /join in private DM with the bot.",
            )
            return
        user_id = int(event.sender_id or 0)
        await self._show_join_picker(event, user_id=user_id)

    async def _show_join_picker(self, event: NewMessage.Event | CallbackQuery.Event, *, user_id: int) -> None:
        data = self._load_data()
        existing = self._in_any_faction(data, user_id)
        if existing is not None:
            text = (
                "<b>Faction Join</b>\n"
                f"{DIVIDER}\n"
                f"You are already in <b>{_esc(existing['name'])}</b>.\n"
                "Use <code>/leave</code> first to switch factions."
            )
            if isinstance(event, CallbackQuery.Event):
                await event.edit(text, parse_mode="html", buttons=None, link_preview=False)
            else:
                await event.respond(text, parse_mode="html", link_preview=False)
            return

        factions = [f for f in data["factions"] if isinstance(f, dict)]
        if not factions:
            text = (
                "<b>Faction Join</b>\n"
                f"{DIVIDER}\n"
                "No factions are available right now."
            )
            if isinstance(event, CallbackQuery.Event):
                await event.edit(text, parse_mode="html", buttons=None, link_preview=False)
            else:
                await event.respond(text, parse_mode="html", link_preview=False)
            return

        rows: list[list[Button]] = []
        current_row: list[Button] = []
        for faction in factions:
            current_row.append(Button.inline(f"{faction.get('name')}", data=f"facjoin:{faction.get('id')}".encode("utf-8")))
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)

        text = (
            "<b>Faction Join</b>\n"
            f"{DIVIDER}\n"
            "Choose a faction.\n"
            "Your request will be reviewed by faction officers."
        )
        if isinstance(event, CallbackQuery.Event):
            await event.edit(text, parse_mode="html", buttons=rows, link_preview=False)
        else:
            await event.respond(text, parse_mode="html", buttons=rows, link_preview=False)

    async def on_leave(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._dm_redirect(
                event,
                title="Faction Leave",
                reason="Use /leave in private DM with the bot.",
            )
            return
        user_id = int(event.sender_id or 0)
        async with self._lock:
            data = self._load_data()
            faction = self._in_any_faction(data, user_id)
            if faction is None:
                await event.respond(
                    (
                        "<b>Faction Leave</b>\n"
                        f"{DIVIDER}\n"
                        "You are not in any faction."
                    ),
                    parse_mode="html",
                    link_preview=False,
                )
                return
            if self._is_captain(faction, user_id):
                await event.respond(
                    (
                        "<b>Faction Leave</b>\n"
                        f"{DIVIDER}\n"
                        "Captains cannot leave their faction."
                    ),
                    parse_mode="html",
                    link_preview=False,
                )
                return
            uid = str(user_id)
            faction["members"] = [str(x) for x in faction.get("members", []) if str(x) != uid]
            faction["admins"] = [row for row in faction.get("admins", []) if str((row or {}).get("userId")) != uid]
            self._ensure_defaults(faction)
            faction["memberNames"].pop(uid, None)
            faction_name = str(faction.get("name") or "Faction")
            group_id = faction.get("group")
            self._save_data(data)
            await self._sync_channel(data, faction)
        if group_id:
            try:
                await self.client.kick_participant(int(group_id), user_id)
            except Exception:
                pass
        await event.respond(
            (
                "<b>Faction Leave</b>\n"
                f"{DIVIDER}\n"
                f"You left <b>{_esc(faction_name)}</b>."
            ),
            parse_mode="html",
            link_preview=False,
        )

    async def on_fac_link(self, event: NewMessage.Event) -> None:
        if not event.is_private:
            await self._dm_redirect(
                event,
                title="Faction Invite Link",
                reason="Use /fac_link in private DM with the bot.",
            )
            return
        user_id = int(event.sender_id or 0)
        async with self._lock:
            data = self._load_data()
            faction = self._in_any_faction(data, user_id)
            if faction is None or not faction.get("group"):
                await event.respond(
                    (
                        "<b>Faction Invite Link</b>\n"
                        f"{DIVIDER}\n"
                        "Your faction has no linked group yet."
                    ),
                    parse_mode="html",
                    link_preview=False,
                )
                return
            group_id = int(faction["group"])
            faction_name = str(faction.get("name") or "Faction")
        invite = await self._create_invite_link(group_id)
        if not invite:
            await event.respond(
                (
                    "<b>Faction Invite Link</b>\n"
                    f"{DIVIDER}\n"
                    "Failed to create invite link.\n"
                    "Make sure the bot is admin in the faction group."
                ),
                parse_mode="html",
                link_preview=False,
            )
            return
        await event.respond(
            f"<b>{_esc(faction_name)} - Invite Link</b>\n\n{invite}",
            parse_mode="html",
            link_preview=False,
        )

    async def on_kick_member(self, event: NewMessage.Event) -> None:
        if event.is_private:
            await event.respond("Use this command in your faction group.")
            return
        actor_id = int(event.sender_id or 0)
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        sender_name = _name_from_user(sender, str(actor_id))
        reply = await event.get_reply_message() if event.reply_to_msg_id else None
        target_id: int | None = None
        if reply is not None and reply.sender_id:
            target_id = int(reply.sender_id)
        else:
            parts = event.raw_text.split()
            if len(parts) > 1 and parts[1].isdigit():
                target_id = int(parts[1])
        if target_id is None:
            await event.respond("Usage: reply with /kick_member or /kick_member <uid>")
            return

        async with self._lock:
            data = self._load_data()
            faction = self._in_any_faction(data, actor_id)
            if faction is None:
                await event.respond("You are not in any faction.")
                return
            if str(faction.get("group")) != str(event.chat_id):
                await event.respond("Use this command in your faction group.")
                return
            if not self._is_officer(faction, actor_id):
                await event.respond("Only captain/admin can kick members.")
                return
            if self._is_captain(faction, target_id):
                await event.respond("Captain cannot be kicked.")
                return
            if target_id == actor_id:
                await event.respond("You cannot kick yourself. Use /leave.")
                return
            if not self._is_captain(faction, actor_id) and self._is_admin(faction, target_id):
                await event.respond("Admins cannot kick other admins. Captain only.")
                return
            members = [str(x) for x in faction.get("members", [])]
            if str(target_id) not in members:
                await event.respond("That user is not in your faction.")
                return
            target_name = self._name_for(faction, target_id)
            faction["members"] = [x for x in members if str(x) != str(target_id)]
            faction["admins"] = [row for row in faction.get("admins", []) if str((row or {}).get("userId")) != str(target_id)]
            self._ensure_defaults(faction)
            faction["memberNames"].pop(str(target_id), None)
            faction_name = str(faction.get("name") or "Faction")
            group_id = int(faction.get("group") or 0)
            self._touch_name(faction, actor_id, sender_name)
            self._save_data(data)
            await self._sync_channel(data, faction)
        try:
            if group_id:
                await self.client.kick_participant(group_id, target_id)
        except Exception:
            pass
        try:
            await self.client.send_message(target_id, f"You were removed from {faction_name} by {sender_name}.")
        except Exception:
            pass
        await event.respond(
            f"Member removed.\n\n{_mention(target_id, target_name)} was kicked from <b>{_esc(faction_name)}</b> by {_mention(actor_id, sender_name)}.",
            parse_mode="html",
            link_preview=False,
        )

    async def on_facpromote(self, event: NewMessage.Event) -> None:
        if not event.reply_to_msg_id:
            await event.respond("Reply to a member message with /facpromote.")
            return
        reply = await event.get_reply_message()
        if reply is None or not reply.sender_id:
            await event.respond("Invalid target.")
            return
        actor_id = int(event.sender_id or 0)
        target_id = int(reply.sender_id)
        target_user = reply.sender if isinstance(reply.sender, User) else await reply.get_sender()
        target_name = _name_from_user(target_user, str(target_id))

        async with self._lock:
            data = self._load_data()
            faction = self._find_by_captain(data, actor_id)
            if faction is None:
                await event.respond("Only faction captain can promote members.")
                return
            if self._is_captain(faction, target_id):
                await event.respond("Target is already captain.")
                return
            if str(target_id) not in [str(x) for x in faction.get("members", [])]:
                await event.respond("Target is not a faction member.")
                return
            if self._is_admin(faction, target_id):
                await event.respond(
                    f"{_mention(target_id, self._name_for(faction, target_id))} is already admin.",
                    parse_mode="html",
                    link_preview=False,
                )
                return
            faction.setdefault("admins", []).append({"userId": str(target_id), "name": target_name})
            self._touch_name(faction, target_id, target_name)
            self._save_data(data)
            await self._sync_channel(data, faction)
            faction_name = str(faction.get("name") or "Faction")
        await event.respond(
            f"Promoted.\n\n{_mention(target_id, target_name)} is now an <b>Admin</b> of <b>{_esc(faction_name)}</b>.",
            parse_mode="html",
            link_preview=False,
        )

    async def on_facdemote(self, event: NewMessage.Event) -> None:
        if not event.reply_to_msg_id:
            await event.respond("Reply to an admin message with /facdemote.")
            return
        reply = await event.get_reply_message()
        if reply is None or not reply.sender_id:
            await event.respond("Invalid target.")
            return
        actor_id = int(event.sender_id or 0)
        target_id = int(reply.sender_id)
        target_user = reply.sender if isinstance(reply.sender, User) else await reply.get_sender()
        target_name = _name_from_user(target_user, str(target_id))

        async with self._lock:
            data = self._load_data()
            faction = self._find_by_captain(data, actor_id)
            if faction is None:
                await event.respond("Only faction captain can demote admins.")
                return
            if not self._is_admin(faction, target_id):
                await event.respond(
                    f"{_mention(target_id, self._name_for(faction, target_id))} is not an admin.",
                    parse_mode="html",
                    link_preview=False,
                )
                return
            faction["admins"] = [row for row in faction.get("admins", []) if str((row or {}).get("userId")) != str(target_id)]
            self._save_data(data)
            await self._sync_channel(data, faction)
            faction_name = str(faction.get("name") or "Faction")
        await event.respond(
            f"Demoted.\n\n{_mention(target_id, target_name)} is now a regular <b>Member</b> of <b>{_esc(faction_name)}</b>.",
            parse_mode="html",
            link_preview=False,
        )

    async def on_setpfp(self, event: NewMessage.Event) -> None:
        actor_id = int(event.sender_id or 0)
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.respond("Usage: /setpfp <https://image-url>")
            return
        url = str(parts[1]).strip()
        if not re.match(r"^https?://.+", url, flags=re.IGNORECASE):
            await event.respond("Invalid URL. Must start with http:// or https://")
            return
        sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
        sender_name = _name_from_user(sender, str(actor_id))

        async with self._lock:
            data = self._load_data()
            faction = self._find_by_captain(data, actor_id)
            if faction is None:
                await event.respond("Only faction captain can request photo change.")
                return
            self._ensure_defaults(faction)
            faction["pendingPfp"] = url
            self._touch_name(faction, actor_id, sender_name)
            self._save_data(data)
            faction_id = str(faction.get("id") or "")
            faction_name = str(faction.get("name") or "Faction")

        approve_cb = f"pfpapp:{faction_id}:{actor_id}".encode("utf-8")
        decline_cb = f"pfpdec:{faction_id}:{actor_id}".encode("utf-8")
        caption = (
            f"<b>PFP Change Request</b>\n"
            f"{DIVIDER}\n"
            f"Faction: {_esc(faction_name)}\n"
            f"Captain: {_mention(actor_id, sender_name)}\n"
            f"New image URL: <code>{_esc(url)}</code>"
        )
        for admin_id in self.admin_ids:
            try:
                await self.client.send_file(
                    admin_id,
                    file=url,
                    caption=caption,
                    parse_mode="html",
                    buttons=[[Button.inline("Approve", data=approve_cb), Button.inline("Decline", data=decline_cb)]],
                )
            except Exception:
                continue
        await event.respond("Photo request sent to admins for review.")

    async def on_setname(self, event: NewMessage.Event) -> None:
        actor_id = int(event.sender_id or 0)
        new_name = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""
        if not new_name:
            await event.respond("Usage: /setname <New Faction Name>")
            return
        if len(new_name) > 32:
            await event.respond("Faction name must be 32 chars or fewer.")
            return
        async with self._lock:
            data = self._load_data()
            faction = self._find_by_captain(data, actor_id)
            if faction is None:
                await event.respond("Only faction captain can rename faction.")
                return
            existing = self._find_by_name(data, new_name)
            if existing is not None and str(existing.get("id")) != str(faction.get("id")):
                await event.respond(f"Another faction already has the name {new_name}.")
                return
            old_name = str(faction.get("name") or "Faction")
            faction["name"] = new_name
            self._save_data(data)
            await self._sync_channel(data, faction)
        await event.respond(f"Faction renamed.\n{old_name} -> {new_name}")

    async def on_fac_deposit(self, event: NewMessage.Event) -> None:
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.respond("Usage: /fac_deposit <amount>")
            return
        raw_amount = str(parts[1]).replace(",", "").strip()
        if not raw_amount.isdigit():
            await event.respond("Deposit amount must be a positive number.")
            return
        amount = int(raw_amount)
        if amount <= 0:
            await event.respond("Deposit amount must be greater than 0.")
            return

        user_id = int(event.sender_id or 0)
        async with self._lock:
            data = self._load_data()
            faction = self._in_any_faction(data, user_id)
            if faction is None:
                await event.respond("You are not in any faction.")
                return

            deposit = await run_db_work_async(lambda session: self._deposit_faction_vp(
                session,
                user_id=user_id,
                amount=amount,
            ))
            if deposit["status"] == "missing_trainer":
                await event.respond("Trainer profile not found.")
                return
            if deposit["status"] == "insufficient_vp":
                await event.respond(f"Not enough VP. You have {deposit['current_vp']:,} VP.")
                return
            if deposit["status"] == "failed":
                await event.respond("Could not process deposit.")
                return
            remaining_vp = int(deposit["remaining_vp"])

            self._ensure_defaults(faction)
            inv = faction.get("inv") or {}
            if not isinstance(inv, dict):
                inv = {}
            inv["vp"] = int(inv.get("vp") or 0) + amount
            faction["inv"] = inv
            faction_name = str(faction.get("name") or "Faction")
            faction_vp = int(inv.get("vp") or 0)
            self._save_data(data)
            await self._sync_channel(data, faction)

        await event.respond(
            (
                "<b>Faction Deposit</b>\n"
                f"{DIVIDER}\n"
                f"Faction: <b>{_esc(faction_name)}</b>\n"
                f"Deposited: <code>{amount:,}</code> VP\n"
                f"Your VP left: <code>{remaining_vp:,}</code>\n"
                f"Faction bank VP: <code>{faction_vp:,}</code>"
            ),
            parse_mode="html",
            link_preview=False,
        )

    def _deposit_faction_vp(self, session, *, user_id: int, amount: int) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        trainer = trainers.get_by_telegram_user_id(user_id)
        if trainer is None or trainer.inventory is None:
            return {"status": "missing_trainer"}
        current_vp = int(trainer.inventory.victory_points or 0)
        if current_vp < amount:
            return {"status": "insufficient_vp", "current_vp": current_vp}
        if not inventories.consume_victory_points(trainer, amount):
            return {"status": "failed"}
        return {
            "status": "ok",
            "remaining_vp": int(trainer.inventory.victory_points or 0),
        }

    async def _handle_callback_locked(self, event: CallbackQuery.Event, data: str) -> bool:
        sender_id = int(event.sender_id or 0)
        parts = data.split(":")
        if not parts:
            return False
        tag = parts[0]
        payload = self._load_data()

        if tag == "facdel" and len(parts) >= 3:
            faction_id, admin_id = parts[1], parts[2]
            if str(sender_id) != str(admin_id) or sender_id not in self.admin_ids:
                await event.answer("Not authorized.", alert=True)
                return True
            faction = self._find_by_id(payload, faction_id)
            if faction is None:
                await event.answer("Faction not found.", alert=True)
                return True
            payload["factions"] = [row for row in payload["factions"] if str((row or {}).get("id")) != str(faction_id)]
            self._save_data(payload)
            channel_msg = faction.get("channelMessageId")
            if channel_msg and self.channel_id is not None:
                try:
                    await self.client.delete_messages(self.channel_id, int(channel_msg))
                except Exception:
                    pass
            try:
                await event.edit(f"Faction deleted: {faction.get('name')}", buttons=None)
            except Exception:
                pass
            await event.answer("Deleted.")
            return True

        if tag == "facjoin" and len(parts) >= 2:
            faction_id = parts[1]
            if self._in_any_faction(payload, sender_id) is not None:
                await event.answer("You are already in a faction.", alert=True)
                return True
            faction = self._find_by_id(payload, faction_id)
            if faction is None:
                await event.answer("Faction not found.", alert=True)
                return True
            self._ensure_defaults(faction)
            if not faction.get("group"):
                await event.answer("Faction has no linked group.", alert=True)
                return True
            sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
            sender_name = _name_from_user(sender, str(sender_id))
            self._touch_name(faction, sender_id, sender_name)
            self._save_data(payload)
            approve_cb = f"facapp:{faction.get('id')}:{sender_id}".encode("utf-8")
            decline_cb = f"facdec:{faction.get('id')}:{sender_id}".encode("utf-8")
            try:
                await self.client.send_message(
                    int(faction["group"]),
                    (
                        "<b>Join Request</b>\n"
                        f"{DIVIDER}\n"
                        f"{_mention(sender_id, sender_name)} wants to join <b>{_esc(faction.get('name'))}</b>."
                    ),
                    parse_mode="html",
                    buttons=[[Button.inline("Approve", data=approve_cb), Button.inline("Decline", data=decline_cb)]],
                    link_preview=False,
                )
            except Exception:
                await event.answer("Could not send request to faction group.", alert=True)
                return True
            try:
                await event.edit(f"Request sent to {faction.get('name')}.", buttons=None)
            except Exception:
                pass
            await event.answer("Request sent.")
            return True

        if tag == "faccmd" and len(parts) >= 2 and parts[1] == "join":
            if not event.is_private:
                url = await self._bot_dm_url_value()
                if url:
                    await event.answer("Open bot DM to join faction.", alert=True)
                    await event.respond(
                        (
                            "<b>Faction Join</b>\n"
                            f"{DIVIDER}\n"
                            "Use this in DM:",
                        ),
                        parse_mode="html",
                        buttons=[[Button.url("Open Bot DM", url)]],
                        link_preview=False,
                    )
                else:
                    await event.answer("Use /join in DM with the bot.", alert=True)
                return True
            await self._show_join_picker(event, user_id=sender_id)
            await event.answer()
            return True

        if tag in {"facapp", "facdec", "pfpapp", "pfpdec"}:
            return await self._handle_officer_or_admin_callback(event, payload, parts, sender_id)
        return False

    async def _handle_officer_or_admin_callback(
        self,
        event: CallbackQuery.Event,
        payload: dict[str, Any],
        parts: list[str],
        sender_id: int,
    ) -> bool:
        tag = parts[0]
        if tag in {"facapp", "facdec"} and len(parts) >= 3:
            faction_id, user_id = parts[1], int(parts[2])
            faction = self._find_by_id(payload, faction_id)
            if faction is None:
                await event.answer("Faction not found.", alert=True)
                return True
            if not self._is_officer(faction, sender_id):
                await event.answer("Not authorized.", alert=True)
                return True
            approver_name = self._name_for(faction, sender_id)
            if approver_name == str(sender_id):
                sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
                approver_name = _name_from_user(sender, str(sender_id))
                self._touch_name(faction, sender_id, approver_name)

            if tag == "facapp":
                if self._in_any_faction(payload, user_id) is not None:
                    await event.answer("User already in a faction.", alert=True)
                    return True
                members = [str(x) for x in faction.get("members", [])]
                if str(user_id) not in members:
                    members.append(str(user_id))
                faction["members"] = members
                self._save_data(payload)
                await self._sync_channel(payload, faction)

                invite = None
                if faction.get("group"):
                    invite = await self._create_invite_link(int(faction["group"]))
                text = f"Welcome to {faction.get('name')}.\nApproved by {approver_name}."
                if invite:
                    text += f"\n\nGroup invite link:\n{invite}"
                try:
                    await self.client.send_message(user_id, text, link_preview=False)
                except Exception:
                    pass
                try:
                    await event.edit(
                        f"Approved.\n\n{_mention(user_id, self._name_for(faction, user_id))} was approved by {_mention(sender_id, approver_name)} and added to <b>{_esc(faction.get('name'))}</b>.",
                        parse_mode="html",
                        buttons=None,
                        link_preview=False,
                    )
                except Exception:
                    pass
                await event.answer("Approved.")
                return True

            try:
                await self.client.send_message(
                    user_id,
                    f"Request declined.\n\nYour request to join {faction.get('name')} was declined.",
                )
            except Exception:
                pass
            try:
                await event.edit(
                    f"Declined.\n\n{_mention(user_id, self._name_for(faction, user_id))}'s request was declined by {_mention(sender_id, approver_name)}.",
                    parse_mode="html",
                    buttons=None,
                    link_preview=False,
                )
            except Exception:
                pass
            await event.answer("Declined.")
            return True

        if tag in {"pfpapp", "pfpdec"} and len(parts) >= 3:
            if sender_id not in self.admin_ids:
                await event.answer("Not authorized.", alert=True)
                return True
            faction_id, captain_id = parts[1], int(parts[2])
            faction = self._find_by_id(payload, faction_id)
            if faction is None:
                await event.answer("Faction not found.", alert=True)
                return True
            pending = str(faction.get("pendingPfp") or "").strip()
            if not pending:
                await event.answer("No pending photo.", alert=True)
                return True
            admin_sender = event.sender if isinstance(event.sender, User) else await event.get_sender()
            admin_name = _name_from_user(admin_sender, str(sender_id))
            captain_name = self._name_for(faction, captain_id)
            faction_name = str(faction.get("name") or "Faction")

            if tag == "pfpapp":
                faction["pfp"] = pending
                faction["pendingPfp"] = None
                self._touch_name(faction, sender_id, admin_name)
                self._save_data(payload)
                await self._sync_channel(payload, faction)
                try:
                    await self.client.send_file(captain_id, file=pending, caption=f"Photo approved for {faction_name}.")
                except Exception:
                    pass
                try:
                    await event.edit(
                        (
                            "<b>PFP Approved</b>\n"
                            f"{DIVIDER}\n"
                            f"Faction: {_esc(faction_name)}\n"
                            f"Approved by: {_mention(sender_id, admin_name)}\n"
                            f"Captain: {_mention(captain_id, captain_name)}"
                        ),
                        parse_mode="html",
                        buttons=None,
                        link_preview=False,
                    )
                except Exception:
                    pass
                await event.answer("Approved.")
                return True

            faction["pendingPfp"] = None
            self._save_data(payload)
            try:
                await self.client.send_message(
                    captain_id,
                    f"Photo declined for {faction_name}. Please try a different image with /setpfp.",
                )
            except Exception:
                pass
            try:
                await event.edit(
                    (
                        "<b>PFP Declined</b>\n"
                        f"{DIVIDER}\n"
                        f"Faction: {_esc(faction_name)}\n"
                        f"Declined by: {_mention(sender_id, admin_name)}\n"
                        f"Captain: {_mention(captain_id, captain_name)}"
                    ),
                    parse_mode="html",
                    buttons=None,
                    link_preview=False,
                )
            except Exception:
                pass
            await event.answer("Declined.")
            return True

        return False

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        data = event.data.decode("utf-8")
        if not any(data.startswith(prefix) for prefix in ("facdel:", "facjoin:", "facapp:", "facdec:", "pfpapp:", "pfpdec:", "faccmd:")):
            return False
        async with self._lock:
            return await self._handle_callback_locked(event, data)
