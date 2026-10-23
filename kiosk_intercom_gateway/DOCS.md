# Kiosk Intercom Gateway

Lets SIP phones call your Kiosk Satellite kiosks through Asterisk. Each kiosk gets an
extension. Dial it, and the kiosk rings exactly as it does for an intercom call from
another kiosk.

## 1. Connect to the kiosks

1. Start the app and open **Kiosk Intercom** in the sidebar.
2. Paste the kiosks' shared **Intercom key** (on any kiosk, Settings → Intercom) and Save.

Kiosks need **Remote management** and **Find other kiosks** on, as for the intercom
itself. They should read **Ready** within a few seconds. A kiosk on another VLAN can be
added under **Extra kiosks** as `host` or `host:port` (remote admin port, 2324 by default).

## 2. Connect to Asterisk

This works with the **Asterisk** app for Home Assistant (TECH7Fox). The status page
shows these steps with your own values filled in and copy buttons.

1. Install the Asterisk app and start it once.
2. Create `/addon_configs/b35499aa_asterisk/asterisk/custom/ari.conf`:

   ```ini
   [general]
   enabled = yes

   [kiosk-gateway]
   type = user
   read_only = no
   password = CHOOSE-A-PASSWORD
   ```

3. Copy `/addon_configs/b35499aa_asterisk/asterisk/default/extensions.conf` to
   `.../custom/extensions.conf`, and add these lines to its `[default]` section,
   above the `_X!` line:

   ```ini
   exten => _8XX,1,NoOp(Kiosk intercom)
    same => n,Stasis(kiosk-intercom,${EXTEN})
    same => n,Hangup()
   ```

4. Restart the Asterisk app.
5. In this app's **Configuration**, set **ARI username** (`kiosk-gateway`) and
   **ARI password**, save and restart.

The status page should show **Connected to Asterisk**. Dial a kiosk's extension from
any phone registered to Asterisk.

## Calls

- The kiosk shows the caller's name and number, such as *Front door (201)*. It answers
  according to its own Answer mode: Ring, Answer automatically or Do not disturb.
- Audio is two-way 16 kHz, the kiosks' own quality. The kiosk's Talk mode applies:
  with Push to talk, the person at the kiosk holds the button to speak.
- Either side can hang up. A kiosk that declines, is busy, is on Do not disturb or can't
  be reached gives the caller a busy or congestion signal. One that doesn't answer
  within **Ring timeout** gives no answer.
- Dialling a kiosk's name instead of its number also works, from a phone that can
  dial names or from the dialplan: `Stasis(kiosk-intercom,Kitchen)`.

## Options

| Option | Meaning |
| --- | --- |
| Intercom key | The kiosks' shared key. Kiosks read **Ready** only when it matches. |
| Extra kiosks | Kiosks to probe by address, for networks mDNS does not reach. |
| Discover kiosks over mDNS | Turn off to rely on Extra kiosks alone. |
| Probe interval | Seconds between identity checks of each kiosk (10–600). |
| Asterisk ARI address | `http://127.0.0.1:8088` for the Asterisk app on this Home Assistant. |
| ARI username, ARI password | The user from `ari.conf`. Empty leaves SIP calls off. |
| First kiosk extension | Kiosks are numbered from here (801) in the order they're first seen, and keep their numbers. Avoid 700–703, the Asterisk app's call parking. |
| Ring timeout | Seconds a kiosk may ring before the caller hears no answer. |
| Intercom port | The port kiosks reach the gateway on during a call. 0 picks a free one. |
| Call audio address | Where Asterisk connects for call audio: `127.0.0.1` when Asterisk runs on this Home Assistant, this machine's LAN address when it runs elsewhere. |
| Log level | debug, info, warning or error. |

## In Home Assistant

`sensor.kiosk_intercom_gateway_kiosks` shows the number of Ready kiosks. Its `kiosks`
attribute lists each kiosk's name, extension, address, status, area, DND and version.

Kiosks are matched to their ESPHome devices by name, which adds the area and the
device's `Intercom` sensor state. A kiosk whose name fits no device, or several, is
listed without them.

## Not yet

- Kiosks calling SIP phones, and Announce to all from a phone.
- Kiosks with **Encrypt communications** on. They show **Encrypted** and can't be called.
