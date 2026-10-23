# Kiosk Intercom Gateway

Finds the Kiosk Satellite kiosks on your network and tracks whether each one can take an
intercom call. This is the groundwork for a gateway between the kiosk intercom and your SIP
PBX. Version 0.1.0 places no calls yet.

## Setup

1. Start the app and open **Kiosk Intercom** in the sidebar.
2. Paste the kiosks' shared **Intercom key** (any kiosk, Settings → Intercom) and Save.
   It applies at once and is also kept under Configuration.

Kiosks need **Remote management** and **Find other kiosks** on. A kiosk on another VLAN
can be added under **Extra kiosks** as `host` or `host:port` (remote admin port, 2324 by
default).

## Options

| Option | Meaning |
| --- | --- |
| Intercom key | The shared key. Kiosks read **Ready** only when it matches. |
| Extra kiosks | Kiosks to probe by address, for networks mDNS does not reach. |
| Discover kiosks over mDNS | Off to rely on Extra kiosks alone. |
| Probe interval | Seconds between identity checks of each kiosk (10–600). |
| Log level | debug, info, warning or error. |

## In Home Assistant

`sensor.kiosk_intercom_gateway_kiosks` shows the number of Ready kiosks. Its `kiosks`
attribute lists each one's name, address, status, area, DND and version. The app restores the
sensor within a minute of Home Assistant restarting.

Kiosks are matched to their ESPHome devices by name, which adds the area and the device's
`Intercom` sensor state. A kiosk whose name fits no device, or several, is listed without them.
