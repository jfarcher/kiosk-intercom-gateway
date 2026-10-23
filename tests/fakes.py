"""A fake Kiosk Satellite (as callee) and a fake Asterisk ARI, faithful to the
wire behaviour the gateway relies on."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import uuid
from array import array
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from kig.protocol import TokenVerifier, fingerprint, issue_token


def le_samples(start: int, count: int) -> bytes:
    """A recognisable ramp of little-endian samples."""
    return array("h", ((start + i) % 30000 - 15000 for i in range(count))).tobytes()


def ints(le: bytes) -> list[int]:
    a = array("h")
    a.frombytes(le)
    return list(a)


class FakeKiosk:
    def __init__(self, key: str, name: str = "Kitchen", kid: str = "kitchen-id") -> None:
        self.key, self.name, self.id = key, name, kid
        self.verifier = TokenVerifier(key)
        self.mode = "ring"  # ring, busy, dnd, off
        self.dnd = False
        self.calls: dict[str, dict[str, Any]] = {}
        self.signals: list[tuple[str, str]] = []  # (call id, action) from the gateway
        self.texts: list[str] = []
        self.frames: list[bytes] = []
        self.ws: web.WebSocketResponse | None = None
        self.ws_open = asyncio.Event()
        self.ws_closed = asyncio.Event()
        self.server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/intercom/identity", self._identity)
        app.router.add_post("/api/intercom/call", self._call)
        app.router.add_post("/api/intercom/call/{call}", self._signal)
        app.router.add_get("/api/intercom/audio/{call}", self._audio)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.port

    async def close(self) -> None:
        if self.server:
            await self.server.close()

    async def _identity(self, _: web.Request) -> web.Response:
        return web.json_response({
            "id": self.id, "name": self.name, "version": "2026.10.9", "enabled": True,
            "key": fingerprint(self.key), "dnd": self.dnd, "endpoint": {"port": self.port, "tls": False},
        })

    def _token(self, request: web.Request, call: str) -> bool:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        return self.verifier.verify(token, call)

    async def _call(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not self._token(request, body["call"]):
            return web.json_response({"status": "key"}, status=403)
        if self.mode != "ring":
            return web.json_response({"status": self.mode})
        frm = body["from"]
        self.calls[body["call"]] = {
            "from": frm, "kind": body["kind"], "address": request.remote, "port": frm["port"],
        }
        return web.json_response({"status": "ringing"})

    async def _signal(self, request: web.Request) -> web.Response:
        call = request.match_info["call"]
        if not self._token(request, call):
            return web.json_response({"ok": False}, status=403)
        self.signals.append((call, (await request.json())["action"]))
        return web.json_response({"ok": True})

    async def _audio(self, request: web.Request) -> web.StreamResponse:
        call = request.match_info["call"]
        if not self.verifier.verify(request.query.get("token", ""), call):
            return web.json_response({"error": "refused"}, status=403)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        await ws.send_str(json.dumps({"type": "talk", "on": False}))
        self.ws_open.set()
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                self.frames.append(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                self.texts.append(msg.data)
        self.ws_closed.set()
        return ws

    # ── what the person at the kiosk does ────────────────────────────

    def last_call(self) -> str:
        return list(self.calls)[-1]

    async def _tell_gateway(self, call: str, action: str) -> int:
        c = self.calls[call]
        token = issue_token(self.key, call, self.id)
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"http://{c['address']}:{c['port']}/api/intercom/call/{call}",
                json={"action": action},
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                return r.status

    async def answer(self, call: str | None = None) -> int:
        return await self._tell_gateway(call or self.last_call(), "answer")

    async def decline(self, call: str | None = None) -> int:
        return await self._tell_gateway(call or self.last_call(), "decline")

    async def talk(self, frame: bytes) -> None:
        assert self.ws is not None
        await self.ws.send_bytes(frame)

    async def hang_up(self) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps({"type": "end"}))
        await self.ws.close()


class FakeAudioSocket:
    """Asterisk's end of an AudioSocket external media channel, as
    res_audiosocket.c behaves: connect, send the UUID, then audio."""

    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.messages: list[tuple[int, bytes]] = []  # (kind, payload) from the gateway
        self._task: asyncio.Task[None] | None = None

    async def connect(self, host: str, port: int, media_id: str) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, int(port))
        self.writer.write(struct.pack("!BH", 0x01, 16) + uuid.UUID(media_id).bytes)
        await self.writer.drain()
        self._task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        assert self.reader is not None
        try:
            while True:
                kind, length = struct.unpack("!BH", await self.reader.readexactly(3))
                self.messages.append((kind, await self.reader.readexactly(length) if length else b""))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def audio(self) -> list[bytes]:
        return [p for k, p in self.messages if k == 0x12]

    def send(self, le_pcm: bytes) -> None:
        assert self.writer is not None
        self.writer.write(struct.pack("!BH", 0x12, len(le_pcm)) + le_pcm)

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.writer:
            self.writer.close()


class FakeAri:
    def __init__(self, username: str = "kig", password: str = "secret") -> None:
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.channels: dict[str, dict[str, Any]] = {}
        self.media: dict[str, FakeAudioSocket] = {}
        self.event_ws_connected = asyncio.Event()
        self.server: TestServer | None = None
        self._n = 0

    async def start(self) -> None:
        app = web.Application(middlewares=[self._check_auth])
        r = app.router
        r.add_get("/ari/asterisk/info", self._info)
        r.add_get("/ari/events", self._events)
        r.add_post("/ari/channels/externalMedia", self._external_media)
        r.add_post("/ari/channels/{id}/{op}", self._channel_op)
        r.add_delete("/ari/channels/{id}", self._hangup)
        r.add_get("/ari/channels/{id}/variable", self._variable)
        r.add_post("/ari/bridges", self._bridge)
        r.add_post("/ari/bridges/{id}/addChannel", self._record)
        r.add_delete("/ari/bridges/{id}", self._record)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}"

    async def close(self) -> None:
        for sock in self.media.values():
            sock.close()
        if self.server:
            await self.server.close()

    @web.middleware
    async def _check_auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.headers.get("authorization") != self.auth:
            return web.Response(status=401, text='{"message": "Authentication required"}')
        return await handler(request)

    def _log(self, request: web.Request) -> None:
        self.requests.append((request.method, request.path, dict(request.query)))

    def calls_to(self, method: str, path_part: str) -> list[dict[str, str]]:
        return [q for m, p, q in self.requests if m == method and path_part in p]

    async def _events(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.event_ws_connected.set()

        async def pump() -> None:
            while not ws.closed:
                await ws.send_json(await self.events.get())

        sender = asyncio.ensure_future(pump())
        try:
            async for _ in ws:  # reads the client's close, as Asterisk does
                pass
        finally:
            sender.cancel()
        return ws

    async def _info(self, _: web.Request) -> web.Response:
        return web.json_response({"system": {"version": "22.11.0"}})

    async def _record(self, request: web.Request) -> web.Response:
        self._log(request)
        return web.Response(status=204)

    async def _channel_op(self, request: web.Request) -> web.Response:
        self._log(request)
        if request.match_info["id"] not in self.channels:
            return web.json_response({"message": "Channel not found"}, status=404)
        return web.Response(status=204)

    async def _hangup(self, request: web.Request) -> web.Response:
        self._log(request)
        cid = request.match_info["id"]
        ch = self.channels.pop(cid, None)
        if ch is None:
            return web.json_response({"message": "Channel not found"}, status=404)
        if cid in self.media:
            self.media[cid].close()
        self.hangup_events(ch)
        return web.Response(status=204)

    async def _variable(self, request: web.Request) -> web.Response:
        self._log(request)
        return web.json_response({"value": ""})

    async def _bridge(self, request: web.Request) -> web.Response:
        self._log(request)
        return web.json_response({"id": "bridge-1", "bridge_type": "mixing"})

    async def _external_media(self, request: web.Request) -> web.Response:
        self._log(request)
        q = request.query
        if q.get("encapsulation") != "audiosocket" or q.get("transport") != "tcp":
            return web.json_response({"message": "this fake only does AudioSocket"}, status=400)
        host, port = q["external_host"].rsplit(":", 1)
        self._n += 1
        cid = f"ext-{self._n}"
        ch = {"id": cid, "name": f"AudioSocket/{host}:{port}-{q['data']}"}
        sock = FakeAudioSocket()
        await sock.connect(host, int(port), q["data"])  # Asterisk connects during the request
        self.channels[cid] = ch
        self.media[cid] = sock
        # Like ARI: the channel enters the app with the UUID as its argument.
        await self.events.put({"type": "StasisStart", "channel": ch, "args": [q["data"]]})
        return web.json_response({**ch, "channelvars": {"AUDIOSOCKET_UUID": q["data"]}})

    # ── what Asterisk does on its own ───────────────────────────────

    async def incoming(self, cid: str, target: str, name: str = "Alice", number: str = "101") -> None:
        ch = {"id": cid, "name": f"PJSIP/{number}-0001", "caller": {"name": name, "number": number},
              "dialplan": {"context": "default", "exten": target}}
        self.channels[cid] = ch
        await self.events.put({"type": "StasisStart", "channel": ch, "args": [target]})

    def hangup_events(self, ch: dict[str, Any]) -> None:
        self.events.put_nowait({"type": "StasisEnd", "channel": ch})
        self.events.put_nowait({"type": "ChannelDestroyed", "channel": ch})

    def caller_hangs_up(self, cid: str) -> None:
        ch = self.channels.pop(cid)
        self.hangup_events(ch)

    def hangups(self, cid: str) -> list[str]:
        return [q.get("reason", "") for m, p, q in self.requests if m == "DELETE" and p.endswith(f"/{cid}")]
