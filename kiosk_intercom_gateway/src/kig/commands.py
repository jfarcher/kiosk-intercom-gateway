"""Actions from Home Assistant, through ``hassio.addon_stdin``.

The Supervisor writes each action's ``input`` to the app's stdin as one line
of JSON, the way the Asterisk app takes CLI commands::

    action: hassio.addon_stdin
    data:
      addon: <this app's slug>
      input: {action: call, phone: "101", kiosk: Kitchen}

``call``    rings ``phone``, then the ``kiosk`` once the phone answers.
``hangup``  ends the calls of a ``kiosk`` or ``phone``, one ``call`` id, or all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from .bridge import CallManager

_LOG = logging.getLogger(__name__)


def parse(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
        if isinstance(value, str):  # input given as a JSON string
            value = json.loads(value)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def handle(calls: CallManager, command: dict[str, Any]) -> str:
    """Runs one action; '' when done, else why not (also logged)."""
    action = str(command.get("action") or "").strip().lower()
    text = lambda key: str(command.get(key) or "").strip()  # noqa: E731
    if action == "call":
        if not text("phone") or not text("kiosk"):
            error = "call needs phone and kiosk"
        else:
            error = await calls.action_call(text("phone"), text("kiosk"))
    elif action == "hangup":
        n = await calls.action_hangup(kiosk=text("kiosk"), phone=text("phone"), call=text("call"))
        _LOG.info("hangup action: %d call%s ended", n, "" if n == 1 else "s")
        error = ""
    else:
        error = f"unknown action {action!r} (call or hangup)"
    if error:
        _LOG.warning("action %s: %s", action or "?", error)
    return error


async def read_stdin(calls: CallManager, stop: asyncio.Event) -> None:
    """Takes actions from stdin until it closes (it is closed outside the Supervisor)."""
    if sys.stdin is None or sys.stdin.isatty():
        return
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (OSError, ValueError) as err:
        _LOG.debug("no stdin for actions: %s", err)
        return
    while not stop.is_set():
        line = await reader.readline()
        if not line:
            return
        command = parse(line.decode(errors="replace"))
        if command is None:
            _LOG.warning("ignoring an action that is not a JSON object: %r", line[:200])
            continue
        await handle(calls, command)
