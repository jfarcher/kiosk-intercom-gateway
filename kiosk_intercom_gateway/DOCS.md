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

### Kiosks that are gone

A kiosk that's been reflashed, replaced or retired shows **Offline** (it stopped announcing itself)
or **Unreachable** (it's listed but doesn't answer). Its row says for how long and has **Forget**:
click it twice to take the kiosk off the list and free its extension for the next kiosk. A kiosk
added under **Extra kiosks** is removed from there too. If a forgotten kiosk turns up again, it's
found afresh and gets a new number.

An Offline kiosk also leaves the list by itself after 7 days, keeping its number, so it gets the
same one back if it returns; only Forget frees the number. Kiosks under Extra kiosks stay until
you forget them. A reinstalled kiosk at the same address needs neither: it keeps its number.

## 2. Connect to Asterisk

This works with the **Asterisk** app for Home Assistant (TECH7Fox).

The quick way: install the Asterisk app and start it once, then press **Set up Asterisk for me** on
this app's status page and restart the Asterisk app when it says so. The button:

- adds this app's ARI user (with a made-up password) to the Asterisk app's `custom/ari.conf`, and
  turns ARI on;
- adds the kiosk lines to `custom/extensions.conf`, inside `[default]` above the `_X!` line, or
  writes the Asterisk app's default dialplan with them in place if there's no such file;
- keeps everything else in both files, and a copy of each as it was before
  (`ari.conf.before-kiosk-gateway`, `extensions.conf.before-kiosk-gateway`);
- saves the ARI username and password in this app's Configuration and restarts this app.

Pressing it again changes nothing. It needs this app's access to other apps' configuration folders,
which Home Assistant lists under the app's permissions; it touches only those two files. This app
can't restart the Asterisk app itself, which would need more permissions, so the button links to it.

To do it by hand instead (or with Asterisk on another machine), the status page shows these steps
with your own values filled in and copy buttons.

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

## Announce to all

Dial **800** from any phone to talk to every Ready kiosk at once, one way, the same as a kiosk's own
**Announce to all**:

- Each kiosk plays its chime, shows who's announcing, and offers **Reply** (which calls your phone
  back) and **Dismiss**.
- Kiosks on Do not disturb, already in a call, or with **Accept announcements** off are skipped.
- You hear a beep once the kiosks are listening, so start talking then.
- Hang up to end it. If every kiosk dismisses it, the call ends too.
- The `_8XX` dialplan line already sends 800 here. **Announce to all number** changes it, and kiosks
  are never given that number.
- From an automation, the `call` action with `kiosk: all` rings a phone and, once it's picked up,
  announces from it. Its events have `direction: phone_to_all` and a `kiosks` list of who heard it.

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

## Encrypted kiosks

Kiosks with **Encrypt communications** on (Settings → Intercom) work like any other, both ways. The
status page marks them *encrypted*. Nothing needs setting:

- The gateway talks to them over HTTPS and WSS only, as they talk to each other, and listens on a
  second, encrypted port for them. Its certificate is self-signed and made on first start; kiosks
  don't check each other's certificates, and the gateway doesn't check theirs.
- Each phone gets an encrypted port too. A kiosk is pointed at the port that matches its own
  setting, so encrypted and unencrypted kiosks can both call the phones.
- Encrypted kiosks still can't call unencrypted kiosks, or the reverse. That's the kiosks' own rule.
- Call audio between the gateway and Asterisk stays on this machine (or your LAN, when Asterisk runs
  elsewhere) and isn't encrypted. Phones' own SIP and RTP are whatever Asterisk is set up to use.

## Automations

The status page's **Use calls in automations** card has these examples with this app's slug and your
kiosk and phone names filled in.

### Events

Every call fires `kiosk_intercom_gateway_call` as it rings, is answered and ends:

| Field | Value |
| --- | --- |
| `state` | `ringing`, `answered` or `ended` |
| `direction` | `phone_to_kiosk` or `kiosk_to_phone` |
| `kiosk`, `kiosk_id`, `kiosk_extension` | The kiosk: *Kitchen*, its id, *801* |
| `phone`, `phone_number` | The SIP side: *Alice (101)*, *101* (for a door station, whatever its caller ID says) |
| `reason` (ended) | `ended`, `declined`, `busy`, `dnd`, `no_answer`, `unavailable`, `unknown_kiosk`, `failed` |
| `duration` (ended) | Seconds talked, 0 if never answered |
| `call_id` | The same for all three events of a call |

```yaml
triggers:
  - trigger: event
    event_type: kiosk_intercom_gateway_call
    event_data:
      state: ended
      reason: no_answer
actions:
  - action: persistent_notification.create
    data:
      message: "Missed: {{ trigger.event.data.phone }} and {{ trigger.event.data.kiosk }}"
```

### Actions

Apps take actions through `hassio.addon_stdin`. Use this app's slug, shown on the card and in the
app's address in Settings → Apps.

**Ring a phone, then a kiosk** (click to call). The phone rings showing the kiosk. Once it's picked
up, the phone hears ringback while the kiosk rings, showing the phone:

