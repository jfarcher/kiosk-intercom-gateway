"""App options: /data/options.json under the Supervisor, environment for local runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

OPTIONS = Path(os.environ.get("KIG_OPTIONS", "/data/options.json"))
S6_ENV = Path("/run/s6/container_environment")


def supervisor_token() -> str:
    """The Supervisor's API token. s6-overlay resets the environment of the
    main process, so fall back to the copy s6 keeps of the container's."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        try:
            token = (S6_ENV / "SUPERVISOR_TOKEN").read_text()
        except OSError:
            token = ""
    return token.strip()


def under_supervisor() -> bool:
    return bool(supervisor_token()) or Path("/data/options.json").is_file()


@dataclass
class Config:
    intercom_key: str = ""
    extra_kiosks: list[str] = field(default_factory=list)
    probe_interval: int = 30
    mdns: bool = True
    log_level: str = "info"
    web_port: int = 0  # 0: ask the Supervisor, else 8099
    ingress_only: bool = True


def raw_options(path: Path = OPTIONS) -> dict:
    try:
        raw = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        raw = {}
    return raw if isinstance(raw, dict) else {}


def load(path: Path = OPTIONS) -> Config:
    raw = raw_options(path)
    cfg = Config(
        intercom_key=str(raw.get("intercom_key") or os.environ.get("KIG_KEY", "")).strip(),
        extra_kiosks=[str(x) for x in raw.get("extra_kiosks") or [] if str(x).strip()],
        probe_interval=int(raw.get("probe_interval", 30)),
        mdns=bool(raw.get("mdns", True)),
        log_level=str(raw.get("log_level", os.environ.get("KIG_LOG", "info"))),
        web_port=int(os.environ.get("KIG_PORT", 0)),
        # Outside the Supervisor (local runs) the page is open to localhost only anyway.
        ingress_only=os.environ.get("KIG_OPEN_WEB") != "1",
    )
    cfg.probe_interval = min(600, max(10, cfg.probe_interval))
    return cfg
