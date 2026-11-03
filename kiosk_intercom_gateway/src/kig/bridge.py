"""Calls between SIP phones and kiosks, both ways.

SIP → kiosk: Asterisk hands a call to this app over ARI (Stasis), the gateway
rings the kiosk over the intercom wire as if it were a kiosk, and on answer
bridges Asterisk's external media (slin16 over AudioSocket) to the kiosk's
audio WebSocket.

Kiosk → SIP: every phone is offered to the kiosks as a kiosk of its own
(phones.py). A kiosk's call to one is originated over ARI; when the phone
answers, the gateway tells the kiosk, which opens its audio socket to the
phone's listener. The roles of the wire below swap: the kiosk is the caller.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .ari import Ari, AriError
from .protocol import TokenVerifier, issue_token
from .roster import Kiosk, Roster
from .media import AudioSocketServer, MediaLeg, rms
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
HELPER_CHANNELS = ("AudioSocket/", "UnicastRTP/", "Snoop/", "Announcer/", "Recorder/", "WebSocket/")
# Why an originated call to a phone never got answered (Q.850 cause) → reason.
CAUSE_REASON = {17: "busy", 18: "no_answer", 19: "no_answer", 21: "declined", 20: "unavailable",
                1: "unavailable", 3: "unavailable", 27: "unavailable", 34: "unavailable", 38: "unavailable"}
# Why a kiosk → phone call ended before it was answered → what the kiosk is told.
UNANSWERED_SIGNAL = {"no_answer": "missed"}  # anything else: decline
OUT_ARGS = "kig-out"  # the appArgs that mark our own originated calls
# What the kiosk says about a call → why it ended.
SIGNAL_REASON = {"decline": "declined", "missed": "no_answer", "cancel": "ended", "hangup": "ended"}
TALK_LEVEL = 400.0  # RMS of 16-bit samples above which the SIP side counts as talking
TALK_HANGOVER_S = 0.8


@dataclass
class Call:
    id: str
    kiosk: Kiosk
    channel: str  # the SIP side's channel ("" until an originated call has one)
    caller: str  # the SIP side, as a label
    direction: str = "in"  # in: phone → kiosk; out: kiosk → phone
    peer_url: str = ""  # where the kiosk takes signals about this call
    phone: str = ""  # out: the phone's extension; in: the caller's number
    state: str = "ringing"  # ringing → connecting → in_call → ended
    started: float = field(default_factory=time.time)
    answered: float | None = None
    reason: str = ""
    sip_gone: bool = False
    ext_channel: str = ""
    bridge: str = ""
    media: MediaLeg | None = None
    ws: Any = None  # the kiosk's audio socket: ours as client (in) or as server (out)
    ws_ready: asyncio.Event = field(default_factory=asyncio.Event)
    outbox: asyncio.Queue[bytes | str | None] = field(default_factory=asyncio.Queue)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    # Tasks that must run to the end: cancelled mid-connect, a socket the
    # kiosk already accepted would be left open. They see the call ended.
    protected: set[asyncio.Task[Any]] = field(default_factory=set)
    ringback: str = ""  # a playback on an already answered SIP side (the call action)
    talking: bool = False
    talk_seen: float = 0.0
    timer: asyncio.TimerHandle | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction,
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


def _host(address: str) -> str:
    return f"[{address}]" if ":" in address and not address.startswith("[") else address


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
        media_host: str = "127.0.0.1",
    ) -> None:
        self.roster = roster
        self.state = state
        self.ari = ari
        self.name = name
        self.extension_start = extension_start
        self.ring_timeout = ring_timeout
        self.listen_host, self.listen_port = listen_host, listen_port
        self.media_server = AudioSocketServer(media_host)
        self.calls: dict[str, Call] = {}
        self.by_channel: dict[str, Call] = {}
        self.ext_channels: dict[str, Call] = {}
        self.history: list[dict[str, Any]] = []
        # Channels that left Stasis before their call was registered, and the
        # cause they were destroyed with.
        self._gone: dict[str, float] = {}
        self._gone_cause: dict[str, int] = {}
        self._verifier = TokenVerifier(roster.key)
        self._http: aiohttp.ClientSession | None = None
        self._ws_http: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self.port = 0
        self.phones: Any = None  # PhoneDirectory, when kiosks may call phones
        # Told of every call as it rings, is answered and ends (Home Assistant events).
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.out_ring_timeout = min(ring_timeout, 30.0)  # a calling kiosk gives up at 30 s + 10
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
        await self.media_server.start()
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
        await self.media_server.stop()
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
            "media_port": self.media_server.port,
            "calls": [c.to_json() for c in self.calls.values()],
            "phones": self.phones.snapshot() if self.phones else [],
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
            if c.direction != "in" or c.state != "ringing":
                return web.json_response({"ok": False, "error": "not calling"}, status=403)
            c.state = "connecting"
            if c.timer:
                c.timer.cancel()
            self._spawn(c, self._connect(c), protect=True)
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
            if str(args[0]) == OUT_ARGS:
                c = self.calls.get(str(args[1]) if len(args) > 1 else "")
                if c is not None and c.direction == "out":
                    c.channel = cid
                    self.by_channel[cid] = c
                    self._spawn(c, self._phone_answered(c), protect=True)
                else:  # its kiosk gave up meanwhile
                    asyncio.ensure_future(self._hangup_quietly(cid))
                return
            target = str(args[0])
            answered = str(channel.get("state") or "") == "Up"
            # A call the gateway itself started (the call action) says who the
            # SIP side is in its appArgs: target,label,number.
            label = str(args[1]) if len(args) > 1 and args[1] else caller_label(channel)
            number = str(args[2]) if len(args) > 2 else str((channel.get("caller") or {}).get("number") or "")
            asyncio.ensure_future(self.place(cid, target, label, number, answered=answered))
        elif kind in ("StasisEnd", "ChannelDestroyed"):
            c = self.by_channel.get(cid)
            if c is not None:
                c.sip_gone = True
                reason = "ended"
                if c.direction == "out" and c.state == "ringing":
                    reason = CAUSE_REASON.get(int(event.get("cause") or 0), "declined")
                asyncio.ensure_future(self.end(c, reason))
                return
            c = self.ext_channels.get(cid)
            if c is not None:
                if kind == "ChannelDestroyed":
                    asyncio.ensure_future(self.end(c, "failed"))
                return
            now = time.monotonic()
            self._gone = {k: t for k, t in self._gone.items() if now - t < 120}
            self._gone_cause = {k: v for k, v in self._gone_cause.items() if k in self._gone}
            self._gone[cid] = now
            if kind == "ChannelDestroyed":
                self._gone_cause[cid] = int(event.get("cause") or 0)

    # ── placing a call ───────────────────────────────────────────────

    async def place(
        self, channel: str, target: str, caller: str, number: str = "", answered: bool = False
    ) -> None:
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
        c = Call(id=self._call_id(), kiosk=k, channel=channel, caller=caller, peer_url=k.intercom_url,
                 phone=number)
        self.calls[c.id] = c
        self.by_channel[channel] = c
        try:
            if answered:  # a phone that picked up a call action: ringback until the kiosk answers
                c.ringback = await self.ari.play(channel, "tone:ring")
            else:
                await self.ari.ring(channel)
        except AriError as err:
            _LOG.debug("ring %s: %s", channel, err)
        _LOG.info("%s calling %s (%s)", caller, k.name, c.id)
        status, code = await self._post(
            c,
            "/api/intercom/call",
            {"call": c.id, "kind": "call", "from": self._self_info(k, caller, number)},
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
            self._emit(c, "ringing")
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
        k = self.kiosk_for(target) if target else None
        self._tell({"call_id": "", "direction": "phone_to_kiosk", "state": "ended", "reason": reason,
                    "kiosk": k.name if k else target, "kiosk_id": k.id if k else "",
                    "kiosk_extension": k.extra.get("extension", "") if k else "",
                    "phone": caller, "phone_number": "", "duration": 0})

    # ── the media ────────────────────────────────────────────────────

    async def _bridge_media(self, c: Call) -> None:
        """Asterisk's side of the audio: an AudioSocket leg bridged to the call."""
        assert self.ari is not None
        c.media = MediaLeg(c.id, lambda frame: self._from_sip(c, frame))
        self.media_server.expect(c.media)
        ext = await self.ari.external_media(self.media_server.host, self.media_server.port, c.media.uuid)
        c.ext_channel = str(ext["id"])
        self.ext_channels[c.ext_channel] = c
        await asyncio.wait_for(c.media.connected.wait(), 5)
        c.bridge = await self.ari.bridge(c.channel, c.ext_channel)

    def _go_live(self, c: Call) -> None:
        c.state, c.answered = "in_call", time.time()
        self._emit(c, "answered")
        a, b = (c.caller, c.kiosk.name) if c.direction == "in" else (c.kiosk.name, c.caller)
        _LOG.info("%s and %s connected", a, b)
        c.outbox.put_nowait('{"type":"talk","on":false}')
        self._spawn(c, self._send_loop(c))

    async def _connect(self, c: Call) -> None:
        assert self.ari is not None
        try:
            if c.ringback:
                await self.ari.stop_playback(c.ringback)
                c.ringback = ""
            await self.ari.answer(c.channel)
            await self._bridge_media(c)
            token = issue_token(self.roster.key, c.id, self.state.gateway_id)
            base = c.kiosk.intercom_url.replace("http", "ws", 1)
            c.ws = await self.ws_http.ws_connect(
                f"{base}/api/intercom/audio/{c.id}", params={"token": token}, heartbeat=20
            )
        except Exception as err:
            _LOG.warning("connecting %s to %s failed: %s", c.caller, c.kiosk.name, err)
            await self.end(c, "failed")
            return
        if c.state == "ended":  # hung up while we were connecting
            await c.ws.close()
            return
        self._go_live(c)
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
                if c.media:
                    c.media.send_frame(msg.data)
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
        if c.ringback and self.ari is not None and not c.sip_gone:
            try:
                await self.ari.stop_playback(c.ringback)
            except (AriError, aiohttp.ClientError, OSError, TimeoutError):
                pass
        if tell_kiosk:
            if c.ws is not None and not c.ws.closed:
                try:
                    await c.ws.send_str('{"type":"end"}')
                except (ConnectionError, RuntimeError):
                    pass
            elif previous == "ringing":
                # We rang the kiosk: cancel. The kiosk rang a phone: say why not.
                action = "cancel" if c.direction == "in" else UNANSWERED_SIGNAL.get(reason, "decline")
                await self._post(c, f"/api/intercom/call/{c.id}", {"action": action})
            elif previous in ("connecting", "in_call"):
                await self._post(c, f"/api/intercom/call/{c.id}", {"action": "hangup"})
        c.outbox.put_nowait(None)
        if c.ws is not None:
            await c.ws.close()
        if c.media:
            self.media_server.forget(c.media)
            await c.media.close()
        if self.ari is not None:
            ari = self.ari
            steps = []
            if c.bridge:
                steps.append(lambda: ari.destroy_bridge(c.bridge))
            if c.ext_channel:
                steps.append(lambda: ari.hangup(c.ext_channel))
            if not c.sip_gone and c.channel:
                steps.append(lambda: ari.hangup(c.channel, HANGUP_CAUSE.get(reason, "normal")))
            for step in steps:  # created one at a time, so none is left unawaited
                try:
                    await step()
                except (AriError, aiohttp.ClientError, OSError, TimeoutError) as err:
                    _LOG.debug("cleanup: %s", err)
        self.calls.pop(c.id, None)
        self.by_channel.pop(c.channel, None)
        self.ext_channels.pop(c.ext_channel, None)
        for t in c.tasks:
            if t is not asyncio.current_task() and t not in c.protected:
                t.cancel()
        length = f", {time.time() - c.answered:.0f}s" if c.answered else ""
        a, b = (c.caller, c.kiosk.name) if c.direction == "in" else (c.kiosk.name, c.caller)
        _LOG.info("call %s → %s ended: %s%s", a, b, reason, length)
        self.history.append({**c.to_json(), "at": time.time()})
        self._emit(c, "ended")
        del self.history[:-20]

    # ── kiosk → phone ────────────────────────────────────────────────
    # What each phone's own listener (phones.py) does, as a kiosk would.

    def _calls_with(self, phone: Any) -> list[Call]:
        return [c for c in self.calls.values() if c.direction == "out" and c.phone == phone.resource]

    async def phone_identity(self, phone: Any, request: web.Request) -> web.Response:
        if request.remote:  # a kiosk that has found this phone (or the page's own checks)
            phone.seen_by[request.remote] = time.time()
        return web.json_response(
            {
                "id": phone.id,
                "name": phone.label,
                "version": __version__,
                "enabled": bool(self.ari and self.ari.connected),
                "key": self.roster.key_fingerprint,
                "dnd": False,
                "endpoint": {"port": phone.port, "tls": False},
            }
        )

    async def phone_call(self, phone: Any, request: web.Request) -> web.Response:
        """A kiosk calls this phone: ring it over ARI, answer ``ringing`` at once."""
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        call_id = str(body.get("call") or "")
        frm = body.get("from") if isinstance(body.get("from"), dict) else {}
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not call_id or not frm.get("id"):
            return web.json_response({"error": "call and from required"}, status=400)
        self._verifier.key = self.roster.key
        if not self._verifier.verify(token, call_id):
            return web.json_response({"status": "key"}, status=403)
        if frm.get("tls"):
            return web.json_response({"status": "tls"}, status=409)
        if body.get("kind") == "broadcast":
            return web.json_response({"status": "refused"})  # Announce to all skips phones
        if self.ari is None or not self.ari.connected:
            return web.json_response({"status": "off"})
        if phone.busy or self._calls_with(phone) or call_id in self.calls:
            return web.json_response({"status": "busy"})
        k = self.roster.kiosks.get(str(frm["id"]))
        address = request.remote or str(frm.get("address") or "")
        port = int(frm.get("port") or 2324)
        if k is None:  # a kiosk the gateway has not heard of yet
            k = Kiosk(key=str(frm["id"]), address=address, port=port, id=str(frm["id"]))
            k.name = str(frm.get("name") or address)
        c = Call(
            id=call_id, kiosk=k, channel="", caller=phone.label, direction="out",
            peer_url=f"http://{_host(address)}:{port}", phone=phone.resource,
        )
        self.calls[c.id] = c
        _LOG.info("%s calling %s (%s)", k.name, phone.label, c.id)
        self._spawn(c, self._dial_phone(c, phone))
        return web.json_response({"status": "ringing"})

    async def _dial_phone(self, c: Call, phone: Any) -> None:
        assert self.ari is not None
        ext = c.kiosk.extra.get("extension", "")
        caller_id = f'"{c.kiosk.name}" <{ext}>' if ext else f'"{c.kiosk.name}"'
        try:
            ch = await self.ari.originate(
                f"PJSIP/{phone.resource}", f"{OUT_ARGS},{c.id}", caller_id, int(self.out_ring_timeout)
            )
        except (AriError, aiohttp.ClientError, OSError, TimeoutError) as err:
            _LOG.warning("could not ring %s: %s", phone.label, err)
            await self.end(c, "unavailable")
            return
        cid = str((ch or {}).get("id") or "")
        if c.state == "ended":  # the kiosk gave up while Asterisk dialled
            await self._hangup_quietly(cid)
            return
        if cid and not c.channel:
            c.channel = cid
            self.by_channel[cid] = c
        if cid in self._gone_cause:  # refused before we had its id
            c.sip_gone = True
            await self.end(c, CAUSE_REASON.get(self._gone_cause[cid], "declined"))
            return
        loop = asyncio.get_running_loop()
        c.timer = loop.call_later(
            self.out_ring_timeout + 5, lambda: asyncio.ensure_future(self._ring_timeout(c))
        )
        self._emit(c, "ringing")

    async def _phone_answered(self, c: Call) -> None:
        """The phone picked up: bridge its audio, tell the kiosk, take its socket."""
        if c.state != "ringing":
            return
        c.state = "connecting"
        if c.timer:
            c.timer.cancel()
        try:
            await self._bridge_media(c)
        except Exception as err:
            _LOG.warning("connecting %s to %s failed: %s", c.kiosk.name, c.caller, err)
            await self.end(c, "failed")
            return
        status, code = await self._post(c, f"/api/intercom/call/{c.id}", {"action": "answer"})
        if c.state == "ended":
            return
        if code != 200:
            _LOG.info("%s no longer takes the call (%s)", c.kiosk.name, code or status)
            await self.end(c, "ended", tell_kiosk=False)
            return
        try:
            await asyncio.wait_for(c.ws_ready.wait(), 10)
        except TimeoutError:
            _LOG.warning("%s did not open its audio socket", c.kiosk.name)
            await self.end(c, "failed")

    async def phone_signal(self, phone: Any, request: web.Request) -> web.Response:
        return await self._signal(request)

    async def phone_audio(self, phone: Any, request: web.Request) -> web.StreamResponse:
        call_id = request.match_info["call"]
        self._verifier.key = self.roster.key
        c = self.calls.get(call_id)
        if (
            not self._verifier.verify(request.query.get("token", ""), call_id)
            or c is None
            or c.direction != "out"
            or c.state != "connecting"
        ):
            return web.json_response({"error": "refused"}, status=403)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        c.ws = ws
        c.ws_ready.set()
        self._go_live(c)
        await self._receive_loop(c)
        return ws

    async def _hangup_quietly(self, channel: str) -> None:
        if self.ari is None or not channel:
            return
        try:
            await self.ari.hangup(channel)
        except (AriError, aiohttp.ClientError, OSError, TimeoutError) as err:
            _LOG.debug("hangup %s: %s", channel, err)

    # ── actions (Home Assistant) ─────────────────────────────────────

    async def action_call(self, phone: str, kiosk: str) -> str:
        """Rings ``phone`` and, once it answers, the kiosk: click-to-call. The
        phone shows the kiosk calling; the kiosk shows the phone. Returns ''
        or why not."""
        if self.ari is None or not self.ari.connected:
            return "Asterisk is not connected"
        k = self.kiosk_for(kiosk)
        if k is None:
            return f"no kiosk by the name or extension {kiosk!r}"
        phone = phone.strip()
        endpoint = phone if "/" in phone else f"PJSIP/{phone}"
        number = phone.rsplit("/", 1)[-1]
        known = self.phones.phones.get(number) if self.phones else None
        if known:
            label = known.label
        else:
            cid = await self.ari.endpoint_callerid(number)
            label = f"{cid} ({number})" if cid else f"Phone {number}"
        label = label.replace(",", " ")  # ARI splits appArgs on commas
        ext = k.extra.get("extension", "")
        target = ext or k.id
        try:
            await self.ari.originate(
                endpoint, f"{target},{label},{number}", f'"{k.name}" <{ext}>' if ext else f'"{k.name}"',
                int(self.out_ring_timeout),
            )
        except (AriError, aiohttp.ClientError, OSError, TimeoutError) as err:
            return f"could not ring {phone}: {err}"
        _LOG.info("call action: ringing %s, then %s", label, k.name)
        return ""

    async def action_hangup(self, kiosk: str = "", phone: str = "", call: str = "") -> int:
        """Ends the calls that match (all of them when nothing is named); how many."""
        k = self.kiosk_for(kiosk) if kiosk else None
        if kiosk and k is None:
            return 0
        matched = [
            c for c in list(self.calls.values())
            if (not call or c.id == call)
            and (not k or c.kiosk.id == k.id)
            and (not phone or c.phone == phone.rsplit("/", 1)[-1])
        ]
        for c in matched:
            await self.end(c, "ended")
        return len(matched)

    # ── telling Home Assistant ───────────────────────────────────────

    def _emit(self, c: Call, state: str) -> None:
        data = {
            "call_id": c.id,
            "direction": "phone_to_kiosk" if c.direction == "in" else "kiosk_to_phone",
            "state": state,  # ringing, answered, ended
            "kiosk": c.kiosk.name,
            "kiosk_id": c.kiosk.id,
            "kiosk_extension": c.kiosk.extra.get("extension", ""),
            "phone": c.caller,
            "phone_number": c.phone,
        }
        if state == "ended":
            data["reason"] = c.reason
            data["duration"] = round(time.time() - c.answered) if c.answered else 0
        self._tell(data)

    def _tell(self, data: dict[str, Any]) -> None:
        for listener in self.listeners:
            try:
                listener(data)
            except Exception:
                _LOG.exception("call listener failed")

    # ── helpers ──────────────────────────────────────────────────────

    def _spawn(self, c: Call, coro: Any, protect: bool = False) -> None:
        task = asyncio.ensure_future(coro)
        c.tasks.add(task)
        task.add_done_callback(c.tasks.discard)
        if protect:
            c.protected.add(task)
            task.add_done_callback(c.protected.discard)

    _next = 0

    def _call_id(self) -> str:
        CallManager._next += 1
        return f"{int(time.time() * 1000):x}-{CallManager._next:x}-kig"

    def _self_info(self, k: Kiosk, caller: str, number: str = "") -> dict[str, Any]:
        # The kiosk's call screen names whoever is in ``name``: the SIP caller.
        # A caller the kiosks can call (an offered phone) gives its own id, so
        # the kiosk's Call back after a missed call rings that phone.
        phone = self.phones.phones.get(number) if self.phones and number else None
        return {
            "id": phone.id if phone is not None and phone.runner is not None else self.state.gateway_id,
            "name": caller,
            "address": local_address_towards(k.address),
            "port": self.port,
            "version": __version__,
        }

    async def _post(self, c: Call, path: str, body: dict[str, Any]) -> tuple[str, int | None]:
        token = issue_token(self.roster.key, c.id, self.state.gateway_id)
        try:
            async with self.http.post(
                (c.peer_url or c.kiosk.intercom_url) + path,
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
            _LOG.debug("POST %s%s: %s", c.peer_url or c.kiosk.intercom_url, path, err)
            return "unreachable", None
