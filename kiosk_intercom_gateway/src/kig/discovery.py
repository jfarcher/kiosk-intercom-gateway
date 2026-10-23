"""mDNS browsing for ``_kiosk-satellite._tcp.local.``"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .roster import Roster

_LOG = logging.getLogger(__name__)

SERVICE = "_kiosk-satellite._tcp.local."


def _usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def parse_service(
    name: str, txt: dict[bytes, bytes | None], addresses: list[str], srv_port: int | None
) -> dict[str, Any] | None:
    """One resolved announcement → the peer dict :meth:`Roster.heard` takes."""
    entries = {
        k.decode(errors="replace").lower(): (v or b"").decode(errors="replace")
        for k, v in txt.items()
    }
    instance = name.removesuffix("." + SERVICE).removesuffix(SERVICE)
    kid = entries.get("id") or instance.removeprefix("ks-")
    usable = [a for a in addresses if _usable(a)]
    # Prefer IPv4: it is what kiosks announce and use themselves.
    usable.sort(key=lambda a: ":" in a)
    if not kid or not usable:
        return None
    try:
        port = int(entries.get("port") or srv_port or 0)
    except ValueError:
        port = srv_port or 0
    return {
        "id": kid,
        "name": entries.get("name") or instance,
        "version": entries.get("version", ""),
        "address": usable[0],
        "port": port or None,
        "tls": entries.get("tls") == "1",
        "host": entries.get("host", ""),
    }


def instance_id(name: str) -> str:
    return name.removesuffix("." + SERVICE).removesuffix(SERVICE).removeprefix("ks-")


class Discovery:
    def __init__(self, roster: Roster, interfaces: Any = None) -> None:
        self.roster = roster
        self._interfaces = interfaces
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # mDNS instance name → kiosk id, to forget the right one on removal.
        self._ids: dict[str, str] = {}

    async def start(self) -> None:
        kwargs = {"ip_version": IPVersion.V4Only}
        if self._interfaces:
            kwargs["interfaces"] = self._interfaces
        self._azc = AsyncZeroconf(**kwargs)
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, [SERVICE], handlers=[self._on_change]
        )
        _LOG.info("browsing %s", SERVICE)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.async_cancel()
        for t in list(self._tasks):
            t.cancel()
        if self._azc:
            await self._azc.async_close()

    def _on_change(
        self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if state_change is ServiceStateChange.Removed:
            kid = self._ids.pop(name, instance_id(name))
            _LOG.info("kiosk %s left", kid)
            self.roster.lost(kid)
            return
        task = asyncio.ensure_future(self._resolve(zeroconf, service_type, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000):
            _LOG.debug("could not resolve %s", name)
            return
        peer = parse_service(
            name, info.properties or {}, info.parsed_scoped_addresses(), info.port
        )
        if peer is None:
            _LOG.debug("ignoring %s: no id or usable address", name)
            return
        self._ids[name] = peer["id"]
        k = self.roster.heard(peer)
        if k is not None:
            _LOG.info("kiosk %s (%s) at %s:%s", k.name, k.id, k.address, k.port)
