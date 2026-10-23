"""SIP → kiosk calls: Asterisk hands a call to this app over ARI (Stasis),
the gateway rings the kiosk over the intercom wire as if it were a kiosk,
and on answer bridges Asterisk's external media (slin16 RTP) to the
kiosk's audio WebSocket.

Kiosk Satellite's side of a call, as the gateway sees it (the gateway is the
caller):

1. ``POST <kiosk>/api/intercom/call`` ``{call, kind: "call", from}`` → ``ringing``
   or ``auto`` (answers itself after a countdown), else ``busy``/``dnd``/``off``/
   ``refused``, 403 for another key, 409 for encryption.
2. The kiosk answers by ``POST <gateway>/api/intercom/call/<id>`` ``{action:
   "answer"}`` to the address the call came from and the ``from.port``;
   ``decline`` and ``missed`` arrive the same way.
3. The caller (us) opens ``ws://<kiosk>/api/intercom/audio/<id>?token=`` within
   ten seconds. Binary frames both ways; ``{"type": "end"}`` hangs up.
4. Hanging up before the socket: ``cancel`` while ringing, ``hangup`` after.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .ari import Ari, AriError
from .protocol import TokenVerifier, issue_token
from .roster import Kiosk, Roster
from .rtp import RtpBridge, rms
from .state import State

_LOG = logging.getLogger(__name__)

# Why a call ended → the cause Asterisk gives the SIP caller.
HANGUP_CAUSE = {
    "ended": "normal",
    "declined": "busy",
    "busy": "busy",
    "dnd": "busy",
    "no_answer": "no_answer",
    "unknown_kiosk": "unallocated",
    "unavailable": "congestion",
    "failed": "congestion",
}
# Channels ARI creates for media work, never callers.
HELPER_CHANNELS = ("UnicastRTP/", "Snoop/", "Announcer/", "Recorder/", "WebSocket/")
# What the kiosk says about a call → why it ended.
SIGNAL_REASON = {"decline": "declined", "missed": "no_answer", "cancel": "ended", "hangup": "ended"}
TALK_LEVEL = 400.0  # RMS of 16-bit samples above which the SIP side counts as talking
TALK_HANGOVER_S = 0.8


@dataclass
class Call:
    id: str
    kiosk: Kiosk
    channel: str
    caller: str
    state: str = "ringing"  # ringing → connecting → in_call → ended
    started: float = field(default_factory=time.time)
    answered: float | None = None
    reason: str = ""
    sip_gone: bool = False
    ext_channel: str = ""
    bridge: str = ""
    rtp: RtpBridge | None = None
    ws: aiohttp.ClientWebSocketResponse | None = None
    outbox: asyncio.Queue[bytes | str | None] = field(default_factory=asyncio.Queue)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    talking: bool = False
    talk_seen: float = 0.0
    timer: asyncio.TimerHandle | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kiosk": self.kiosk.name,
            "extension": self.kiosk.extra.get("extension", ""),
            "caller": self.caller,
            "state": self.state,
            "since": self.answered or self.started,
            "reason": self.reason,
        }


def caller_label(channel: dict[str, Any]) -> str:
    caller = channel.get("caller") or {}
    name, number = str(caller.get("name") or "").strip(), str(caller.get("number") or "").strip()
    if name and number and name != number:
        return f"{name} ({number})"
    return name or number or "Phone"


def local_address_towards(host: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


class CallManager:
    def __init__(
        self,
        roster: Roster,
        state: State,
        ari: Ari | None,
        *,
        name: str = "Phone",
        extension_start: int = 801,
        ring_timeout: float = 60.0,
        listen_host: str = "0.0.0.0",
        listen_port: int = 0,
        rtp_host: str = "127.0.0.1",
    ) -> None:
        self.roster = roster
        self.state = state
        self.ari = ari
        self.name = name
        self.extension_start = extension_start
        self.ring_timeout = ring_timeout
        self.listen_host, self.listen_port = listen_host, listen_port
        self.rtp_host = rtp_host
        self.calls: dict[str, Call] = {}
        self.by_channel: dict[str, Call] = {}
        self.ext_channels: dict[str, Call] = {}
        self.history: list[dict[str, Any]] = []
        # Channels that left Stasis before their call was registered.
        self._gone: dict[str, float] = {}
        self._verifier = TokenVerifier(roster.key)
        self._http: aiohttp.ClientSession | None = None
        self._ws_http: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self.port = 0
        roster.on_change(self.assign_extensions)
        self.assign_extensions()

    # ── setup ────────────────────────────────────────────────────────

    def assign_extensions(self) -> None:
        for k in self.roster.kiosks.values():
            if k.id and "extension" not in k.extra:
                k.extra["extension"] = self.state.extension_for(k.id, self.extension_start)

    def kiosk_for(self, target: str) -> Kiosk | None:
        target = target.strip()
        kid = self.state.kiosk_for(target)
        if kid and kid in self.roster.kiosks:
            return self.roster.kiosks[kid]
        return self.roster.find(target) if target else None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))
        return self._http

    @property
    def ws_http(self) -> aiohttp.ClientSession:
        # No total timeout: it would cut a live call's socket.
        if self._ws_http is None:
            self._ws_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=6))
        return self._ws_http

    async def start(self) -> None:
        app = web.Application(client_max_size=65536)
        app.router.add_get("/api/intercom/identity", self._identity)
        app.router.add_post("/api/intercom/call", self._incoming)
        app.router.add_post("/api/intercom/call/{call}", self._signal)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.listen_host, self.listen_port)
        await site.start()
        self.port = int(self._runner.addresses[0][1])
        _LOG.info("intercom listener on port %d", self.port)

    async def stop(self) -> None:
        for c in list(self.calls.values()):
            await self.end(c, "ended")
        if self._runner:
            await self._runner.cleanup()
        for session in (self._http, self._ws_http):
            if session:
                await session.close()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.ari is not None,
            "connected": bool(self.ari and self.ari.connected),
            "error": self.ari.error if self.ari else "",
            "asterisk_version": self.ari.asterisk_version if self.ari else "",
            "app": self.ari.app if self.ari else "",
            "listener_port": self.port,
            "calls": [c.to_json() for c in self.calls.values()],
            "history": self.history[-10:],
        }

    # ── the intercom listener ────────────────────────────────────────

    async def _identity(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "id": self.state.gateway_id,
                "name": self.name,
                "version": __version__,
                "enabled": True,
                "key": self.roster.key_fingerprint,
                "dnd": False,
                "endpoint": {"port": self.port, "tls": False},
            }
        )

    async def _incoming(self, _: web.Request) -> web.Response:
        # Kiosk → SIP calls come in a later version.
        return web.json_response({"status": "off"})

    async def _signal(self, request: web.Request) -> web.Response:
        call_id = request.match_info["call"]
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        self._verifier.key = self.roster.key
        if not self._verifier.verify(token, call_id):
            return web.json_response({"ok": False, "error": "refused"}, status=403)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        c = self.calls.get(call_id)
        if c is None:
            return web.json_response({"ok": False, "error": "no such call"}, status=403)
        action = str((body or {}).get("action") or "")
        if action == "answer":
            if c.state != "ringing":
                return web.json_response({"ok": False, "error": "not calling"}, status=403)
            c.state = "connecting"
            if c.timer:
                c.timer.cancel()
            self._spawn(c, self._connect(c))
            return web.json_response({"ok": True})
        reason = SIGNAL_REASON.get(action)
        if reason is None:
            return web.json_response({"ok": False, "error": "unknown action"}, status=403)
        self._spawn(c, self.end(c, reason, tell_kiosk=False))
        return web.json_response({"ok": True})

    # ── ARI events ───────────────────────────────────────────────────

    async def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        channel = event.get("channel") or {}
        cid = str(channel.get("id") or "")
        if kind == "StasisStart":
            name = str(channel.get("name", ""))
            if cid in self.ext_channels or name.startswith(HELPER_CHANNELS):
                return  # our own media channels, snoops and the like
            args = event.get("args") or []
            if not args:
                # Only the dialplan's Stasis(app,<target>) brings callers here;
                # channels created over ARI by anyone else carry no target.
                _LOG.debug("ignoring %s: entered %s without a target", name, self.ari and self.ari.app)
                return
            target = str(args[0])
            asyncio.ensure_future(self.place(cid, target, caller_label(channel)))
        elif kind in ("StasisEnd", "ChannelDestroyed"):
            c = self.by_channel.get(cid)
            if c is not None:
                c.sip_gone = True
                asyncio.ensure_future(self.end(c, "ended"))
                return
            c = self.ext_channels.get(cid)
            if c is not None:
                if kind == "ChannelDestroyed":
                    asyncio.ensure_future(self.end(c, "failed"))
                return
            now = time.monotonic()
            self._gone = {k: t for k, t in self._gone.items() if now - t < 120}
            self._gone[cid] = now

    # ── placing a call ───────────────────────────────────────────────

    async def place(self, channel: str, target: str, caller: str) -> None:
        assert self.ari is not None
        k = self.kiosk_for(target)
        if k is None:
            _LOG.info("%s dialled %r: no kiosk by that extension or name", caller, target)
            await self._refuse(channel, "unknown_kiosk", caller, target)
            return
        if k.status(self.roster.key_fingerprint) in ("unknown", "unreachable"):
            await self.roster.probe(k)  # a fresh look before giving up
        status = k.status(self.roster.key_fingerprint)
        if status != "ready":
            _LOG.info("%s → %s refused here: kiosk is %s", caller, k.name, status)
            await self._refuse(channel, "dnd" if status == "dnd" else "unavailable", caller, k.name)
            return
        if channel in self._gone:
            _LOG.info("%s hung up before %s was rung", caller, k.name)
            return
        c = Call(id=self._call_id(), kiosk=k, channel=channel, caller=caller)
        self.calls[c.id] = c
        self.by_channel[channel] = c
        try:
            await self.ari.ring(channel)
        except AriError as err:
            _LOG.debug("ring %s: %s", channel, err)
        _LOG.info("%s calling %s (%s)", caller, k.name, c.id)
        status, code = await self._post(
            c,
            "/api/intercom/call",
            {"call": c.id, "kind": "call", "from": self._self_info(k, caller)},
        )
        if c.state == "ended":  # the caller gave up while we asked
            if status in ("ringing", "auto"):
                await self._post(c, f"/api/intercom/call/{c.id}", {"action": "cancel"})
            return
        if status in ("ringing", "auto"):
            loop = asyncio.get_running_loop()
            c.timer = loop.call_later(
                self.ring_timeout, lambda: asyncio.ensure_future(self._ring_timeout(c))
            )
            return
        if code == 403:
            _LOG.warning("%s refused the call: different intercom key", k.name)
        elif code == 409:
            _LOG.warning("%s refused the call: it requires encrypted intercom", k.name)
        else:
            _LOG.info("%s refused the call: %s", k.name, status)
        reason = {"busy": "busy", "dnd": "dnd"}.get(status, "unavailable")
        await self.end(c, reason, tell_kiosk=False)

    async def _ring_timeout(self, c: Call) -> None:
        if c.state == "ringing":
            await self.end(c, "no_answer")

    async def _refuse(self, channel: str, reason: str, caller: str, target: str) -> None:
        assert self.ari is not None
        try:
            await self.ari.hangup(channel, HANGUP_CAUSE[reason])
        except AriError as err:
            _LOG.debug("hangup %s: %s", channel, err)
        self.history.append({"caller": caller, "kiosk": target, "state": "ended", "reason": reason,
                             "at": time.time()})

    # ── the media ────────────────────────────────────────────────────

    async def _connect(self, c: Call) -> None:
        assert self.ari is not None
        try:
            await self.ari.answer(c.channel)
            c.rtp = await RtpBridge.open(lambda frame: self._from_sip(c, frame), self.rtp_host)
            host, port = c.rtp.local
            ext = await self.ari.external_media(host, port)
            c.ext_channel = str(ext["id"])
            self.ext_channels[c.ext_channel] = c
            vars_ = ext.get("channelvars") or {}
            r_host = vars_.get("UNICASTRTP_LOCAL_ADDRESS") or await self.ari.channel_var(
                c.ext_channel, "UNICASTRTP_LOCAL_ADDRESS"
            )
            r_port = vars_.get("UNICASTRTP_LOCAL_PORT") or await self.ari.channel_var(
                c.ext_channel, "UNICASTRTP_LOCAL_PORT"
            )
            if r_port:
                c.rtp.set_remote(str(r_host), int(r_port))
            c.bridge = await self.ari.bridge(c.channel, c.ext_channel)
            token = issue_token(self.roster.key, c.id, self.state.gateway_id)
            base = c.kiosk.intercom_url.replace("http", "ws", 1)
            c.ws = await self.ws_http.ws_connect(
                f"{base}/api/intercom/audio/{c.id}", params={"token": token}, heartbeat=20
            )
        except Exception as err:
            _LOG.warning("connecting %s to %s failed: %s", c.caller, c.kiosk.name, err)
            await self.end(c, "failed")
            return
        if c.state == "ended":
            return
        c.state, c.answered = "in_call", time.time()
        _LOG.info("%s and %s connected", c.caller, c.kiosk.name)
        c.outbox.put_nowait('{"type":"talk","on":false}')
        self._spawn(c, self._send_loop(c))
        self._spawn(c, self._receive_loop(c))

    def _from_sip(self, c: Call, frame: bytes) -> None:
        now = time.monotonic()
        loud = rms(frame) >= TALK_LEVEL
        if loud:
            c.talk_seen = now
        talking = loud or (c.talking and now - c.talk_seen < TALK_HANGOVER_S)
        if talking != c.talking:
            c.talking = talking
            c.outbox.put_nowait('{"type":"talk","on":%s}' % ("true" if talking else "false"))
        c.outbox.put_nowait(frame)

    async def _send_loop(self, c: Call) -> None:
        while c.ws is not None and not c.ws.closed:
            item = await c.outbox.get()
            if item is None:
                return
            try:
                if isinstance(item, str):
                    await c.ws.send_str(item)
                else:
                    await c.ws.send_bytes(item)
            except (ConnectionError, RuntimeError):
                return

    async def _receive_loop(self, c: Call) -> None:
        assert c.ws is not None
        async for msg in c.ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                if c.rtp:
                    c.rtp.send_frame(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                if '"end"' in msg.data:
                    break
            else:
                break
        await self.end(c, "ended", tell_kiosk=False)

    # ── ending ───────────────────────────────────────────────────────

    async def end(self, c: Call, reason: str, tell_kiosk: bool = True) -> None:
        if c.state == "ended":
            return
        previous, c.state, c.reason = c.state, "ended", reason
        if c.timer:
            c.timer.cancel()
        if tell_kiosk:
            if c.ws is not None and not c.ws.closed:
                try:
                    await c.ws.send_str('{"type":"end"}')
                except (ConnectionError, RuntimeError):
                    pass
            elif previous == "ringing":
                await self._post(c, f"/api/intercom/call/{c.id}", {"action": "cancel"})
            elif previous in ("connecting", "in_call"):
                await self._post(c, f"/api/intercom/call/{c.id}", {"action": "hangup"})
        c.outbox.put_nowait(None)
        if c.ws is not None:
            await c.ws.close()
        if c.rtp:
            c.rtp.close()
        if self.ari is not None:
            for task in (
                self.ari.destroy_bridge(c.bridge) if c.bridge else None,
                self.ari.hangup(c.ext_channel) if c.ext_channel else None,
                None if c.sip_gone else self.ari.hangup(c.channel, HANGUP_CAUSE.get(reason, "normal")),
            ):
                if task is not None:
                    try:
                        await task
                    except (AriError, aiohttp.ClientError, OSError) as err:
                        _LOG.debug("cleanup: %s", err)
        self.calls.pop(c.id, None)
        self.by_channel.pop(c.channel, None)
        self.ext_channels.pop(c.ext_channel, None)
        for t in c.tasks:
            if t is not asyncio.current_task():
                t.cancel()
        length = f", {time.time() - c.answered:.0f}s" if c.answered else ""
        _LOG.info("call %s → %s ended: %s%s", c.caller, c.kiosk.name, reason, length)
        self.history.append({**c.to_json(), "at": time.time()})
        del self.history[:-20]

    # ── helpers ──────────────────────────────────────────────────────

    def _spawn(self, c: Call, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        c.tasks.add(task)
        task.add_done_callback(c.tasks.discard)

    _next = 0

    def _call_id(self) -> str:
        CallManager._next += 1
        return f"{int(time.time() * 1000):x}-{CallManager._next:x}-kig"

    def _self_info(self, k: Kiosk, caller: str) -> dict[str, Any]:
        # The kiosk's call screen names whoever is in ``name``: the SIP caller.
        return {
            "id": self.state.gateway_id,
            "name": caller,
            "address": local_address_towards(k.address),
            "port": self.port,
            "version": __version__,
        }

    async def _post(self, c: Call, path: str, body: dict[str, Any]) -> tuple[str, int | None]:
        token = issue_token(self.roster.key, c.id, self.state.gateway_id)
        try:
            async with self.http.post(
                c.kiosk.intercom_url + path,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
                status = str((data or {}).get("status") or "") if isinstance(data, dict) else ""
                return status or ("ok" if r.status == 200 else "failed"), r.status
        except (aiohttp.ClientError, OSError, TimeoutError) as err:
            _LOG.debug("POST %s%s: %s", c.kiosk.intercom_url, path, err)
            return "unreachable", None
