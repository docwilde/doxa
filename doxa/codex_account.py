# SPDX-License-Identifier: AGPL-3.0-only
"""Read the signed-in Codex account from the installed CLI's app-server.

Only account/read's public display fields cross this boundary. Credentials
remain with Codex, and failure simply leaves the identity banner sparse.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any


async def read_account(
    command: tuple[str, ...] = ("codex", "app-server", "--stdio"),
    *,
    timeout: float = 3.0,
) -> dict[str, str]:
    from . import __version__

    proc: asyncio.subprocess.Process | None = None

    async def send(message: dict[str, Any]) -> None:
        assert proc is not None and proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def response(request_id: int) -> dict[str, Any]:
        assert proc is not None and proc.stdout is not None
        for _ in range(256):
            line = await proc.stdout.readline()
            if not line:
                raise ValueError("Codex app-server closed the account stream")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Invalid Codex app-server response")
            if message.get("id") == request_id:
                result = message.get("result")
                if "error" in message or not isinstance(result, dict):
                    raise ValueError("Codex app-server refused account/read")
                return result
        raise ValueError("Too many Codex app-server notifications")

    try:
        async with asyncio.timeout(timeout):
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=1024 * 1024,
            )
            await send({
                "id": 0,
                "method": "initialize",
                "params": {"clientInfo": {
                    "name": "doxa", "title": "DOXA", "version": __version__,
                }},
            })
            await response(0)
            await send({"method": "initialized", "params": {}})
            await send({
                "id": 1, "method": "account/read",
                "params": {"refreshToken": False},
            })
            result = await response(1)
            account = result.get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                return {}
            return {
                key: value for key in ("type", "email", "planType")
                if isinstance((value := account.get(key)), str) and value
            }
    except (OSError, ValueError, TimeoutError):
        return {}
    finally:
        if proc is not None:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=1.0)
            except (OSError, TimeoutError):
                pass
