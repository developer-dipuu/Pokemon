from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from bot.bridge.showdown_bridge import ShowdownBattleProcess


@dataclass
class PendingChallenge:
    challenge_id: str
    chat_id: int
    public_message_id: int
    challenger_id: int
    challenger_name: str
    challenger_username: str | None = None
    mode: str = "random"
    generation: int = 9
    visuals_enabled: bool = False
    format_id: str = "gen9randombattle"
    format_label: str = "Gen 9 Random Battle"
    opponent_id: int | None = None
    opponent_name: str | None = None
    opponent_username: str | None = None
    targeted: bool = False
    state: str = "open"
    expires_at: float = 0.0
    expiry_task: asyncio.Task[None] | None = None


@dataclass
class PlayerState:
    slot: str
    user_id: int
    name: str
    current_request: dict[str, Any] | None = None
    request_token: int = 0
    locked_choice: str | None = None
    last_error: str | None = None
    primed_action: str | None = None
    used_primary_gimmick: str | None = None
    next_action_at: float = 0.0


@dataclass
class BattleSession:
    battle_id: str
    chat_id: int
    public_message_id: int
    format_id: str
    format_label: str
    players: dict[str, PlayerState]
    public_view: Any
    bridge: ShowdownBattleProcess | None = None
    finished: bool = False
    battle_mode: str = "pvp"
    metadata: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runner_task: asyncio.Task[None] | None = None
    last_render_fingerprint: str = ""
    last_visual_scene_fingerprint: str = ""
    public_render_task: asyncio.Task[None] | None = None
    render_requested: bool = False
    last_public_edit_at: float = 0.0
    visual_message_id: int | None = None

    def player_for_user(self, user_id: int) -> PlayerState | None:
        for player in self.players.values():
            if player.user_id == user_id:
                return player
        return None
