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
# The entity registry of a large install is one websocket message of several
# MB (4.3 MB reported), past aiohttp's 4 MiB default.
MAX_WS_MESSAGE = 64 * 1024 * 1024
REGISTRY_INTERVAL = 600.0  # registries change rarely and are big: re-read every 10 min
PUBLISH_INTERVAL = 60.0  # the sensor, and kiosks' HA call state
CALLS_ENTITY = "sensor.kiosk_intercom_gateway_calls"
CALL_EVENT = "kiosk_intercom_gateway_call"
PHONE_ENTITY = "binary_sensor.kiosk_intercom_phone_{slug}"
SENSOR_FIELDS = ("name", "id", "address", "extension", "status", "area", "dnd", "version")


SELF_INFO: dict[str, Any] = {}  # the Supervisor's /addons/self/info, once read


async def supervisor_self_info(
    token: str | None = None, url: str = "http://supervisor/addons/self/info"
) -> dict[str, Any]:
    """This app as the Supervisor sees it (slug, ingress port, …); {} outside it."""
    token = token or supervisor_token()
    if not token:
        return {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(url, headers={"Authorization": f"Bearer {token}"}) as r:
            r.raise_for_status()
            data = (await r.json())["data"]
    SELF_INFO.update(data if isinstance(data, dict) else {})
    return SELF_INFO


async def supervisor_ingress_port(
    token: str | None = None, url: str = "http://supervisor/addons/self/info"
) -> int | None:
    """The host port the Supervisor assigned this app for ingress
    (``ingress_port: 0``), or None outside the Supervisor."""
    port = (await supervisor_self_info(token, url)).get("ingress_port")
    return int(port) if port else None


async def save_options(
    changes: dict[str, Any],
    current: dict[str, Any],
    token: str | None = None,
    url: str = "http://supervisor/addons/self/options",
) -> None:
    """Writes options back through the Supervisor, which validates them and
    rewrites /data/options.json, so the Configuration tab shows them too. The
    Supervisor replaces the options whole, hence ``current``."""
    token = token or supervisor_token()
    if not token:
        raise RuntimeError("not running under the Supervisor")
    body = {"options": {**current, **changes}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post(url, json=body, headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status >= 300:
                try:
                    msg = (await r.json()).get("message")
                except (ValueError, aiohttp.ContentTypeError):
                    msg = None
                raise RuntimeError(msg or f"Supervisor answered HTTP {r.status}")


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
    def __init__(
        self, roster: Roster, token: str | None = None, core_api: str = CORE_API, core_ws: str = CORE_WS
    ) -> None:
        self.roster = roster
        self.core_api, self.core_ws = core_api, core_ws
        self.token = token or supervisor_token()
        self._session: aiohttp.ClientSession | None = None
        self._devices: list[dict[str, Any]] = []
        self._dirty = asyncio.Event()
        self.calls: Any = None  # CallManager, for the calls and phone sensors
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._phone_entities: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(self.token)

    def call_event(self, data: dict[str, Any]) -> None:
        """A call rang, was answered or ended: an event for automations, and
        the calls sensor follows."""
        if not self.available:
            return
        try:
            self._events.put_nowait(data)
        except asyncio.QueueFull:
            _LOG.warning("Home Assistant is not taking events; dropping %s", data.get("state"))
        self.changed()

    async def _send_events(self, stop: asyncio.Event) -> None:
        assert self._session is not None
        while not stop.is_set():
            data = await self._events.get()
            try:
                async with self._session.post(
                    f"{self.core_api}/events/{CALL_EVENT}", headers=self._headers, data=json.dumps(data)
                ) as r:
                    if r.status >= 300:
                        _LOG.warning("firing %s failed: HTTP %s", CALL_EVENT, r.status)
            except (aiohttp.ClientError, OSError, TimeoutError) as err:
                _LOG.warning("firing %s failed: %s", CALL_EVENT, err)

    def changed(self) -> None:
        self._dirty.set()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _ws_call(self, *types: str) -> list[Any]:
        assert self._session is not None
        results: list[Any] = []
        async with self._session.ws_connect(
            self.core_ws,
            heartbeat=30,
            max_msg_size=MAX_WS_MESSAGE,
            timeout=aiohttp.ClientWSTimeout(ws_close=5),
        ) as ws:
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
        async with self._session.get(f"{self.core_api}/states/{entity_id}", headers=self._headers) as r:
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
                    {key: k[key] for key in SENSOR_FIELDS}
                    for k in snap
                ],
            },
        }
        async with self._session.post(
            f"{self.core_api}/states/{SUMMARY_ENTITY}",
            headers=self._headers,
            data=json.dumps(body),
        ) as r:
            if r.status >= 300:
                _LOG.warning("publishing %s failed: HTTP %s", SUMMARY_ENTITY, r.status)
        if self.calls is not None:
            await self._publish_calls()

    async def _set_state(self, entity: str, body: dict[str, Any]) -> None:
        assert self._session is not None
        async with self._session.post(
            f"{self.core_api}/states/{entity}", headers=self._headers, data=json.dumps(body)
        ) as r:
            if r.status >= 300:
                _LOG.warning("publishing %s failed: HTTP %s", entity, r.status)

    async def _publish_calls(self) -> None:
        status = self.calls.status()
        live = status["calls"]
        await self._set_state(CALLS_ENTITY, {
            "state": len(live),
            "attributes": {
                "friendly_name": "Kiosk intercom: calls",
                "icon": "mdi:phone-in-talk" if live else "mdi:phone",
                "unit_of_measurement": "calls",
                "calls": [
                    {"direction": "phone_to_kiosk" if c["direction"] == "in" else "kiosk_to_phone",
                     "state": c["state"], "kiosk": c["kiosk"], "kiosk_extension": c["extension"],
                     "phone": c["caller"], "since": c["since"]}
                    for c in live
                ],
            },
        })
        # One connectivity sensor per phone kiosks may call; gone ones removed.
        seen = set()
        for p in status.get("phones", []):
            if not p["selected"]:
                continue
            entity = PHONE_ENTITY.format(slug=re.sub(r"[^a-z0-9]+", "_", p["extension"].lower()).strip("_"))
            seen.add(entity)
            await self._set_state(entity, {
                "state": "on" if p["state"] == "online" else "off",
                "attributes": {
                    "friendly_name": f"{p['name']} registered",
                    "device_class": "connectivity",
                    "extension": p["extension"],
                    "on_kiosks": p["offered"],
                    "busy": p["busy"],
                    "found_by": p.get("seen_by", []),
                },
            })
        for entity in self._phone_entities - seen:
            assert self._session is not None
            async with self._session.delete(f"{self.core_api}/states/{entity}", headers=self._headers):
                pass
        self._phone_entities = seen

    async def run(
        self,
        stop: asyncio.Event,
        registry_interval: float = REGISTRY_INTERVAL,
        publish_interval: float = PUBLISH_INTERVAL,
    ) -> None:
        """Re-reads HA's registries every ``registry_interval`` and publishes
        the sensor every ``publish_interval`` and on roster changes (debounced),
        which also restores it after HA restarts. Each step fails on its own:
        a registry that cannot be read must not stop the sensor."""
        if not self.available:
            _LOG.info("no Supervisor token: Home Assistant integration off")
            return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        sender = asyncio.ensure_future(self._send_events(stop))
        loop = asyncio.get_running_loop()
        next_refresh = 0.0
        try:
            while not stop.is_set():
                if loop.time() >= next_refresh:
                    next_refresh = loop.time() + registry_interval
                    try:
                        await self.refresh_devices()
                    except Exception as err:  # HA restarting, etc.
                        _LOG.warning("reading Home Assistant's device registry failed: %s", err)
                        next_refresh = loop.time() + publish_interval  # try again sooner
                steps = ((self.enrich, "reading kiosks' states"), (self.publish, "publishing the sensor"))
                for step, what in steps:
                    try:
                        await step()
                    except Exception as err:
                        _LOG.warning("Home Assistant: %s failed: %s", what, err)
                self._dirty.clear()
                waiters = [asyncio.ensure_future(self._dirty.wait()), asyncio.ensure_future(stop.wait())]
                await asyncio.wait(waiters, timeout=publish_interval, return_when=asyncio.FIRST_COMPLETED)
                for p in waiters:
                    p.cancel()
                await asyncio.sleep(1)  # debounce bursts of roster changes
        finally:
            sender.cancel()
            await self._session.close()
