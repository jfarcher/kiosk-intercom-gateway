"""What the gateway remembers across restarts: its own intercom id, the
extension each kiosk was given (so a kiosk keeps its number) and where each
kiosk was last seen (so a reinstalled kiosk, back at the same address under a
new id, is known for the same one and keeps its number too).

``extensions`` is the one source of truth for kiosk numbers: what the page
shows and what a dialled number resolves to both come from it."""

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
        where = data.get("addresses")
        # Kiosk id → "address:port" of its remote admin, last seen.
        self.addresses: dict[str, str] = (
            {str(k): str(v) for k, v in where.items()} if isinstance(where, dict) else {}
        )
        ports = data.get("phone_ports")
        # A port per offered phone, kept so kiosks find it where it was.
        self.phone_ports: dict[str, int] = (
            {str(k): int(v) for k, v in ports.items() if str(v).isdigit()} if isinstance(ports, dict) else {}
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
            data = {"gateway_id": self.gateway_id, "extensions": self.extensions,
                    "addresses": self.addresses, "phone_ports": self.phone_ports}
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)
        except OSError as err:
            _LOG.warning("could not save %s: %s", self.path, err)

    def extension_for(self, kiosk_id: str, start: int, reserved: set[str] | None = None) -> str:
        """The kiosk's extension, giving it the lowest free one from ``start``
        the first time it is seen (never one of ``reserved``)."""
        if kiosk_id in self.extensions:
            return self.extensions[kiosk_id]
        taken = set(self.extensions.values()) | (reserved or set())
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

    def move_extension(self, old_id: str, new_id: str) -> str | None:
        """A kiosk changed id (reinstalled): ``new_id`` takes ``old_id``'s number,
        and any number ``new_id`` was given meanwhile is free again. The
        number moved, or None when ``old_id`` had none."""
        ext = self.extensions.pop(old_id, None)
        if ext is None:
            return None
        self.extensions[new_id] = ext
        if old_id in self.addresses:
            self.addresses.setdefault(new_id, self.addresses[old_id])
            del self.addresses[old_id]
        self.save()
        return ext

    def assign(self, kiosk_id: str, extension: str) -> str | None:
        """Gives ``kiosk_id`` this number, taking it from whichever id held it.
        The id it was taken from, if any (the caller checks it may be taken)."""
        holder = self.kiosk_for(extension)
        if holder is not None and holder != kiosk_id:
            del self.extensions[holder]
        self.extensions[kiosk_id] = extension
        self.save()
        return holder if holder != kiosk_id else None

    def remember_address(self, kiosk_id: str, endpoint: str) -> None:
        if kiosk_id and endpoint and self.addresses.get(kiosk_id) != endpoint:
            self.addresses[kiosk_id] = endpoint
            self.save()

    def ids_at(self, endpoint: str) -> list[str]:
        """Every id last seen at ``endpoint`` ("address:port")."""
        return [kid for kid, where in self.addresses.items() if where == endpoint]