```yaml
action: hassio.addon_stdin
data:
  addon: <slug>
  input:
    action: call
    phone: "101"
    kiosk: Kitchen        # a kiosk's name or extension
```

**Hang up** a kiosk's calls, a phone's calls (`phone: "101"`), one call (`call: <call_id>`), or every
call (nothing named):

```yaml
action: hassio.addon_stdin
data:
  addon: <slug>
  input:
    action: hangup
    kiosk: Kitchen
```

**A kiosk calls a phone** with the kiosk's own ESPHome action, the phone named as it appears in the
kiosks' list:

```yaml
action: esphome.<kiosk node>_intercom_call
data:
  kiosk: "Alice (101)"
```

From the Asterisk dialplan, a kiosk is `Local/<extension>@default`, for example
`Dial(PJSIP/1001&Local/801@default,30)` to ring a desk phone and the kitchen kiosk together.
`DIALSTATUS` reports BUSY, NOANSWER or CONGESTION as usual.

A failed action, such as an unknown kiosk or Asterisk not connected, logs a warning in this app's log.

### Sensors

| Entity | State | Attributes |
| --- | --- | --- |
| `sensor.kiosk_intercom_gateway_kiosks` | Ready kiosks | Every kiosk: name, extension, status, area… |
| `sensor.kiosk_intercom_gateway_calls` | Live calls | Each: direction, state, kiosk, phone, since |
| `binary_sensor.kiosk_intercom_phone_<extension>` | On while registered | on_kiosks, busy, found_by |
| `binary_sensor.kiosk_intercom_gateway_problem` | On while something's wrong | problems: id, kind, message |
| `binary_sensor.kiosk_intercom_gateway_asterisk` | On while connected to Asterisk | asterisk_version, error |

These are set by the app, so they have no settings page in Home Assistant. They come back within a
minute of Home Assistant restarting.

## Problems

When something stops calls working, the app tells Home Assistant: a notification (under
Notifications) and `binary_sensor.kiosk_intercom_gateway_problem`, and a banner on the status page.
The notification goes away by itself once it's fixed.

| Problem | After |
| --- | --- |
| Not connected to Asterisk, with the reason (wrong password, unreachable…) | 2 minutes |
| No intercom key set on the gateway | 1 minute |
| A kiosk holds a different intercom key | 1 minute |
| A kiosk that was Ready stopped answering or announcing itself | 10 minutes |
| A phone you chose for the kiosks stopped registering (sensor and page only, no notification) | 10 minutes |

The waits keep restarts and reboots from raising alarms. **Notify about problems** turns the
notifications off; the sensor and banner stay. For a phone notification, or one on your phone,
use the sensor in an automation:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.kiosk_intercom_gateway_problem
    to: "on"
actions:
  - action: notify.mobile_app_<your phone>
    data:
      message: "{{ state_attr('binary_sensor.kiosk_intercom_gateway_problem', 'problems')
                   | map(attribute='message') | join(' ') }}"
```

## Options

| Option | Meaning |
| --- | --- |
| Intercom key | The kiosks' shared key. Kiosks read **Ready** only when it matches. |
| Extra kiosks | Kiosks to probe by address, for networks mDNS does not reach. |
| Discover kiosks over mDNS | Turn off to rely on Extra kiosks alone. |
| Probe interval | Seconds between identity checks of each kiosk (10–600). |
| Asterisk ARI address | `http://127.0.0.1:8088` for the Asterisk app on this Home Assistant. |
| ARI username, ARI password | The user from `ari.conf`. Empty leaves SIP calls off. |
| First kiosk extension | Kiosks are numbered from here (801) in the order they're first seen, and keep their numbers, through a reinstall too. **Change** beside a kiosk on the page gives it another. Avoid 700–703, the Asterisk app's call parking. |
| Ring timeout | Seconds a kiosk may ring before the caller hears no answer. |
| Intercom port | The port kiosks reach the gateway on during a call. 0 picks a free one. |
| Kiosks can call phones | Offer Asterisk's phones in the kiosks' Call list. |
| Phones | Which phones to offer, as `101` or `101=Front door`. The status page edits this for you. Empty offers every numbered extension. |
| Advertise address | The address kiosks reach the phones on. Empty finds this machine's LAN address. Set it when Home Assistant has more than one network. |
| Call audio address | Where Asterisk connects for call audio: `127.0.0.1` when Asterisk runs on this Home Assistant, this machine's LAN address when it runs elsewhere. |
| Announce to all number | Dial it to talk to every Ready kiosk at once. 800 by default. |
| Notify about problems | A Home Assistant notification per problem (above), cleared once fixed. On by default. |
| Log level | debug, info, warning or error. |

## In Home Assistant

`sensor.kiosk_intercom_gateway_kiosks` shows the number of Ready kiosks. Its `kiosks`
attribute lists each kiosk's name, extension, address, status, area, DND, encryption and version.

Kiosks are matched to their ESPHome devices by name, which adds the area and the
device's `Intercom` sensor state. A kiosk whose name fits no device, or several, is
listed without them.

## Not yet

- A kiosk's Announce to all reaching phones (phones skip it).
