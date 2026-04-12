from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from bot.config import RUNTIME_DIR


WEEKEND_BOOST_PATH = RUNTIME_DIR / "weekend_boost.json"
WEEKEND_WEEKDAYS = {5, 6}


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "enabled_by_user_id": None,
        "enabled_at": None,
        "disabled_by_user_id": None,
        "disabled_at": None,
    }


def _normalized_state(raw: Any) -> dict[str, Any]:
    state = _default_state()
    if isinstance(raw, dict):
        state.update(
            {
                "enabled": bool(raw.get("enabled", False)),
                "enabled_by_user_id": raw.get("enabled_by_user_id"),
                "enabled_at": raw.get("enabled_at"),
                "disabled_by_user_id": raw.get("disabled_by_user_id"),
                "disabled_at": raw.get("disabled_at"),
            }
        )
    return state


def load_weekend_boost_state() -> dict[str, Any]:
    try:
        raw = json.loads(WEEKEND_BOOST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except (TypeError, ValueError):
        return _default_state()
    return _normalized_state(raw)


def save_weekend_boost_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_state(state)
    WEEKEND_BOOST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEEKEND_BOOST_PATH.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
    return normalized


def weekend_boost_window_open(*, now: datetime | None = None) -> bool:
    return _utc_now(now).weekday() in WEEKEND_WEEKDAYS


def weekend_boost_active(*, now: datetime | None = None) -> bool:
    state = load_weekend_boost_state()
    return bool(state.get("enabled")) and weekend_boost_window_open(now=now)


def set_weekend_boost_enabled(
    enabled: bool,
    *,
    actor_user_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc_now(now)
    if enabled and not weekend_boost_window_open(now=current):
        raise ValueError("Weekend boost can only be enabled on Saturday or Sunday (UTC).")

    state = load_weekend_boost_state()
    state["enabled"] = bool(enabled)
    timestamp = current.isoformat()
    if enabled:
        state["enabled_by_user_id"] = int(actor_user_id or 0) or None
        state["enabled_at"] = timestamp
    else:
        state["disabled_by_user_id"] = int(actor_user_id or 0) or None
        state["disabled_at"] = timestamp
    return save_weekend_boost_state(state)


def weekend_boost_status_text(*, now: datetime | None = None) -> str:
    current = _utc_now(now)
    state = load_weekend_boost_state()
    enabled = bool(state.get("enabled"))
    window_open = weekend_boost_window_open(now=current)
    if enabled and window_open:
        status = "ACTIVE"
    elif enabled:
        status = "ENABLED, waiting for Saturday/Sunday UTC"
    else:
        status = "OFF"

    lines = [
        f"Weekend boost: {status}",
        f"Weekend window (UTC): {'open' if window_open else 'closed'}",
        "Wild IV target: mostly above 100 total IV",
        "Mega Stone drop rate: 1.5x",
        "TM drop rate: 2x",
    ]
    if state.get("enabled_at"):
        lines.append(f"Last enabled at: {state['enabled_at']}")
    if state.get("disabled_at"):
        lines.append(f"Last disabled at: {state['disabled_at']}")
    return "\n".join(lines)
