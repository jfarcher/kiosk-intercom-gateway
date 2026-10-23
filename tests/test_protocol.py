import base64
import hashlib
import hmac
import json

from kig import protocol as p

KEY = "s3cr3t-intercom-key"


def test_fingerprint_matches_identity_format():
    assert p.fingerprint(KEY) == hashlib.sha256(KEY.encode()).hexdigest()[:8]
    assert p.fingerprint("") == ""


def test_frame_size():
    assert p.FRAME_BYTES == 2560


def test_token_round_trip():
    t = p.issue_token(KEY, "call-1", "gw")
    claims = p.token_claims(KEY, t)
    assert claims["intercom"] == "call-1" and claims["from"] == "gw"
    assert isinstance(claims["exp"], int)


def test_token_shape_is_what_a_kiosk_expects():
    """A kiosk splits on '.', base64url-decodes the payload (padded, like Dart's
    base64Url) and compares HMAC-SHA256 keyed with 'intercom:<key>'."""
    t = p.issue_token(KEY, "c", "gw", now=1000.0)
    payload, sig = t.split(".")
    assert len(payload) % 4 == 0 and len(sig) % 4 == 0
    expected = base64.urlsafe_b64encode(
        hmac.new(f"intercom:{KEY}".encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    assert sig == expected
    assert json.loads(base64.urlsafe_b64decode(payload))["exp"] == 1_060_000


def test_token_from_a_kiosk_verifies():
    """Built by hand the way the kiosk builds it, with Dart's compact JSON."""
    payload = base64.urlsafe_b64encode(
        b'{"intercom":"k1","from":"kitchen","n":"abc","exp":9999999999999}'
    ).decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(f"intercom:{KEY}".encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    v = p.TokenVerifier(KEY)
    assert v.verify(f"{payload}.{sig}", "k1")


def test_rejects_wrong_key_tamper_expiry_call_and_replay():
    v = p.TokenVerifier(KEY)
    assert not v.verify(p.issue_token("other", "c", "gw"), "c")
    t = p.issue_token(KEY, "c", "gw")
    assert not v.verify(t[:-2] + "AA", "c")
    assert not v.verify(p.issue_token(KEY, "c", "gw", now=0), "c")
    assert not v.verify(p.issue_token(KEY, "c", "gw"), "different-call")
    assert v.verify(t, "c")
    assert not v.verify(t, "c")  # once only
    assert p.token_claims("", t) is None
    assert p.token_claims(KEY, "not-a-token") is None
