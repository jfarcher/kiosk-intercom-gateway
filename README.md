# Kiosk Intercom Gateway

A Home Assistant app that joins the [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite)
intercom from the outside. The goal is a two-way gateway between the kiosks and an existing
SIP PBX: dial an extension and a kiosk rings, or pick a phone from a kiosk's Call list.

**Status: 0.1.0, discovery only.** This version finds and tracks every kiosk and checks that
it can be called. It also carries the intercom protocol layer the call bridge will build on.
No calls are placed yet.

## What 0.1.0 does

- **Finds kiosks automatically** over mDNS (`_kiosk-satellite._tcp`), the same way kiosks find
  each other. Kiosks mDNS cannot reach (another VLAN) can be added by address.
- **Probes each kiosk** at `/api/intercom/identity` and shows the same status words as the
  kiosks' own Kiosks list:

  | Status | Meaning |
  | --- | --- |
  | Ready | The intercom is on with the same key as the gateway. |
  | Do not disturb | The kiosk refuses calls right now. |
  | Different key | The kiosk holds another key. |
  | Intercom off | The kiosk is reachable but its intercom is off. |
  | Encrypted | The kiosk uses **Encrypt communications**, which the gateway does not speak yet. |
  | Unreachable / Offline | The kiosk does not answer, or no longer announces itself. |

- **Matches kiosks to Home Assistant** through their ESPHome devices, which adds each kiosk's
  area and HA's `Intercom` call state. This is best effort, by name.
- **Shows the roster** on an ingress page (sidebar: *Kiosk Intercom*). It is also published as
  `sensor.kiosk_intercom_gateway_kiosks`: the state is the number of Ready kiosks, and the
  attributes list every kiosk.

## Install

1. In Home Assistant go to **Settings → Apps** (formerly Add-ons) **→ store → ⋮ → Repositories**
   and add `https://github.com/jfarcher/kiosk-intercom-gateway`. While this repository is
   private, Home Assistant needs credentials in the URL to read it.
2. Install **Kiosk Intercom Gateway**.
3. Start the app and open **Kiosk Intercom** in the sidebar.
4. Paste the **Intercom key** from any kiosk (Settings → Intercom) and Save.

Every kiosk needs **Remote management** and **Find other kiosks** turned on, as for the
intercom itself.

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

## Roadmap

1. **Discovery and roster.** Done in 0.1.0.
2. **SIP → kiosk:** a call to an extension on your PBX rings the matching kiosk, which answers
   with its own Ring / Auto / DND rules.
3. **Kiosk → SIP:** SIP destinations are advertised over mDNS, one entry and port per
   destination, so they appear in each kiosk's Call list.
4. **Announce to all** from SIP, and HA entities and actions for the gateway's own calls.
5. HTTPS/WSS for kiosks with **Encrypt communications**.

The open design question for steps 2 and 3 is how to reach the existing PBX:

- **Register as SIP endpoints.** Works with any PBX (FreePBX, 3CX, a FRITZ!Box, plain
  Asterisk), with a SIP stack inside the app.
- **Asterisk ARI `externalMedia` with `slin16`.** Asterisk only, but 16 kHz end to end with no
  SIP stack.

## Related work

[Eibelucas/Kiosk-Satellite-SIP](https://github.com/Eibelucas/Kiosk-Satellite-SIP) is a SIP
phone plugin plus a HAOS add-on that bundles Asterisk and bridges one configured kiosk to it.
This project differs in its focus: many auto-discovered kiosks, both call directions, and your
existing PBX.

## Development

```sh
pip install -r requirements-dev.txt
pytest
cd kiosk_intercom_gateway/src && KIG_KEY=<key> KIG_OPTIONS=/dev/null python3 -m kig  # page on :8099
```

## Licence

MIT. Kiosk Satellite is © Xavier Larrea under its own licence. This project talks to it over
the network and contains none of its code.
