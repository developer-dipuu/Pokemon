from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from bot.bridge.showdown_bridge import ShowdownBridgeError, ensure_showdown_build


async def run_dex_tool(*, bot_dir: Path, showdown_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    await ensure_showdown_build(showdown_dir)

    process = await asyncio.to_thread(
        subprocess.run,
        ["node", str(bot_dir / "bridge" / "showdown_dex_tools.js")],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(bot_dir),
    )
    stdout = process.stdout or ""
    stderr = process.stderr or ""

    try:
        result = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        if process.returncode != 0:
            raise ShowdownBridgeError(
                "The Showdown Dex helper failed.\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            ) from exc
        raise ShowdownBridgeError(
            f"The Showdown Dex helper returned invalid JSON: {stdout!r}"
        ) from exc

    if result.get("ok", False):
        return result

    if process.returncode != 0:
        raise ShowdownBridgeError(
            "The Showdown Dex helper failed.\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )

    if not result.get("ok", False):
        raise ShowdownBridgeError(str(result.get("error") or "The Showdown Dex helper rejected the request."))
    return result
