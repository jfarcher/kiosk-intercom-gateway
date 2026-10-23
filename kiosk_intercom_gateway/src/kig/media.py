"""Audio between Asterisk and a kiosk over AudioSocket.

Why AudioSocket and not RTP: with ARI external media over RTP, Asterisk 22
sends slin16 under payload type 118 but drops it on receive. Payload types
above 35 need a receive mapping that chan_rtp never sets up
(``ast_rtp_codecs_get_payload``), so the kiosk would never be heard.
AudioSocket carries slin16 both ways (message kind 0x12, Asterisk 20.13+/
21.8+/22.3+), and the Home Assistant Asterisk app builds it.

The protocol (res_audiosocket.c): Asterisk connects over TCP and every
message is ``kind (1 byte) | length (2 bytes, big-endian) | payload``. It
starts with kind 0x01 (the 16-byte UUID given as the external media
``data``), then kind 0x12 audio: signed 16-bit samples in host byte order,
little-endian on every machine Home Assistant runs on, the kiosks' own
format. 0x00 hangs up, 0x03 is DTMF, 0xff an error.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import uuid
from array import array
from collections import deque
from collections.abc import Callable

from .protocol import FRAME_BYTES, SAMPLE_RATE

_LOG = logging.getLogger(__name__)

KIND_HANGUP, KIND_UUID, KIND_DTMF, KIND_SLIN16, KIND_ERROR = 0x00, 0x01, 0x03, 0x12, 0xFF
PTIME_MS = 20
CHUNK_BYTES = SAMPLE_RATE * PTIME_MS // 1000 * 2  # 640
MAX_QUEUE = 25  # 500 ms toward Asterisk before dropping the oldest


def rms(le_pcm: bytes) -> float:
    a = array("h")
    a.frombytes(le_pcm[: len(le_pcm) // 2 * 2])
    if not a:
        return 0.0
    return math.sqrt(sum(s * s for s in a) / len(a))


def message(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack("!BH", kind, len(payload)) + payload


class MediaLeg:
    """One call's AudioSocket connection. ``on_frame`` gets 80 ms kiosk frames
    of Asterisk's audio; :meth:`send_frame` takes kiosk audio, paced out as
    one 20 ms message per tick."""

    def __init__(self, call_id: str, on_frame: Callable[[bytes], None]) -> None:
        self.call_id = call_id
        self.uuid = str(uuid.uuid4())
        self.on_frame = on_frame
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.received = self.sent = 0
        self._writer: asyncio.StreamWriter | None = None
        self._inbound = bytearray()
        self._outbound = bytearray()
        self._queue: deque[bytes] = deque()
        self._tasks: list[asyncio.Task[None]] = []

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._tasks = [asyncio.ensure_future(self._read(reader)), asyncio.ensure_future(self._pace())]
        self.connected.set()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                kind, length = struct.unpack("!BH", await reader.readexactly(3))
                payload = await reader.readexactly(length) if length else b""
                if kind == KIND_SLIN16:
                    self.received += 1
                    self._inbound += payload
                    while len(self._inbound) >= FRAME_BYTES:
                        self.on_frame(bytes(self._inbound[:FRAME_BYTES]))
                        del self._inbound[:FRAME_BYTES]
                elif kind == KIND_HANGUP:
                    break
                elif kind == KIND_ERROR:
                    _LOG.debug("AudioSocket error from Asterisk: %s", payload.hex())
                # DTMF and anything else: not for a kiosk.
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed.set()

    def send_frame(self, le_pcm: bytes) -> None:
        self._outbound += le_pcm
        while len(self._outbound) >= CHUNK_BYTES:
            self._queue.append(bytes(self._outbound[:CHUNK_BYTES]))
            del self._outbound[:CHUNK_BYTES]
        while len(self._queue) > MAX_QUEUE:
            self._queue.popleft()

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        tick = PTIME_MS / 1000
        due = loop.time()
        started = False
        while self._writer is not None and not self.closed.is_set():
            due += tick
            await asyncio.sleep(max(0.0, due - loop.time()))
            if loop.time() - due > 0.2:  # fell far behind (suspended): resync
                due = loop.time()
            if not started and len(self._queue) < 2:  # a 40 ms jitter buffer
                continue
            started = bool(self._queue)
            if self._queue:
                try:
                    self._writer.write(message(KIND_SLIN16, self._queue.popleft()))
                    self.sent += 1
                except (ConnectionError, RuntimeError):
                    return

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._writer is not None:
            try:
                self._writer.write(message(KIND_HANGUP))
                await asyncio.wait_for(self._writer.drain(), 1)
            except (ConnectionError, RuntimeError, TimeoutError):
                pass
            self._writer.close()
        self.closed.set()


class AudioSocketServer:
    """Where Asterisk connects for every call's media. Connections are matched
    to their call by the UUID Asterisk sends first."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host, self.port = host, port
        self._legs: dict[str, MediaLeg] = {}
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._connection, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def expect(self, leg: MediaLeg) -> None:
        self._legs[leg.uuid] = leg

    def forget(self, leg: MediaLeg) -> None:
        self._legs.pop(leg.uuid, None)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            kind, length = struct.unpack("!BH", await asyncio.wait_for(reader.readexactly(3), 5))
            payload = await asyncio.wait_for(reader.readexactly(length), 5)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            writer.close()
            return
        leg = None
        if kind == KIND_UUID and length == 16:
            leg = self._legs.get(str(uuid.UUID(bytes=payload)))
        if leg is None:
            _LOG.debug("AudioSocket connection for no known call (kind %#x)", kind)
            writer.close()
            return
        leg.attach(reader, writer)
