"""What the gateway remembers across restarts: its own intercom id and the
extension each kiosk was given, so a kiosk keeps its number."""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

STATE = Path(os.environ.get("KIG_STATE", "/data/state.json"))


class State:
    def __init__(self, path: Path = STATE) -> None:
        self.path = path
        data = self._read()
        self.gateway_id: str = str(data.get("gateway_id") or f"kig-{secrets.token_hex(6)}")
        ext = data.get("extensions")
        self.extensions: dict[str, str] = (
            {str(k): str(v) for k, v in ext.items()} if isinstance(ext, dict) else {}
        )
        if not data.get("gateway_id"):
            self.save()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            _LOG.warning("ignoring unreadable %s: %s", self.path, err)
            return {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"gateway_id": self.gateway_id, "extensions": self.extensions}
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)
        except OSError as err:
            _LOG.warning("could not save %s: %s", self.path, err)

    def extension_for(self, kiosk_id: str, start: int) -> str:
        """The kiosk's extension, giving it the lowest free one from ``start``
        the first time it is seen."""
        if kiosk_id in self.extensions:
            return self.extensions[kiosk_id]
        taken = set(self.extensions.values())
        n = start
        while str(n) in taken:
            n += 1
        self.extensions[kiosk_id] = str(n)
        self.save()
        return str(n)

    def kiosk_for(self, extension: str) -> str | None:
        for kid, ext in self.extensions.items():
            if ext == extension:
                return kid
        return None
