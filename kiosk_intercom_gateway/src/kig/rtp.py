"""Audio between Asterisk's external media channel and a kiosk.

Asterisk side: RTP over UDP, payload ``slin16`` (L16, 16 kHz mono,
big-endian, network order), 20 ms per packet. Kiosk side: PCM16
little-endian 16 kHz mono in 80 ms frames. Same rate, so the work is a byte
swap and reframing, plus pacing toward Asterisk: kiosk frames arrive in
80 ms bursts and go out as one 20 ms packet per tick.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import struct
from array import array
from collections import deque
from collections.abc import Callable

from .protocol import FRAME_BYTES, SAMPLE_RATE

_LOG = logging.getLogger(__name__)

PTIME_MS = 20
CHUNK_BYTES = SAMPLE_RATE * PTIME_MS // 1000 * 2  # 640
SAMPLES_PER_CHUNK = CHUNK_BYTES // 2
SLIN16_PT = 118  # Asterisk's payload type for slin16
MAX_QUEUE = 25  # 500 ms toward Asterisk before dropping the oldest


def swap16(data: bytes) -> bytes:
    """Little-endian <-> big-endian 16-bit samples."""
    a = array("h")
    a.frombytes(data[: len(data) // 2 * 2])
    a.byteswap()
    return a.tobytes()


def rms(le_pcm: bytes) -> float:
    a = array("h")
    a.frombytes(le_pcm[: len(le_pcm) // 2 * 2])
    if not a:
        return 0.0
    return math.sqrt(sum(s * s for s in a) / len(a))


def parse_rtp(packet: bytes) -> tuple[int, bytes] | None:
    """(payload type, payload) of an RTP packet, None for anything else."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    cc = packet[0] & 0x0F
    offset = 12 + 4 * cc
    if packet[0] & 0x10:  # header extension
        if len(packet) < offset + 4:
            return None
        (length,) = struct.unpack_from("!H", packet, offset + 2)
        offset += 4 + 4 * length
    payload = packet[offset:]
    if packet[0] & 0x20 and payload:  # padding
        payload = payload[: -payload[-1]]
    return packet[1] & 0x7F, payload


class RtpBridge(asyncio.DatagramProtocol):
    """One call's RTP leg. Calls ``on_frame`` with little-endian 80 ms frames
    from Asterisk; :meth:`send_frame` takes little-endian kiosk audio."""

    def __init__(self, on_frame: Callable[[bytes], None]) -> None:
        self.on_frame = on_frame
        self.transport: asyncio.DatagramTransport | None = None
        self.remote: tuple[str, int] | None = None
        self.payload_type = SLIN16_PT
        self.received = self.sent = 0
        self._inbound = bytearray()
        self._queue: deque[bytes] = deque()
        self._outbound = bytearray()
        self._seq = secrets.randbelow(1 << 16)
        self._ts = secrets.randbelow(1 << 32)
        self._ssrc = secrets.randbelow(1 << 32)
        self._marker = True
        self._pacer: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    @classmethod
    async def open(cls, on_frame: Callable[[bytes], None], host: str = "127.0.0.1") -> RtpBridge:
        loop = asyncio.get_running_loop()
        _, proto = await loop.create_datagram_endpoint(lambda: cls(on_frame), local_addr=(host, 0))
        return proto

    @property
    def local(self) -> tuple[str, int]:
        assert self.transport is not None
        return self.transport.get_extra_info("sockname")[:2]

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self._pacer = asyncio.ensure_future(self._pace())

    def set_remote(self, host: str, port: int) -> None:
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        self.remote = (host, port)

    def close(self) -> None:
        if self._pacer:
            self._pacer.cancel()
        if self.transport:
            self.transport.close()

    # ── Asterisk → kiosk ─────────────────────────────────────────────

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = parse_rtp(data)
        if parsed is None:
            return
        pt, payload = parsed
        if self.remote is None:
            self.remote = addr[:2]
        self.payload_type = pt
        self.received += 1
        self._inbound += swap16(payload)
        while len(self._inbound) >= FRAME_BYTES:
            frame = bytes(self._inbound[:FRAME_BYTES])
            del self._inbound[:FRAME_BYTES]
            self.on_frame(frame)

    # ── kiosk → Asterisk ─────────────────────────────────────────────

    def send_frame(self, le_pcm: bytes) -> None:
        self._outbound += swap16(le_pcm)
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
        while True:
            due += tick
            await asyncio.sleep(max(0.0, due - loop.time()))
            if loop.time() - due > 0.2:  # fell far behind (suspended): resync
                due = loop.time()
            # Hold two packets back at first, a small jitter buffer.
            if not started and len(self._queue) < 2:
                self._ts = (self._ts + SAMPLES_PER_CHUNK) & 0xFFFFFFFF
                continue
            started = True
            if self._queue and self.remote and self.transport:
                chunk = self._queue.popleft()
                header = struct.pack(
                    "!BBHII",
                    0x80,
                    (0x80 if self._marker else 0) | self.payload_type,
                    self._seq,
                    self._ts,
                    self._ssrc,
                )
                self.transport.sendto(header + chunk, self.remote)
                self._marker = False
                self._seq = (self._seq + 1) & 0xFFFF
                self.sent += 1
            elif not self._queue:
                started = False
                self._marker = True
            self._ts = (self._ts + SAMPLES_PER_CHUNK) & 0xFFFFFFFF
