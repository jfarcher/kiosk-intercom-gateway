"""Home Assistant side: publish the roster, and learn each kiosk's HA device.

Runs only under the Supervisor (``SUPERVISOR_TOKEN`` set). Kiosks show up in
HA as ESPHome devices with an ``Intercom`` text sensor; the gateway matches
them to discovered kiosks by name to show areas and HA's call state. The
match is best effort: a kiosk whose name fits no device, or several, simply
carries no area.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

from .config import supervisor_token
from .roster import Roster

_LOG = logging.getLogger(__name__)

CORE_API = "http://supervisor/core/api"
CORE_WS = "ws://supervisor/core/websocket"
SUMMARY_ENTITY = "sensor.kiosk_intercom_gateway_kiosks"


async def supervisor_ingress_port(
    token: str | None = None, url: str = "http://supervisor/addons/self/info"
) -> int | None:
    """The host port the Supervisor assigned this app for ingress
    (``ingress_port: 0``), or None outside the Supervisor."""
    token = token or supervisor_token()
    if not token:
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(url, headers={"Authorization": f"Bearer {token}"}) as r:
            r.raise_for_status()
            port = (await r.json())["data"]["ingress_port"]
    return int(port) if port else None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def intercom_devices(
    entities: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    areas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """HA devices that carry a kiosk's ESPHome ``Intercom`` sensor."""
    by_id = {d["id"]: d for d in devices}
    area_names = {a["area_id"]: a.get("name", "") for a in areas}
    out = []
    for e in entities:
        if e.get("platform") != "esphome" or not str(e.get("entity_id", "")).startswith("sensor."):
            continue
        if (e.get("original_name") or "").strip().lower() != "intercom":
            continue
        d = by_id.get(e.get("device_id"))
        if d is None:
            continue
        slug = e["entity_id"].removeprefix("sensor.").removesuffix("_intercom")
        out.append(
            {
                "device_id": d["id"],
                "name": d.get("name_by_user") or d.get("name") or "",
                "names": {_norm(n) for n in (d.get("name"), d.get("name_by_user"), slug) if n},
                "area": area_names.get(d.get("area_id") or e.get("area_id"), ""),
                "intercom_entity": e["entity_id"],
            }
        )
    return out


def match(kiosk_names: list[str], devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    keys = {_norm(n) for n in kiosk_names if n}
    keys.discard("")
    hits = [d for d in devices if d["names"] & keys]
    return hits[0] if len(hits) == 1 else None


class HomeAssistant:
    def __init__(self, roster: Roster, token: str | None = None) -> None:
        self.roster = roster
        self.token = token or supervisor_token()
        self._session: aiohttp.ClientSession | None = None
        self._devices: list[dict[str, Any]] = []
        self._dirty = asyncio.Event()

    @property
    def available(self) -> bool:
        return bool(self.token)

    def changed(self) -> None:
        self._dirty.set()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _ws_call(self, *types: str) -> list[Any]:
        assert self._session is not None
        results: list[Any] = []
        async with self._session.ws_connect(CORE_WS, heartbeat=30) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": self.token})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"HA websocket auth failed: {msg}")
            for i, t in enumerate(types, start=1):
                await ws.send_json({"id": i, "type": t})
                while True:
                    msg = await ws.receive_json()
                    if msg.get("id") == i:
                        break
                if not msg.get("success"):
                    raise RuntimeError(f"{t} failed: {msg.get('error')}")
                results.append(msg["result"])
        return results

    async def refresh_devices(self) -> None:
        entities, devices, areas = await self._ws_call(
            "config/entity_registry/list",
            "config/device_registry/list",
            "config/area_registry/list",
        )
        self._devices = intercom_devices(entities, devices, areas)
        _LOG.debug("HA knows %d kiosk devices", len(self._devices))

    async def _state(self, entity_id: str) -> str:
        assert self._session is not None
        async with self._session.get(f"{CORE_API}/states/{entity_id}", headers=self._headers) as r:
            if r.status != 200:
                return ""
            return str((await r.json()).get("state", ""))

    async def enrich(self) -> None:
        for k in self.roster.kiosks.values():
            hit = match([k.name, k.host.removesuffix(".local"), k.id], self._devices)
            if hit is None:
                k.ha_device = k.ha_area = k.ha_state = ""
                continue
            k.ha_device, k.ha_area = hit["name"], hit["area"]
            k.ha_state = await self._state(hit["intercom_entity"])

    async def publish(self) -> None:
        assert self._session is not None
        snap = self.roster.snapshot()
        ready = sum(1 for k in snap if k["status"] == "ready")
        body = {
            "state": ready,
            "attributes": {
                "friendly_name": "Kiosk intercom: ready kiosks",
                "icon": "mdi:phone-voip",
                "unit_of_measurement": "kiosks",
                "state_class": "measurement",
                "kiosks": [
                    {key: k[key] for key in ("name", "id", "address", "status", "area", "dnd", "version")}
                    for k in snap
                ],
            },
        }
        async with self._session.post(
            f"{CORE_API}/states/{SUMMARY_ENTITY}",
            headers=self._headers,
            data=json.dumps(body),
        ) as r:
            if r.status >= 300:
                _LOG.warning("publishing %s failed: HTTP %s", SUMMARY_ENTITY, r.status)

    async def run(self, stop: asyncio.Event, interval: float = 60.0) -> None:
        """Re-reads HA's devices every ``interval``; publishes on every roster
        change (debounced) and with each refresh, which also restores the
        sensor after HA restarts."""
        if not self.available:
            _LOG.info("no Supervisor token: Home Assistant integration off")
            return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        loop = asyncio.get_running_loop()
        next_refresh = 0.0
        try:
            while not stop.is_set():
                try:
                    if loop.time() >= next_refresh:
                        next_refresh = loop.time() + interval
                        await self.refresh_devices()
                    await self.enrich()
                    await self.publish()
                except Exception as err:  # HA restarting, etc.
                    _LOG.warning("Home Assistant update failed: %s", err)
                self._dirty.clear()
                wait = max(1.0, next_refresh - loop.time())
                waiters = [asyncio.ensure_future(self._dirty.wait()), asyncio.ensure_future(stop.wait())]
                done, pending = await asyncio.wait(waiters, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                await asyncio.sleep(1)  # debounce bursts of roster changes
        finally:
            await self._session.close()
