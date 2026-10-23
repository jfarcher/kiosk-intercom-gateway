"""Which kiosks exist, where they are and whether they can take a call.

Kiosks come from two sources:

* mDNS: every kiosk with "Find other kiosks" on announces
  ``ks-<id>._kiosk-satellite._tcp.local`` with TXT ``id``, ``name``,
  ``version``, ``port`` (remote admin), optionally ``tls=1`` and ``host``.
* Manual entries (``host[:port]``) for kiosks mDNS cannot reach, such as
  another VLAN. They are keyed by address until their first identity probe
  names them, then merged with any mDNS entry of the same id.

Each kiosk is probed on its admin port at ``/api/intercom/identity``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import DEFAULT_ADMIN_PORT, fingerprint

_LOG = logging.getLogger(__name__)

# (status code, decoded JSON body) or None when the kiosk did not answer.
ProbeResult = tuple[int, dict[str, Any] | None] | None
Prober = Callable[[str], Awaitable[ProbeResult]]

STATUS_TEXT = {
    "ready": "Ready",
    "dnd": "Do not disturb",
    "off": "Intercom off",
    "key": "Different key",
    "no_key": "No key set on the gateway",
    "encryption": "Encrypted (not supported yet)",
    "unreachable": "Unreachable",
    "unknown": "Not probed yet",
    "offline": "Offline",
}


@dataclass
class Kiosk:
    key: str  # roster key: kiosk id, or "manual:host:port" before the first probe
    address: str
    port: int = DEFAULT_ADMIN_PORT
    tls: bool = False
    id: str = ""
    name: str = ""
    version: str = ""
    host: str = ""
    manual: bool = False
    heard: bool = False  # currently announced over mDNS
    # From the identity probe.
    enabled: bool | None = None
    fingerprint: str | None = None
    dnd: bool = False
    intercom_port: int | None = None
    intercom_tls: bool | None = None
    probe_failed: bool = False
    probed_at: float | None = None
    # From Home Assistant.
    ha_device: str = ""
    ha_area: str = ""
    ha_state: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def admin_url(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{_host_for_url(self.address)}:{self.port}"

    @property
    def intercom_url(self) -> str:
        tls = self.tls if self.intercom_tls is None else self.intercom_tls
        port = self.port if self.intercom_port is None else self.intercom_port
        return f"{'https' if tls else 'http'}://{_host_for_url(self.address)}:{port}"

    @property
    def present(self) -> bool:
        return self.heard or self.manual

    def status(self, our_fingerprint: str) -> str:
        """Mirrors the kiosk's own Kiosks list, plus gateway-specific cases."""
        if not self.present:
            return "offline"
        if self.enabled is None:
            return "unreachable" if self.probe_failed else "unknown"
        if not self.enabled:
            return "off"
        if not our_fingerprint:
            return "no_key"
        if self.fingerprint != our_fingerprint:
            return "key"
        if self.intercom_tls:
            return "encryption"
        if self.dnd:
            return "dnd"
        return "ready"

    def to_json(self, our_fingerprint: str) -> dict[str, Any]:
        status = self.status(our_fingerprint)
        return {
            "id": self.id,
            "name": self.name or self.address,
            "address": self.address,
            "admin_port": self.port,
            "intercom_url": self.intercom_url if self.enabled else None,
            "version": self.version,
            "status": status,
            "status_text": STATUS_TEXT[status],
            "source": "manual" if self.manual and not self.heard else "mdns",
            "dnd": self.dnd,
            "area": self.ha_area,
            "ha_device": self.ha_device,
            "ha_intercom_state": self.ha_state,
            "probed_at": self.probed_at,
        }


def _host_for_url(address: str) -> str:
    return f"[{address}]" if ":" in address and not address.startswith("[") else address


