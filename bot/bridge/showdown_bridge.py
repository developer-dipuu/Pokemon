from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any


class ShowdownBridgeError(RuntimeError):
    """Raised when the local Showdown worker cannot be started or used."""


_BUILD_LOCK = asyncio.Lock()


async def ensure_showdown_build(showdown_dir: Path) -> None:
    """Build the local Showdown dist output once if it does not exist yet."""
    dist_entry = showdown_dir / "dist" / "sim" / "index.js"
    if dist_entry.exists():
        return

    async with _BUILD_LOCK:
        if dist_entry.exists():
            return

        process = await asyncio.to_thread(
            subprocess.run,
            ["node", "build"],
            cwd=str(showdown_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = process.stdout or ""
        stderr = process.stderr or ""

        if process.returncode != 0:
            raise ShowdownBridgeError(
                "Pokemon Showdown build failed.\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )

        if not dist_entry.exists():
            raise ShowdownBridgeError(
                f"Pokemon Showdown build completed without creating {dist_entry}."
            )


class ShowdownBattleProcess:
    """Async JSON bridge around a local Node worker that hosts one battle."""

    def __init__(
        self,
        *,
        battle_id: str,
        bot_dir: Path,
        showdown_dir: Path,
        format_id: str,
        p1_name: str,
        p2_name: str,
        p1_team: str | None = None,
        p2_team: str | None = None,
    ) -> None:
        self.battle_id = battle_id
        self.bot_dir = bot_dir
        self.showdown_dir = showdown_dir
        self.format_id = format_id
        self.p1_name = p1_name
        self.p2_name = p2_name
        self.p1_team = p1_team
        self.p2_team = p2_team
        self.worker_path = self.bot_dir / "bridge" / "showdown_worker.js"

        self.process: subprocess.Popen[str] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._pending_responses: dict[str, asyncio.Future[Any]] = {}
        self._request_counter = 0

    async def start(self) -> None:
        await ensure_showdown_build(self.showdown_dir)

        self.process = await asyncio.to_thread(
            subprocess.Popen,
            ["node", str(self.worker_path)],
            cwd=str(self.bot_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout_task = asyncio.create_task(self._pump_stdout())
        self._stderr_task = asyncio.create_task(self._pump_stderr())

        await self._send(
            {
                "type": "start",
                "battleId": self.battle_id,
                "formatid": self.format_id,
                "p1": {"name": self.p1_name, "team": self.p1_team},
                "p2": {"name": self.p2_name, "team": self.p2_team},
            }
        )

    async def choose(self, slot: str, choice: str) -> None:
        await self._send({"type": "choose", "slot": slot, "choice": choice})

    async def undo(self, slot: str) -> None:
        await self._send({"type": "undo", "slot": slot})

    async def forfeit(self, slot: str) -> None:
        await self._send({"type": "forfeit", "slot": slot})

    async def active_stats(self, slot: str) -> dict[str, Any]:
        payload = await self._request({"type": "active-stats", "slot": slot})
        if not isinstance(payload, dict):
            raise ShowdownBridgeError("The Showdown worker returned an invalid active stat snapshot.")
        return payload

    async def next_event(self) -> dict[str, Any]:
        event = await self._events.get()
        if event is None:
            raise EOFError("Showdown worker closed.")
        return event

    async def close(self) -> None:
        process = self.process
        if process is None:
            return

        try:
            await self._send({"type": "close"})
        except ShowdownBridgeError:
            pass

        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)

        for handle in (process.stdin, process.stdout, process.stderr):
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass

        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._fail_pending_responses("Showdown worker closed.")
        if self.process is process:
            self.process = None

    async def _request(self, payload: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        self._request_counter += 1
        request_id = f"req-{self._request_counter}"
        future: asyncio.Future[Any] = loop.create_future()
        self._pending_responses[request_id] = future
        try:
            await self._send({**payload, "requestId": request_id})
        except Exception:
            self._pending_responses.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        try:
            return await future
        finally:
            self._pending_responses.pop(request_id, None)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ShowdownBridgeError("Showdown worker is not running.")
        if self.process.poll() is not None:
            raise ShowdownBridgeError("Showdown worker already exited.")

        line = json.dumps(payload, separators=(",", ":")) + "\n"
        await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ShowdownBridgeError("Lost connection to the Showdown worker.") from exc

    async def _pump_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None

        try:
            while True:
                line = await asyncio.to_thread(self.process.stdout.readline)
                if not line:
                    break

                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    await self._events.put(
                        {
                            "type": "bridge_error",
                            "message": f"Invalid worker JSON: {line}",
                        }
                    )
                    raise ShowdownBridgeError("The Showdown worker emitted invalid JSON.") from exc

                if event.get("type") == "response":
                    request_id = str(event.get("requestId") or "")
                    future = self._pending_responses.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(event.get("payload"))
                    continue
                if event.get("type") == "response_error":
                    request_id = str(event.get("requestId") or "")
                    future = self._pending_responses.get(request_id)
                    if future is not None and not future.done():
                        future.set_exception(ShowdownBridgeError(str(event.get("message") or "Showdown worker request failed.")))
                    continue

                await self._events.put(event)
        finally:
            self._fail_pending_responses("Showdown worker closed.")
            await self._events.put(None)

    async def _pump_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None

        while True:
            line = await asyncio.to_thread(self.process.stderr.readline)
            if not line:
                break
            text = line.rstrip()
            if text:
                print(f"[showdown:{self.battle_id}] {text}")

    def _fail_pending_responses(self, message: str) -> None:
        if not self._pending_responses:
            return
        error = ShowdownBridgeError(message)
        pending = list(self._pending_responses.values())
        self._pending_responses.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
