"""A minimal Asterisk REST Interface client: REST calls and the event stream."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

_LOG = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class AriError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"ARI {status}: {message}")
        self.status = status


class Ari:
    def __init__(self, url: str, username: str, password: str, app: str) -> None:
        self.base = url.rstrip("/") + "/ari"
        self.app = app
        self._auth = aiohttp.BasicAuth(username, password)
        self._session: aiohttp.ClientSession | None = None
        self.connected = False
        self.error = ""
        self.asterisk_version = ""

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                auth=self._auth, timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def request(self, method: str, path: str, **params: Any) -> Any:
        query = {k: _q(v) for k, v in params.items() if v is not None}
        async with self.session.request(method, self.base + path, params=query) as r:
            text = await r.text()
            if r.status >= 300:
                raise AriError(r.status, text.strip()[:200])
            if not text:
                return None
            try:
                return await r.json(content_type=None)
            except ValueError:
                return text

    # ── the handful of operations the bridge uses ────────────────────

    async def ring(self, channel: str) -> None:
        await self.request("POST", f"/channels/{channel}/ring")

    async def answer(self, channel: str) -> None:
        await self.request("POST", f"/channels/{channel}/answer")

    async def hangup(self, channel: str, reason: str = "normal") -> None:
        try:
            await self.request("DELETE", f"/channels/{channel}", reason=reason)
        except AriError as err:
            if err.status != 404:  # already gone
                raise

    async def external_media(self, host: str, port: int, media_id: str) -> dict[str, Any]:
        """An AudioSocket channel carrying slin16 to and from ``host:port``,
        identified by ``media_id`` (a UUID). See media.py for why not RTP."""
        return await self.request(
            "POST",
            "/channels/externalMedia",
            app=self.app,
            external_host=f"{host}:{port}",
            encapsulation="audiosocket",
            transport="tcp",
            format="slin16",
            data=media_id,
        )

    async def bridge(self, *channels: str) -> str:
        b = await self.request("POST", "/bridges", type="mixing")
        await self.request("POST", f"/bridges/{b['id']}/addChannel", channel=",".join(channels))
        return str(b["id"])

    async def destroy_bridge(self, bridge: str) -> None:
        try:
            await self.request("DELETE", f"/bridges/{bridge}")
        except AriError as err:
            if err.status != 404:
                raise

    # ── events ───────────────────────────────────────────────────────

    async def run(self, handler: EventHandler, stop: asyncio.Event) -> None:
        """Follows the event stream, reconnecting with backoff, until ``stop``."""
        url = self.base.replace("http", "ws", 1) + "/events"
        delay = 1.0
        while not stop.is_set():
            try:
                info = await self.request("GET", "/asterisk/info")
                self.asterisk_version = str(((info or {}).get("system") or {}).get("version") or "")
                async with self.session.ws_connect(
                    url,
                    params={"app": self.app, "subscribeAll": "false"},
                    heartbeat=30,
                    timeout=aiohttp.ClientWSTimeout(ws_close=2),
                ) as ws:
                    self.connected, self.error, delay = True, "", 1.0
                    _LOG.info("connected to Asterisk %s ARI as app %s", self.asterisk_version, self.app)
                    stopper = asyncio.ensure_future(stop.wait())
                    try:
                        while True:
                            receive = asyncio.ensure_future(ws.receive())
                            done, _ = await asyncio.wait(
                                {receive, stopper}, return_when=asyncio.FIRST_COMPLETED
                            )
                            if stopper in done:
                                receive.cancel()
                                return
                            msg = receive.result()
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                break
                            try:
                                event = msg.json()
                            except ValueError:
                                continue
                            # In order: a hangup must not overtake its call's start.
                            # The handler hands slow work to tasks of its own.
                            await _safe(handler, event)
                    finally:
                        stopper.cancel()
                self.error = "event stream closed"
            except AriError as err:
                self.error = "wrong ARI username or password" if err.status == 401 else str(err)
            except (aiohttp.ClientError, OSError, TimeoutError) as err:
                self.error = f"cannot reach Asterisk ARI at {self.base} ({err.__class__.__name__})"
            self.connected = False
            _LOG.warning("ARI: %s; retrying in %.0fs", self.error, delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 30.0)


def _q(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _safe(handler: EventHandler, event: dict[str, Any]) -> None:
    try:
        await handler(event)
    except Exception:
        _LOG.exception("handling ARI event %s failed", event.get("type"))
