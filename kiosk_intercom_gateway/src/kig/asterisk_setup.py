"""Sets the Asterisk app up for kiosk calls: the page's copy-and-paste steps, done.

Asterisk apps with a web UI (jfarcher/asterisk-hass-addons 6.6+) manage ARI and
the kiosk numbers themselves: their Apps tab writes
``asterisk/integrations/kiosk-intercom-gateway.json`` with the login to use.
When that file is there, nothing is written; the gateway just takes the login
from it. Editing ``extensions.conf`` there would replace that app's dial plan.

Otherwise (TECH7Fox's Asterisk app):

Writes the gateway's ARI user into ``custom/ari.conf`` and the kiosk lines into
``custom/extensions.conf`` of the Asterisk app (TECH7Fox), in its folder under
/addon_configs (the ``all_addon_configs`` map). Only what the gateway needs is
touched: other sections and lines stay as they are, and a file that already
exists is copied to ``<name>.before-kiosk-gateway`` first. Running it again
changes nothing. Asterisk reads the files when it restarts.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_LOG = logging.getLogger(__name__)

ROOT = Path("/addon_configs")
ASTERISK_SLUG = "b35499aa_asterisk"  # TECH7Fox's repository
HANDOFF = Path("integrations") / "kiosk-intercom-gateway.json"
BACKUP_SUFFIX = ".before-kiosk-gateway"
_SECTION = re.compile(r"^\s*\[([^\]]+)\]")


def find(root: Path = ROOT) -> Path | None:
    """The Asterisk app's config folder (holding ``custom/``), if this app can see it."""
    candidates = [root / ASTERISK_SLUG / "asterisk"]
    try:
        candidates += sorted(p / "asterisk" for p in root.iterdir() if p.name.endswith("_asterisk"))
    except OSError:
        return None
    # An Asterisk app that offers this gateway a login wins: it's the one set up for it.
    for c in candidates:
        if (c / HANDOFF).is_file():
            return c
    for c in candidates:
        if c.is_dir():
            return c
    return None


class ManagedError(Exception):
    """The Asterisk app manages this itself and said no; the message says what to do."""


@dataclass
class Handoff:
    """The login an Asterisk app with a web UI offers this gateway."""

    username: str
    password: str
    app: str
    ari_url: str
    ari_enabled: bool
    first: int | None  # first number routed to the gateway, such as 800

    @property
    def extension_start(self) -> int | None:
        """Kiosk numbering inside the routed range: 801 for 800-899 (800 announces)."""
        return None if self.first is None else self.first + 1


_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def handoff(folder: Path | None) -> Handoff | None:
    """The Asterisk app's offer, if it made one (and it reads as one)."""
    if folder is None:
        return None
    try:
        raw = json.loads((folder / HANDOFF).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    username, password, app = (str(raw.get(k) or "") for k in ("username", "password", "app"))
    url = str(raw.get("ari_url") or "")
    if not (_NAME.match(username) and _NAME.match(app) and password
            and url.startswith(("http://", "https://"))):
        return None
    numbers = raw.get("numbers") if isinstance(raw.get("numbers"), dict) else {}
    first = str(numbers.get("first") or "")
    return Handoff(username, password, app, url, bool(raw.get("ari_enabled")),
                   int(first) if first.isdigit() else None)


def pattern(start: int) -> str:
    """The dialplan pattern for the kiosks' numbers: 801 → _8XX (as the page shows)."""
    t = str(start or 801)
    return "_" + t[0] + "X" * (len(t) - 1)


def kiosk_lines(pat: str, app: str) -> str:
    return (f"; Kiosk intercom: {pat[1:]} rings a kiosk through Kiosk Intercom Gateway.\n"
            f"exten => {pat},1,NoOp(Kiosk intercom)\n"
            f" same => n,Stasis({app},${{EXTEN}})\n"
            " same => n,Hangup()\n")


def default_extensions(pat: str, app: str) -> str:
    """The Asterisk app's own default dialplan, with the kiosk lines in place."""
    return ("[general]\nstatic=yes\n\n[default]\ninclude => parkedcalls\nexten => 444,1,Park(,s)\n"
            "exten => 555,1,ParkedCall(default,701)\n" + kiosk_lines(pat, app) +
            "exten => _X!,1,Dial(${PJSIP_DIAL_CONTACTS(${EXTEN})})\n\n"
            "[parkedcallstimeout]\nexten => s,1,Hangup()\n")


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """(name, header line, end line exclusive) of each section."""
    heads = [(m.group(1).strip(), i) for i, line in enumerate(lines) if (m := _SECTION.match(line))]
    ends = [i for _, i in heads[1:]] + ([len(lines)] if heads else [])
    return [(name, i, end) for (name, i), end in zip(heads, ends, strict=True)]


def _find(lines: list[str], name: str) -> tuple[int, int] | None:
    for n, start, end in _sections(lines):
        if n == name:
            return start, end
    return None


def ari_password(text: str | None, user: str) -> str:
    """The password ari.conf already gives ``user``, if any."""
    lines = (text or "").splitlines()
    where = _find(lines, user)
    if where is None:
        return ""
    for line in lines[where[0] + 1:where[1]]:
        m = re.match(r"^\s*password\s*=\s*(\S+)", line)
        if m:
            return m.group(1)
    return ""


def ari_conf(text: str | None, user: str, password: str) -> str:
    """ari.conf with ARI enabled and the gateway's user, everything else kept."""
    lines = (text or "").splitlines()
    general = _find(lines, "general")
    if general is None:
        lines = ["[general]", "enabled = yes", ""] + lines
    else:
        start, end = general
        for i in range(start + 1, end):
            if re.match(r"^\s*enabled\s*=", lines[i]):
                lines[i] = "enabled = yes"
                break
        else:
            lines.insert(start + 1, "enabled = yes")
    body = ["type = user", "read_only = no", f"password = {password}"]
    mine = _find(lines, user)
    if mine is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"[{user}]", *body]
    else:
        start, end = mine
        tail = end
        while tail > start + 1 and not lines[tail - 1].strip():
            tail -= 1  # keep the blank lines before the next section
        lines[start + 1:tail] = body
    return "\n".join(lines).strip("\n") + "\n"


