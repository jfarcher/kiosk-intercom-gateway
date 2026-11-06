"""Set up Asterisk for me: the right lines in, nothing else touched."""

from __future__ import annotations

from kig import asterisk_setup as a

APP = "kiosk-intercom"


def folder(tmp_path):
    f = tmp_path / "b35499aa_asterisk" / "asterisk"
    (f / "custom").mkdir(parents=True)
    return f


def test_find(tmp_path):
    assert a.find(tmp_path) is None
    assert a.find(tmp_path / "missing") is None
    f = folder(tmp_path)
    assert a.find(tmp_path) == f
    other = tmp_path / "12345678_asterisk" / "asterisk"
    other.mkdir(parents=True)
    assert a.find(tmp_path) == f  # TECH7Fox's first


def test_fresh_install(tmp_path):
    f = folder(tmp_path)
    assert a.status(f, "kiosk-gateway", APP) == {"available": True, "folder": str(f),
                                                 "slug": "b35499aa_asterisk", "done": False}
    r = a.apply(f, "kiosk-gateway", APP, 801)
    assert r.changed == ["ari.conf", "extensions.conf"] and r.backups == [] and len(r.password) > 16
    ari = (f / "custom" / "ari.conf").read_text()
    assert ari == (f"[general]\nenabled = yes\n\n[kiosk-gateway]\ntype = user\nread_only = no\n"
                   f"password = {r.password}\n")
    ext = (f / "custom" / "extensions.conf").read_text()
    assert ext == a.default_extensions("_8XX", APP)
    assert ext.index("Stasis(kiosk-intercom,${EXTEN})") < ext.index("exten => _X!")
    assert a.status(f, "kiosk-gateway", APP)["done"]

    again = a.apply(f, "kiosk-gateway", APP, 801)  # idempotent, same password
    assert again.changed == [] and again.password == r.password


def test_existing_files_are_kept_and_backed_up(tmp_path):
    f = folder(tmp_path)
    ari_old = "[general]\nenabled = no\npretty = yes\n\n[homeassistant]\ntype = user\npassword = theirs\n"
    ext_old = ("[general]\nstatic=yes\n\n[default]\nexten => 100,1,Answer()\n"
               "exten => _X!,1,Dial(PJSIP/${EXTEN})\n\n[other]\nexten => s,1,Hangup()\n")
    (f / "custom" / "ari.conf").write_text(ari_old)
    (f / "custom" / "extensions.conf").write_text(ext_old)
    r = a.apply(f, "kiosk-gateway", APP, 801, password="chosen")
    assert r.password == "chosen" and sorted(r.backups) == ["ari.conf.before-kiosk-gateway",
                                                            "extensions.conf.before-kiosk-gateway"]
    assert (f / "custom" / "ari.conf.before-kiosk-gateway").read_text() == ari_old
    ari = (f / "custom" / "ari.conf").read_text()
    assert "enabled = yes" in ari and "enabled = no" not in ari and "pretty = yes" in ari
    assert "[homeassistant]\ntype = user\npassword = theirs\n" in ari
    assert ari.endswith("[kiosk-gateway]\ntype = user\nread_only = no\npassword = chosen\n")
    ext = (f / "custom" / "extensions.conf").read_text().splitlines()
    i = ext.index("exten => _8XX,1,NoOp(Kiosk intercom)")
    assert ext[i - 1].startswith("; Kiosk intercom") and ext[i - 2] == "exten => 100,1,Answer()"
    assert ext[i + 3].startswith("exten => _X!") and ext[-1] == "exten => s,1,Hangup()"

    # A second run (a new password, say) keeps the original backup, from before the gateway.
    a.apply(f, "kiosk-gateway", APP, 801, password="new")
    assert (f / "custom" / "ari.conf.before-kiosk-gateway").read_text() == ari_old
    ari = (f / "custom" / "ari.conf").read_text()
    assert ari.count("[kiosk-gateway]") == 1 and "password = new" in ari and "password = chosen" not in ari


def test_section_replaced_in_the_middle(tmp_path):
    text = ("[general]\nenabled = yes\n\n[kiosk-gateway]\npassword = old\nread_only = yes\n"
            "\n[next]\ntype = user\n")
    assert a.ari_password(text, "kiosk-gateway") == "old"
    out = a.ari_conf(text, "kiosk-gateway", "old")
    assert out == ("[general]\nenabled = yes\n\n[kiosk-gateway]\ntype = user\nread_only = no\n"
                   "password = old\n\n[next]\ntype = user\n")
    assert a.ari_conf("", "u", "p").startswith("[general]\nenabled = yes\n")


def test_dialplan_variants():
    assert a.extensions_conf("[default]\nexten => 1,1,Answer()\n", "_8XX", APP).endswith(
        "exten => 1,1,Answer()\n" + a.kiosk_lines("_8XX", APP))  # no _X!: end of [default]
    no_default = a.extensions_conf("[phones]\nexten => 1,1,Answer()\n", "_8XX", APP)
    assert no_default.endswith("\n\n[default]\n" + a.kiosk_lines("_8XX", APP))
    assert a.extensions_conf("same => n,Stasis(kiosk-intercom,${EXTEN})", "_8XX", APP) is None
    assert a.pattern(801) == "_8XX" and a.pattern(5001) == "_5XXX"
