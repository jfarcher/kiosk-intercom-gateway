# Changelog

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
