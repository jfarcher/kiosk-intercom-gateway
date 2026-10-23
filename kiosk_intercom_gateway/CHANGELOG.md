# Changelog

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
