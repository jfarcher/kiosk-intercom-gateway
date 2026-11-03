"""The SIP phones kiosks can call: listed from Asterisk, each offered to the
kiosks as if it were a kiosk.

A kiosk's call request names no target (``POST /api/intercom/call`` on the
peer's own port), so every phone gets a listener of its own: its own port,
its own mDNS announcement (``ks-<gateway id>-<extension>``) and its own
intercom identity. Phones come and go with their registration: only those
Asterisk reports as reachable are offered, so a phone that is switched off
leaves the kiosks' Call lists.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from . import __version__
from .ari import Ari, AriError
from .roster import Roster
from .state import State

_LOG = logging.getLogger(__name__)

AUTO_RESOURCE = re.compile(r"^\d{2,6}$")  # numbered extensions, not trunks
REFRESH_EVERY = 30.0

Handler = Callable[["Phone", web.Request], Awaitable[web.StreamResponse]]


@dataclass
class Phone:
    resource: str  # the PJSIP endpoint, its extension
    name: str
    state: str = "unknown"  # Asterisk's: online, offline, unknown
    busy: bool = False
    port: int = 0
    gateway_id: str = ""
    runner: web.AppRunner | None = None

    @property
    def id(self) -> str:
        return f"{self.gateway_id}-{self.resource}"

    @property
    def label(self) -> str:
        return self.name if self.resource in self.name else f"{self.name} ({self.resource})"

    @property
    def offered(self) -> bool:
        return self.state != "offline"

    def to_json(self) -> dict[str, Any]:
        return {"extension": self.resource, "name": self.label, "state": self.state,
                "busy": self.busy, "offered": self.offered and self.runner is not None, "port": self.port}


def parse_phones(entries: list[str]) -> dict[str, str]:
    """``["101", "102=Front door", "103: Office"]`` → ``{resource: name or ""}``."""
    out: dict[str, str] = {}
    for entry in entries:
        m = re.match(r"^\s*([^=:\s]+)\s*(?:[=:]\s*(.*?))?\s*$", str(entry))
        if m:
            out[m.group(1)] = m.group(2) or ""
    return out


def local_address_towards(host: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def default_address() -> str:
    """This machine's address on the default route (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


