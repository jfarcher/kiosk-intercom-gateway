# Kiosk Intercom Gateway

Connects your Kiosk Satellite kiosks and the SIP phones on Asterisk, both ways:

- **Phones call kiosks.** Each kiosk gets an extension. Dial it, and the kiosk rings exactly as
  it does for an intercom call from another kiosk.
- **Kiosks call phones.** Asterisk's phones appear in every kiosk's **Call a kiosk** list. Tap
  one and it rings.

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

3. Create `/addon_configs/b35499aa_asterisk/asterisk/custom/extensions.conf` with the Asterisk
   app's default dialplan plus the kiosk lines. The status page has the file ready to copy:

   ```ini
   [general]
   static=yes

   [default]
   include => parkedcalls
   exten => 444,1,Park(,s)
   exten => 555,1,ParkedCall(default,701)
   ; Kiosk intercom: 8XX rings a kiosk through Kiosk Intercom Gateway.
   exten => _8XX,1,NoOp(Kiosk intercom)
    same => n,Stasis(kiosk-intercom,${EXTEN})
    same => n,Hangup()
   exten => _X!,1,Dial(${PJSIP_DIAL_CONTACTS(${EXTEN})})

   [parkedcallstimeout]
   exten => s,1,Hangup()
   ```

   If you already have your own `custom/extensions.conf`, add just the three kiosk lines, inside
   its `[default]` section and above the `_X!` line. Lines anywhere else are never reached.

4. Restart the Asterisk app.
5. In this app's **Configuration**, set **ARI username** (`kiosk-gateway`) and
   **ARI password**, save and restart.

The status page should show **Connected to Asterisk**. Dial a kiosk's extension from
any phone registered to Asterisk.

## Phones calling kiosks

- The kiosk shows the caller's name and number, such as *Front door (201)*. It answers
  according to its own Answer mode: Ring, Answer automatically or Do not disturb.
- Audio is two-way 16 kHz, the kiosks' own quality. The kiosk's Talk mode applies:
  with Push to talk, the person at the kiosk holds the button to speak.
- Either side can hang up. A kiosk that declines, is busy, is on Do not disturb or can't
  be reached gives the caller a busy or congestion signal. One that doesn't answer
  within **Ring timeout** gives no answer.
- Dialling a kiosk's name instead of its number also works, from a phone that can
  dial names or from the dialplan: `Stasis(kiosk-intercom,Kitchen)`.

## Kiosks calling phones

The **Phones kiosks can call** card on the status page lists every phone (PJSIP endpoint)
Asterisk has:

- By default every numbered extension is offered, named by its caller ID. Untick a phone to
  hide it, or type a name to show instead. **Save** applies the change at once and keeps it
  in the app's Configuration. **Back to automatic** returns to every numbered extension.
- A phone is on the kiosks' **Call a kiosk** list while it is registered with Asterisk. A
  phone that's switched off, or a browser softphone that's closed, drops off the list
  within about a minute and returns when it registers again.
- The phone rings showing the kiosk's name and extension, such as *Kitchen (801)*, so it can
  call straight back.
- The phone rings for 30 seconds, a kiosk's default ring time. Busy and declined calls show on
  the kiosk as declined, unanswered ones as no answer.
- After a missed call from a phone that's on the list, the kiosk's **Call back** rings
  that phone.

How it works: kiosks find each other over mDNS, so the gateway announces each phone the same
way, with a port of its own. Kiosks therefore need to hear the gateway's mDNS, which is the
same requirement as for hearing each other. Their Remote Administration switcher lists these
phones too. Opening one there shows a page explaining what it is.

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
| Kiosks can call phones | Offer Asterisk's phones in the kiosks' Call list. |
| Phones | Which phones to offer, as `101` or `101=Front door`. The status page edits this for you. Empty offers every numbered extension. |
| Advertise address | The address kiosks reach the phones on. Empty finds this machine's LAN address. Set it when Home Assistant has more than one network. |
| Call audio address | Where Asterisk connects for call audio: `127.0.0.1` when Asterisk runs on this Home Assistant, this machine's LAN address when it runs elsewhere. |
| Log level | debug, info, warning or error. |

## In Home Assistant

`sensor.kiosk_intercom_gateway_kiosks` shows the number of Ready kiosks. Its `kiosks`
attribute lists each kiosk's name, extension, address, status, area, DND and version.

Kiosks are matched to their ESPHome devices by name, which adds the area and the
device's `Intercom` sensor state. A kiosk whose name fits no device, or several, is
listed without them.

## Not yet

- Announce to all from a phone, or to phones from a kiosk (phones skip Announce to all).
- Kiosks with **Encrypt communications** on. They show **Encrypted** and can't be called.
