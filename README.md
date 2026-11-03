# Kiosk Intercom Gateway

A Home Assistant app that joins the [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite)
intercom from the outside, connecting the kiosks and the SIP phones on Asterisk both ways.

**Status: 0.3.0.** Dial a kiosk's extension and it rings like an intercom call. Pick a phone in a
kiosk's Call list and it rings through Asterisk. Audio is two-way 16 kHz in both directions.

## What it does

- **Finds kiosks automatically** over mDNS (`_kiosk-satellite._tcp`), the same way kiosks find
  each other. Kiosks mDNS cannot reach (another VLAN) can be added by address. Each kiosk is
  probed at `/api/intercom/identity` and shows the same status words as the kiosks' own list:

  | Status | Meaning |
  | --- | --- |
  | Ready | The intercom is on with the same key as the gateway. |
  | Do not disturb | The kiosk refuses calls right now. |
  | Different key | The kiosk holds another key. |
  | Intercom off | The kiosk is reachable but its intercom is off. |
  | Encrypted | The kiosk uses **Encrypt communications**, which the gateway does not speak yet. |
  | Unreachable / Offline | The kiosk does not answer, or no longer announces itself. |

- **Gives every kiosk an extension** (801, 802, …) that it keeps. A call to it over Asterisk's
  ARI rings the kiosk with the caller's name. The kiosk's own Ring / Auto / DND rules apply,
  and on answer the gateway bridges the audio. Declines, busy kiosks and timeouts reach the
  caller as the matching SIP cause.
- **Offers Asterisk's phones to the kiosks.** Each phone is announced over mDNS the way kiosks
  announce themselves, with its own port and intercom identity, so it appears in every kiosk's
  **Call a kiosk** list. The gateway rings it with ARI `originate`. A phone stays listed while
  it is registered, and the status page picks which phones are offered.
- **Matches kiosks to Home Assistant** through their ESPHome devices, which adds each kiosk's
  area and HA's `Intercom` call state (best effort, by name).
- **Shows it all** on an ingress page (sidebar: *Kiosk Intercom*): kiosks, extensions, the
  Asterisk connection, live and recent calls, and setup steps. It also publishes
  `sensor.kiosk_intercom_gateway_kiosks`.

## Install

1. In Home Assistant go to **Settings → Apps** (formerly Add-ons) **→ store → ⋮ → Repositories**
   and add `https://github.com/jfarcher/kiosk-intercom-gateway`.
2. Install **Kiosk Intercom Gateway**.
3. Start the app and open **Kiosk Intercom** in the sidebar.
4. Paste the **Intercom key** from any kiosk (Settings → Intercom) and Save.

Every kiosk needs **Remote management** and **Find other kiosks** turned on, as for the
intercom itself. For calls, set up the [Asterisk app](https://github.com/TECH7Fox/asterisk-hass-addons)
as the app's [documentation](kiosk_intercom_gateway/DOCS.md#2-connect-to-asterisk) describes:
one `ari.conf` and three dialplan lines.

## How the intercom works

The protocol was worked out from reading the Kiosk Satellite source. No code from it is used.

- **Discovery:** mDNS `ks-<id>._kiosk-satellite._tcp.local`, TXT `id`, `name`, `version`,
  `port` (remote admin), `tls`.
- **Identity:** `GET /api/intercom/identity` on the admin port returns
  `{id, name, version, enabled, key, dnd, endpoint: {port, tls}}`. `key` is SHA-256(key)[:8].
  It is limited to one probe per second per client.
- **Signaling:** the caller sends `POST /api/intercom/call` with
  `{call, kind: call|broadcast, from: {id, name, port, version}}`. The callee replies
  `ringing` / `auto` / `busy` / `dnd` / `off`, or 403 for a wrong key. Later steps go back and
  forth as `POST /api/intercom/call/<id>` with `{action: answer|decline|missed|cancel|hangup}`.
- **Audio:** the caller opens `ws(s)://callee/api/intercom/audio/<id>?token=…`. Binary frames
  are PCM16 LE mono 16 kHz, 80 ms each. Text frames are `{"type":"talk","on":bool}` and
  `{"type":"end"}`.
- **Auth:** each request carries a single-use HMAC-SHA256 token keyed with `"intercom:" + key`.
  It is valid for 60 s (see [`protocol.py`](kiosk_intercom_gateway/src/kig/protocol.py)).

The wire format is internal to Kiosk Satellite and may change between releases.

## How a SIP call reaches a kiosk

```
phone ──SIP──▶ Asterisk ──Stasis(kiosk-intercom,801)──▶ gateway (ARI events)
                  │                                       │ POST /api/intercom/call  ──▶ kiosk rings
                  │                                       │ ◀── POST …/call/<id> answer
                  │◀──ARI: answer, externalMedia, bridge──┤
                  │◀═══ AudioSocket slin16 (TCP) ════════▶│◀═══ WebSocket PCM16 ═══▶ kiosk
```

The gateway acts as a kiosk toward the kiosk and as a Stasis app toward Asterisk. Call audio
uses **AudioSocket**, not RTP. Asterisk 22's ARI external media over RTP sends slin16 (payload
type 118) but drops it on receive: dynamic payload types need a receive mapping chan_rtp
never sets up. AudioSocket carries slin16 both ways (kind `0x12`, Asterisk 20.13 / 21.8 /
22.3 and later) in the kiosks' own byte order.

A kiosk calling a phone runs the same wire with the roles swapped. The kiosk POSTs the call to
the phone's own port, the gateway originates `PJSIP/<ext>` with `appArgs kig-out,<call id>`, and
on answer the gateway bridges AudioSocket and signals `answer`. The kiosk then opens its audio
WebSocket to the phone's port. The gateway re-sends each phone's PTR, SRV, TXT and A records in
one packet every 30 s, because kiosks drop a peer after three missed announcements.

CI places real calls through the Asterisk app's own image
(`ghcr.io/tech7fox/asterisk-hass-addon`), both into a simulated kiosk and from it to two PJSIP
test phones that Asterisk answers itself. It checks the audio in both directions and the
clean-up afterwards.

## Roadmap

1. Discovery and roster. Done in 0.1.
2. SIP → kiosk calls. Done in 0.2.
3. Kiosk → SIP calls. Done in 0.3.
4. **Announce to all** from a phone, and HA entities and actions for the gateway's calls.
5. HTTPS/WSS for kiosks with **Encrypt communications**.

## Related work

[Eibelucas/Kiosk-Satellite-SIP](https://github.com/Eibelucas/Kiosk-Satellite-SIP) is a SIP
phone plugin plus a HAOS add-on that bundles Asterisk and bridges one configured kiosk to it.
This project differs in its focus: many auto-discovered kiosks, each with its own extension,
wideband audio, and the Asterisk app you may already run.

## Development

```sh
pip install -r requirements-dev.txt
pytest                      # unit and end-to-end tests against a fake Asterisk and fake kiosks
ruff check .
cd kiosk_intercom_gateway/src && KIG_KEY=<key> KIG_OPTIONS=/dev/null python3 -m kig  # page on :8099
```

## Licence

MIT. Kiosk Satellite is © Xavier Larrea under its own licence. This project talks to it over
the network and contains none of its code.