class PhoneDirectory:
    def __init__(
        self,
        ari: Ari,
        roster: Roster,
        state: State,
        discovery: Any,
        handlers: dict[str, Handler],
        *,
        configured: list[str] | None = None,
        advertise_address: str = "",
        listen_host: str = "0.0.0.0",
    ) -> None:
        self.ari = ari
        self.roster = roster
        self.state = state
        self.discovery = discovery
        self.handlers = handlers
        self.configured = parse_phones(configured or [])
        self.advertise_address = advertise_address
        self.listen_host = listen_host
        self.phones: dict[str, Phone] = {}  # the ones offered (or, offline, wanted)
        self.endpoints: dict[str, dict[str, Any]] = {}  # every endpoint Asterisk has
        self._names: dict[str, str] = {}
        self.error = ""

    # ── lookup ───────────────────────────────────────────────────────

    def by_id(self, pid: str) -> Phone | None:
        for p in self.phones.values():
            if p.id == pid:
                return p
        return None

    @property
    def auto(self) -> bool:
        """No list chosen: every numbered extension is offered."""
        return not self.configured

    def selected(self, resource: str) -> bool:
        return resource in self.configured if self.configured else bool(AUTO_RESOURCE.match(resource))

    def snapshot(self) -> list[dict[str, Any]]:
        """Every endpoint, for the page's Phones card, offered ones marked."""
        out = []
        for resource, e in sorted(self.endpoints.items()):
            p = self.phones.get(resource)
            out.append({
                "extension": resource,
                "name": p.label if p else e.get("callerid") or resource,
                "callerid": e.get("callerid", ""),
                "custom_name": self.configured.get(resource, ""),
                "state": e.get("state", "unknown"),
                "busy": bool(e.get("channel_ids")),
                "selected": self.selected(resource),
                "offered": bool(p and p.runner is not None),
                "port": p.port if p else 0,
            })
        return out

    async def choose(self, entries: list[str] | None) -> list[str]:
        """New selection from the page: ``None`` for automatic, else entries as
        the ``phones`` option takes them. Applied at once; returns them."""
        self.configured = parse_phones(entries or [])
        await self.refresh()
        return [f"{r}={n}" if n else r for r, n in self.configured.items()]

    @property
    def address(self) -> str:
        if self.advertise_address:
            return self.advertise_address
        for k in self.roster.kiosks.values():
            if k.present and k.address:
                found = local_address_towards(k.address)
                if found:
                    return found
        return default_address()

    # ── keeping in step with Asterisk ────────────────────────────────

    async def refresh(self) -> None:
        if not self.ari.connected:
            listed: list[dict[str, Any]] = []
        else:
            try:
                listed = await self.ari.endpoints()
                self.error = ""
            except AriError as err:
                self.error = str(err)
                _LOG.warning("listing Asterisk's phones failed: %s", err)
                return
        wanted: dict[str, dict[str, Any]] = {}
        self.endpoints = {}
        for e in listed:
            resource = str(e.get("resource") or "")
            if not resource:
                continue
            self.endpoints[resource] = {**e, "callerid": await self._callerid(resource)}
            if self.selected(resource):
                wanted[resource] = e
        for resource in list(self.phones):
            if resource not in wanted:
                await self._drop(self.phones.pop(resource))
        for resource, e in wanted.items():
            callerid = self.endpoints[resource]["callerid"]
            name = self.configured.get(resource) or callerid or f"Phone {resource}"
            p = self.phones.get(resource)
            if p is None:
                p = Phone(resource=resource, name=name, gateway_id=self.state.gateway_id)
                self.phones[resource] = p
            p.name = name
            p.state = str(e.get("state") or "unknown")
            p.busy = bool(e.get("channel_ids"))
            if p.offered and p.runner is None:
                await self._listen(p)
            elif not p.offered and p.runner is not None:
                await self._drop(p, keep=True)
        await self._advertise()

    async def _callerid(self, resource: str) -> str:
        if resource not in self._names:  # asked once per endpoint; callerids change rarely
            self._names[resource] = await self.ari.endpoint_callerid(resource)
        return self._names[resource]

    async def _listen(self, p: Phone) -> None:
        app = web.Application(client_max_size=65536)
        for method, path, key in (
            ("GET", "/", "page"),
            ("GET", "/api/intercom/identity", "identity"),
            ("POST", "/api/intercom/call", "call"),
            ("POST", "/api/intercom/call/{call}", "signal"),
            ("GET", "/api/intercom/audio/{call}", "audio"),
        ):
            handler = self.handlers.get(key) or self._page
            app.router.add_route(method, path, _bind(handler, p))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        preferred = self.state.phone_ports.get(p.resource, 0)
        for port in ((preferred, 0) if preferred else (0,)):
            try:
                site = web.TCPSite(runner, self.listen_host, port)
                await site.start()
                break
            except OSError:
                if port == 0:
                    await runner.cleanup()
                    raise
        p.runner = runner
        p.port = int(runner.addresses[0][1])
        if self.state.phone_ports.get(p.resource) != p.port:
            self.state.phone_ports[p.resource] = p.port
            self.state.save()
        _LOG.info("offering %s to the kiosks on port %d", p.label, p.port)

    async def _drop(self, p: Phone, keep: bool = False) -> None:
        if p.runner is not None:
            await p.runner.cleanup()
            p.runner = None
            _LOG.info("%s is %s: no longer offered", p.label, p.state if keep else "gone from Asterisk")

    async def _advertise(self) -> None:
        if self.discovery is None:
            return
        address = self.address
        if not address:
            _LOG.warning("no address to announce phones on; set Advertise address")
            return
        host = self.state.gateway_id
        infos = {
            p.id: self.discovery.service_info(p.id, p.label, address, p.port, __version__, host)
            for p in self.phones.values()
            if p.runner is not None
        }
        await self.discovery.advertise(infos)

    async def run(self, stop: asyncio.Event) -> None:
        """Refreshes every 30 s, and at once whenever the ARI connection comes
        up or goes down, so phones show up right after a (re)start."""
        loop = asyncio.get_running_loop()
        was_connected: bool | None = None
        next_at = 0.0
        while not stop.is_set():
            connected = self.ari.connected
            if connected != was_connected or loop.time() >= next_at:
                was_connected, next_at = connected, loop.time() + REFRESH_EVERY
                try:
                    await self.refresh()
                except Exception:
                    _LOG.exception("refreshing phones failed")
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except TimeoutError:
                pass

    async def close(self) -> None:
        for p in self.phones.values():
            await self._drop(p, keep=True)
        if self.discovery is not None:
            await self.discovery.advertise({})

    @staticmethod
    async def _page(p: Phone, _: web.Request) -> web.Response:
        # Kiosks list these ports in their remote-admin switcher too.
        return web.Response(
            content_type="text/html",
            text=f"<!doctype html><meta charset=utf-8><title>{html.escape(p.label)}</title>"
            "<body style='font:16px system-ui;margin:2em'>"
            f"<h1>{html.escape(p.label)}</h1><p>This is a SIP phone offered to Kiosk Satellite's intercom "
            "by Kiosk Intercom Gateway. Call it from a kiosk's <b>Call a kiosk</b> screen. It has no "
            "remote admin.</p>",
        )


def _bind(handler: Handler, p: Phone) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def bound(request: web.Request) -> web.StreamResponse:
        return await handler(p, request)

    return bound
