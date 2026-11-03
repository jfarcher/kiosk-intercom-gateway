"""mDNS browsing for ``_kiosk-satellite._tcp.local.``"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

from zeroconf import DNSOutgoing, IPVersion, ServiceInfo, ServiceStateChange, Zeroconf, current_time_millis
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .roster import Roster

_FLAGS_QR_RESPONSE = 0x8000  # zeroconf's const values (not exported)
_FLAGS_AA = 0x0400

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
    """Browses for kiosks and, for the phones the gateway offers, announces
    them the way kiosks announce themselves.

    Kiosks (FleetDiscovery.kt) re-announce every 30 seconds, query only when
    they start, drop a peer after three missed announcements and only take an
    announcement whose PTR, SRV, TXT and A records arrive in one packet. So
    the gateway repeats every phone's full record set every 30 seconds itself;
    python-zeroconf alone announces once and then waits for the TTL.
    """

    ANNOUNCE_EVERY = 30.0

    def __init__(
        self, roster: Roster, interfaces: Any = None, browse: bool = True, ignore_prefix: str = ""
    ) -> None:
        self.roster = roster
        self._interfaces = interfaces
        self._browse = browse
        self.ignore_prefix = ignore_prefix  # our own phones, heard back
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # mDNS instance name → kiosk id, to forget the right one on removal.
        self._ids: dict[str, str] = {}
        self._advertised: dict[str, ServiceInfo] = {}
        self._announcer: asyncio.Task[None] | None = None

    async def start(self) -> None:
        kwargs = {"ip_version": IPVersion.V4Only}
        if self._interfaces:
            kwargs["interfaces"] = self._interfaces
        self._azc = AsyncZeroconf(**kwargs)
        if self._browse:
            self._browser = AsyncServiceBrowser(
                self._azc.zeroconf, [SERVICE], handlers=[self._on_change]
            )
            _LOG.info("browsing %s", SERVICE)
        self._announcer = asyncio.ensure_future(self._announce_loop())

    async def stop(self) -> None:
        if self._announcer:
            self._announcer.cancel()
        if self._browser:
            await self._browser.async_cancel()
        for t in list(self._tasks):
            t.cancel()
        if self._azc:
            for info in list(self._advertised.values()):
                try:
                    await self._azc.async_unregister_service(info)  # goodbyes, TTL 0
                except Exception as err:  # shutting down anyway
                    _LOG.debug("unregister %s: %s", info.name, err)
            self._advertised.clear()
            await self._azc.async_close()

    # ── announcing ───────────────────────────────────────────────────

    @staticmethod
    def service_info(
        kid: str, name: str, address: str, port: int, version: str, host: str
    ) -> ServiceInfo:
        """A kiosk-style announcement: ``ks-<id>`` with TXT id, name, version, port."""
        return ServiceInfo(
            SERVICE,
            f"ks-{kid}.{SERVICE}",
            addresses=[socket.inet_aton(address)],
            port=port,
            properties={"id": kid, "name": name[:200], "version": version, "port": str(port)},
            server=f"{host}.local.",
        )

    async def advertise(self, infos: dict[str, ServiceInfo]) -> None:
        """Makes ``infos`` (by id) the set announced: new ones registered,
        changed ones replaced, missing ones withdrawn with a goodbye."""
        assert self._azc is not None
        for kid in list(self._advertised):
            old = self._advertised[kid]
            new = infos.get(kid)
            if new is None or (new.port, new.properties, new.addresses) != (
                old.port, old.properties, old.addresses
            ):
                del self._advertised[kid]
                try:
                    await self._azc.async_unregister_service(old)
                except Exception as err:
                    _LOG.debug("unregister %s: %s", old.name, err)
        for kid, info in infos.items():
            if kid in self._advertised:
                continue
            try:
                await self._azc.async_register_service(info, cooperating_responders=True, strict=False)
            except Exception as err:
                _LOG.warning("could not announce %s: %s", info.name, err)
                continue
            self._advertised[kid] = info
        if infos:
            self.announce()

    def announce(self) -> None:
        """Every advertised record set in one response packet, unsolicited."""
        if self._azc is None or not self._advertised:
            return
        out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
        now = current_time_millis()
        for info in self._advertised.values():
            for record in (info.dns_pointer(), info.dns_service(), info.dns_text(), *info.dns_addresses()):
                out.add_answer_at_time(record, now)
        self._azc.zeroconf.async_send(out)

    async def _announce_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ANNOUNCE_EVERY)
            try:
                self.announce()
            except Exception:
                _LOG.exception("mDNS announcement failed")

    # ── browsing ─────────────────────────────────────────────────────

    def _ours(self, kid: str) -> bool:
        return bool(self.ignore_prefix) and kid.startswith(self.ignore_prefix)

    def _on_change(
        self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if self._ours(instance_id(name)):
            return
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
        if self._ours(peer["id"]):
            return
        self._ids[name] = peer["id"]
        k = self.roster.heard(peer)
        if k is not None:
            _LOG.info("kiosk %s (%s) at %s:%s", k.name, k.id, k.address, k.port)