def parse_manual(entry: str) -> tuple[str, int, bool]:
    """``host``, ``host:port``, ``https://host:port`` → (host, port, tls)."""
    text = entry.strip()
    tls = False
    if "://" in text:
        scheme, text = text.split("://", 1)
        tls = scheme.lower() == "https"
    text = text.rstrip("/")
    if text.startswith("["):  # [v6]:port
        host, _, rest = text[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else DEFAULT_ADMIN_PORT
        return host, port, tls
    if text.count(":") == 1:
        host, port_s = text.split(":")
        return host, int(port_s), tls
    return text, DEFAULT_ADMIN_PORT, tls


class Roster:
    def __init__(self, prober: Prober, key: str = "", probe_interval: float = 30.0) -> None:
        self._prober = prober
        self.key_fingerprint = fingerprint(key)
        self.probe_interval = probe_interval
        self.kiosks: dict[str, Kiosk] = {}
        self._listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()

    # ── change notification ──────────────────────────────────────────

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _changed(self) -> None:
        for listener in self._listeners:
            try:
                listener()
            except Exception:  # a listener must never break discovery
                _LOG.exception("roster listener failed")

    # ── sources ──────────────────────────────────────────────────────

    def add_manual(self, entry: str) -> Kiosk:
        host, port, tls = parse_manual(entry)
        for k in self.kiosks.values():
            if k.address == host and k.port == port:
                k.manual = True
                return k
        k = Kiosk(key=f"manual:{host}:{port}", address=host, port=port, tls=tls, manual=True)
        self.kiosks[k.key] = k
        self._changed()
        return k

    def heard(self, peer: dict[str, Any]) -> Kiosk | None:
        """An mDNS announcement (already parsed from TXT)."""
        kid = str(peer.get("id") or "")
        address = str(peer.get("address") or "")
        if not kid or not address:
            return None
        port = int(peer.get("port") or DEFAULT_ADMIN_PORT)
        tls = bool(peer.get("tls"))
        # A manual entry for the same endpoint becomes this kiosk.
        for mk in [k for k in self.kiosks.values() if k.key.startswith("manual:")]:
            if mk.address == address and mk.port == port:
                del self.kiosks[mk.key]
                mk.key = kid
                self.kiosks[kid] = mk
        k = self.kiosks.get(kid)
        if k is None:
            k = Kiosk(key=kid, address=address, port=port, tls=tls, id=kid)
            self.kiosks[kid] = k
        moved = (k.address, k.port, k.tls) != (address, port, tls)
        k.id = kid
        k.address, k.port, k.tls = address, port, tls
        k.name = str(peer.get("name") or k.name)
        k.version = str(peer.get("version") or k.version)
        k.host = str(peer.get("host") or k.host)
        k.heard = True
        if moved:
            self._reset_probe(k)
        self._changed()
        return k

    def lost(self, kid: str) -> None:
        k = self.kiosks.get(kid)
        if k is None:
            return
        k.heard = False
        if not k.manual:
            self._reset_probe(k)
        self._changed()

    @staticmethod
    def _reset_probe(k: Kiosk) -> None:
        k.enabled = None
        k.fingerprint = None
        k.probed_at = None
        k.probe_failed = False

    # ── probing ──────────────────────────────────────────────────────

    async def probe(self, k: Kiosk) -> None:
        url = k.admin_url
        result = await self._prober(f"{url}/api/intercom/identity")
        if k.admin_url != url:  # it moved while we asked
            return
        if result is not None and result[0] == 429:
            return  # rate limited; keep what we had, try next round
        k.probed_at = time.time()
        if result is None:
            k.probe_failed = True
            k.enabled = None
        elif result[0] == 404:  # a build without the intercom
            k.probe_failed = False
            k.enabled = False
        elif result[0] != 200 or not isinstance(result[1], dict):
            k.probe_failed = True
            k.enabled = None
        else:
            self._apply_identity(k, result[1])
        self._changed()

    def _apply_identity(self, k: Kiosk, data: dict[str, Any]) -> None:
        k.probe_failed = False
        k.enabled = bool(data.get("enabled"))
        k.fingerprint = str(data.get("key") or "")
        k.dnd = bool(data.get("dnd"))
        k.name = str(data.get("name") or k.name)
        k.version = str(data.get("version") or k.version)
        endpoint = data.get("endpoint")
        if isinstance(endpoint, dict) and endpoint.get("port"):
            k.intercom_port = int(endpoint["port"])
            k.intercom_tls = bool(endpoint.get("tls"))
        else:
            k.intercom_port = None
            k.intercom_tls = None
        kid = str(data.get("id") or "")
        if kid and not k.id:
            k.id = kid
        if kid and k.key != kid:
            # A manual entry learnt its id: merge with a known kiosk of that id.
            other = self.kiosks.get(kid)
            self.kiosks.pop(k.key, None)
            if other is not None and other is not k:
                k.heard = k.heard or other.heard
                k.manual = k.manual or other.manual
                k.host = k.host or other.host
            k.key = kid
            self.kiosks[kid] = k

    def due(self, now: float | None = None) -> list[Kiosk]:
        now = time.time() if now is None else now
        return [
            k
            for k in self.kiosks.values()
            if k.present and (k.probed_at is None or now - k.probed_at >= self.probe_interval)
        ]

    async def probe_due(self) -> None:
        async with self._lock:
            await asyncio.gather(*(self.probe(k) for k in self.due()))

    async def run(self, stop: asyncio.Event) -> None:
        """Probe whatever is due every couple of seconds until ``stop``."""
        while not stop.is_set():
            try:
                await self.probe_due()
            except Exception:
                _LOG.exception("probe round failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass

    # ── views ────────────────────────────────────────────────────────

    def find(self, target: str) -> Kiosk | None:
        """A kiosk by id, name (any case) or address, like the kiosks' own lookup."""
        t = target.strip().lower()
        for k in self.kiosks.values():
            if t in (k.id.lower(), k.address.lower()):
                return k
        matches = [k for k in self.kiosks.values() if k.name.lower() == t]
        return matches[0] if len(matches) == 1 else None

    def snapshot(self) -> list[dict[str, Any]]:
        order = {"ready": 0, "dnd": 1, "encryption": 2, "key": 3, "no_key": 3, "off": 4,
                 "unknown": 5, "unreachable": 6, "offline": 7}
        items = [k.to_json(self.key_fingerprint) for k in self.kiosks.values()]
        return sorted(items, key=lambda j: (order[j["status"]], j["name"].lower()))