def extensions_conf(text: str | None, pat: str, app: str) -> str | None:
    """extensions.conf with the kiosk lines in ``[default]`` above ``_X!``;
    None when they are there already."""
    if text is None or not text.strip():
        return default_extensions(pat, app)
    if f"Stasis({app}," in text:
        return None
    lines = text.splitlines()
    block = kiosk_lines(pat, app).rstrip("\n").split("\n")
    where = _find(lines, "default")
    if where is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "[default]", *block]
    else:
        start, end = where
        at = next((i for i in range(start + 1, end) if re.match(r"^\s*exten\s*=>\s*_X!", lines[i])), None)
        if at is None:
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1
        lines[at:at] = block
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass
class Result:
    folder: str
    password: str
    changed: list[str] = field(default_factory=list)  # files written
    unchanged: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    managed: Handoff | None = None  # the Asterisk app manages ARI and the dial plan

    def to_json(self) -> dict[str, object]:
        return {"folder": self.folder, "changed": self.changed, "unchanged": self.unchanged,
                "backups": self.backups, "managed": self.managed is not None,
                "restart_asterisk": self.managed is None and bool(self.changed)}


def _write(path: Path, text: str, result: Result) -> None:
    old = path.read_text() if path.exists() else None
    if old == text:
        result.unchanged.append(path.name)
        return
    if old is not None:
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if not backup.exists():  # the original, from before the gateway ever touched it
            shutil.copy2(path, backup)
            result.backups.append(backup.name)
    tmp = path.with_name(path.name + ".kig-tmp")
    tmp.write_text(text)
    tmp.replace(path)
    result.changed.append(path.name)


def status(folder: Path | None, user: str, app: str, password: str = "") -> dict[str, object]:
    """For the page: can the button be offered, and is it done already."""
    if folder is None:
        return {"available": False}
    offer = handoff(folder)
    if offer is not None:
        return {"available": True, "folder": str(folder), "slug": folder.parent.name,
                "managed": True, "ari_enabled": offer.ari_enabled,
                "done": (offer.username, offer.password, offer.app) == (user, password, app)}
    custom = folder / "custom"
    try:
        ari = (custom / "ari.conf").read_text() if (custom / "ari.conf").exists() else ""
        ext = (custom / "extensions.conf").read_text() if (custom / "extensions.conf").exists() else ""
    except OSError:
        return {"available": False}
    return {"available": True, "folder": str(folder), "slug": folder.parent.name,
            "done": bool(ari_password(ari, user)) and f"Stasis({app}," in ext}


def apply(folder: Path, user: str, app: str, start: int, password: str = "") -> Result:
    """Writes both files. Keeps a password ari.conf already has for ``user``
    unless one is given; makes one up if neither.

    With an Asterisk app that offers a login (see ``handoff``), writes nothing
    and returns that login instead."""
    offer = handoff(folder)
    if offer is not None:
        if not offer.ari_enabled:
            raise ManagedError("ARI is turned off in the Asterisk app. Turn on "
                               "“Allow apps to control calls” on its Apps tab, then try again.")
        _LOG.info("Asterisk in %s offers login %s; no files written", folder, offer.username)
        return Result(str(folder), offer.password, managed=offer)
    custom = folder / "custom"
    custom.mkdir(parents=True, exist_ok=True)
    ari_path, ext_path = custom / "ari.conf", custom / "extensions.conf"
    ari_old = ari_path.read_text() if ari_path.exists() else None
    password = password or ari_password(ari_old, user) or secrets.token_urlsafe(18)
    result = Result(str(folder), password)
    _write(ari_path, ari_conf(ari_old, user, password), result)
    ext_old = ext_path.read_text() if ext_path.exists() else None
    ext_new = extensions_conf(ext_old, pattern(start), app)
    if ext_new is None:
        result.unchanged.append(ext_path.name)
    else:
        _write(ext_path, ext_new, result)
    _LOG.info("Asterisk set up in %s: wrote %s", folder, ", ".join(result.changed) or "nothing")
    return result
