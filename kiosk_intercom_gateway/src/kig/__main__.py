"""Entry point: ``python3 -m kig``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from aiohttp import web

from . import __version__, config
from .ari import Ari
from .bridge import CallManager
from .discovery import Discovery
from .phones import PhoneDirectory
from .ha import HomeAssistant, save_options, supervisor_ingress_port
from .probe import HttpProber
from .roster import Roster
from .state import State
from .web import make_app

_LOG = logging.getLogger("kig")
DEFAULT_WEB_PORT = 8099  # outside the Supervisor


async def web_port(cfg: config.Config) -> int:
    if cfg.web_port:
        return cfg.web_port
    if not config.under_supervisor():
        return DEFAULT_WEB_PORT
    try:
        port = await supervisor_ingress_port()
    except Exception as err:
        _LOG.error("could not read the ingress port from the Supervisor: %s", err)
        port = None
    if not port:
        _LOG.error("no ingress port from the Supervisor (token %s); not guessing one",
                   "present" if config.supervisor_token() else "missing")
        raise SystemExit(1)
    return port


async def main() -> None:
    cfg = config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOG.info("Kiosk Intercom Gateway %s starting", __version__)
    if not cfg.intercom_key:
        _LOG.warning("no intercom key set: kiosks will be listed but none reads Ready")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with contextlib.AsyncExitStack() as stack:
        prober = HttpProber()
        stack.push_async_callback(prober.close)
        roster = Roster(prober, key=cfg.intercom_key, probe_interval=cfg.probe_interval)
        for entry in cfg.extra_kiosks:
            try:
                roster.add_manual(entry)
            except ValueError:
                _LOG.warning("ignoring extra kiosk %r: expected host or host:port", entry)

        ha = HomeAssistant(roster)
        roster.on_change(ha.changed)

        ari = None
        if cfg.ari_username:
            ari = Ari(cfg.ari_url, cfg.ari_username, cfg.ari_password, cfg.ari_app)
            stack.push_async_callback(ari.close)
        else:
            _LOG.info("no ARI username set: SIP calls are off")
        state = State()
        calls = CallManager(
            roster,
            state,
            ari,
            extension_start=cfg.extension_start,
            ring_timeout=cfg.ring_timeout,
            listen_port=cfg.intercom_port,
            media_host=cfg.media_host,
        )
        try:
            await calls.start()
        except OSError as err:
            _LOG.error("cannot open the intercom listener (port %d) or call audio (%s): %s",
                       cfg.intercom_port, cfg.media_host, err.strerror or err)
            raise SystemExit(1) from err
        stack.push_async_callback(calls.stop)

        discovery = None
        if cfg.mdns or ari is not None:
            # Browsing finds kiosks; announcing offers phones to them.
            discovery = Discovery(roster, browse=cfg.mdns, ignore_prefix=state.gateway_id)
            await discovery.start()
            stack.push_async_callback(discovery.stop)

        phones = None
        if ari is not None and cfg.offer_phones:
            phones = PhoneDirectory(
                ari, roster, state, discovery,
                {"identity": calls.phone_identity, "call": calls.phone_call,
                 "signal": calls.phone_signal, "audio": calls.phone_audio},
                configured=cfg.phones, advertise_address=cfg.advertise_address,
            )
            calls.phones = phones
            stack.push_async_callback(phones.close)

        def info() -> dict:
            return {
                "version": __version__,
                "key_fingerprint": roster.key_fingerprint,
                "discovery": cfg.mdns,
                "home_assistant": ha.available,
                "phones_auto": phones.auto if phones else True,
                "phones_enabled": phones is not None,
                "asterisk": {**calls.status(), "ari_url": cfg.ari_url, "username": cfg.ari_username,
                             "extension_start": cfg.extension_start, "media_host": cfg.media_host},
            }

        async def set_key(key: str) -> dict:
            """Applies a key at once and keeps it in the app's configuration."""
            roster.set_key(key)
            _LOG.info("intercom key changed (fingerprint %s)", roster.key_fingerprint)
            if not config.under_supervisor():
                return {"ok": True, "saved": False, "fingerprint": roster.key_fingerprint,
                        "note": "Applied until restart (not running under Home Assistant)."}
            try:
                await save_options({"intercom_key": key}, config.raw_options())
            except Exception as err:
                _LOG.warning("could not save the key to the configuration: %s", err)
                return {"ok": True, "saved": False, "fingerprint": roster.key_fingerprint,
                        "note": f"Applied, but not saved ({err}). Set it under Configuration too."}
            return {"ok": True, "saved": True, "fingerprint": roster.key_fingerprint}

        async def set_phones(entries: list[str] | None) -> dict:
            """Which phones kiosks can call, from the page: applied at once and
            kept in the app's configuration (an empty list is automatic)."""
            if phones is None:
                return {"ok": False, "error": "Kiosk → phone calls are off (no ARI user, or turned off)."}
            chosen = await phones.choose(entries)
            _LOG.info("phones offered to kiosks: %s", ", ".join(chosen) if chosen else "automatic")
            if not config.under_supervisor():
                return {"ok": True, "saved": False, "note": "Applied until restart."}
            try:
                await save_options({"phones": chosen}, config.raw_options())
            except Exception as err:
                _LOG.warning("could not save the phone list to the configuration: %s", err)
                return {"ok": True, "saved": False, "note": f"Applied, but not saved ({err})."}
            return {"ok": True, "saved": True}

        port = await web_port(cfg)
        runner = web.AppRunner(
            make_app(roster, info, cfg.ingress_only, set_key=set_key, set_phones=set_phones),
            access_log=None,
        )
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        try:
            await web.TCPSite(runner, "0.0.0.0", port).start()
        except OSError as err:
            _LOG.error("status page cannot listen on port %d: %s (another service on the host holds it)",
                       port, err.strerror or err)
            raise SystemExit(1) from err
        _LOG.info("status page on port %d", port)

        jobs = [roster.run(stop), ha.run(stop)]
        if ari is not None:
            jobs.append(ari.run(calls.on_event, stop))
        if phones is not None:
            jobs.append(phones.run(stop))
        await asyncio.gather(*jobs)
    _LOG.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
