# Changelog

## 0.3.1

- Fix phones vanishing from the kiosks after **Save** on the Phones card. Changing a phone
  withdrew its announcement and made a new one, and the withdrawal's goodbye packets arrived
  after it, so kiosks dropped the phone for up to 30 seconds. Changes are now announced in place.
- Each offered phone on the Phones card says which kiosks have found it, or that none has yet.
  That shows whether the kiosks hear the gateway's announcements.

## 0.3.0

- **Kiosks can call SIP phones.** Asterisk's phones appear in every kiosk's **Call a kiosk**
  list. Tapping one rings the phone, which shows the kiosk's name and extension. Answered, it's
  a two-way 16 kHz call. Busy, declined and unanswered calls show on the kiosk as such.
- A phone is listed while it's registered with Asterisk, and drops off when it isn't.
- **Pick the phones on the status page.** The new Phones card lists every Asterisk phone with
  its registration, a checkbox to offer it to kiosks and a name to show instead of its caller ID.
  The choice applies at once and is kept in the Configuration (`phones`).
- A kiosk's **Call back** after a missed call from an offered phone now rings that phone.
- The Asterisk setup card offers the complete `custom/extensions.conf` to paste, so the
  kiosk lines can't end up in the wrong section.
- New options: Kiosks can call phones, Phones, Advertise address.

## 0.2.3

- A new icon and logo: a blue house with a phone inside.

## 0.2.2

- Fix "Home Assistant update failed: WebSocketError message too big" on larger
  installs: the entity registry can be a single message over 4 MiB. The limit is now 64 MiB.
- That failure also stopped `sensor.kiosk_intercom_gateway_kiosks` from being published.
  Each Home Assistant step now fails on its own, so the sensor keeps updating.
- The registries are re-read every 10 minutes instead of every minute. The sensor
  and kiosks' call states still update every minute.

## 0.2.1

- The ARI address works with or without `/ari` at the end
  (`http://127.0.0.1:8088` and `http://127.0.0.1:8088/ari` both work). Before, the
  second asked Asterisk for `/ari/ari/…` and got 404.
- A 404 or 401 from Asterisk now says what it means on the status page and in the log.

## 0.2.0

- **SIP phones can call kiosks.** Dial a kiosk's extension on Asterisk and the kiosk
  rings like an intercom call, with the caller's name on screen and the kiosk's own
  Ring, Answer automatically and Do not disturb rules. Answered, the call is two-way
  16 kHz audio. Either side can hang up. A kiosk that declines, is busy or on Do not
  disturb gives the caller a busy signal.
- Every kiosk gets an extension from 801 that it keeps, shown on the status page and
  in `sensor.kiosk_intercom_gateway_kiosks`. Dialling a kiosk's name works too.
- The status page shows the Asterisk connection, live and recent calls, and the
  setup steps for the Home Assistant Asterisk app with ready-to-copy config.
- New options: ARI address, username and password, first extension, ring timeout,
  intercom port and call audio address.

## 0.1.3

- Set the intercom key from the gateway's own page: paste it and Save. It takes
  effect at once, without a restart, and is saved to the app's Configuration so
  both stay in step. **Change key** replaces it later.

## 0.1.2

- Fix 0.1.1 still starting on port 8099. s6-overlay hid the Supervisor's token
  from the app, so it could not ask for its ingress port and also ran with the
  Home Assistant integration off. The app now starts through `with-contenv`, also
  reads s6's copy of the token, and stops with a clear error instead of guessing
  a port when it runs under the Supervisor.

## 0.1.1

- Fix "address in use" on port 8099 at start. The status page now listens on a free
  port the Supervisor assigns for ingress, instead of a fixed one that another app
  on the host may hold.

## 0.1.0

- First version: discovers Kiosk Satellite kiosks over mDNS (plus kiosks added
  by address), probes each one's intercom identity, checks the shared key and
  shows the result on an ingress page and as `sensor.kiosk_intercom_gateway_kiosks`.
- Matches kiosks to their ESPHome devices in Home Assistant for area and call state.
- Intercom token signing and verification compatible with Kiosk Satellite.
