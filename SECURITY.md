# Security

## What the app can reach

The app runs as a Home Assistant app (add-on) with the access its `config.yaml` asks for:

- **MQTT**, through the broker Home Assistant is set up with. It reads every message under
  each Zigbee2MQTT base topic it tracks, and publishes to them when you ask it to: rename a
  device, set an area, reconfigure, start a routing scan, open pairing through a router, or
  start an OTA update. It also publishes its own MQTT discovery sensors and an availability
  topic under `z2m_tracker/`.
- **The Supervisor and Home Assistant APIs**, with the token the Supervisor gives every app.
  It uses them to send notifications, fire `z2m_tracker_alert` events, call the notify
  service you name, read areas, and save its own options.
- **Your Home Assistant configuration folder, read-only.** The history import reads
  Zigbee2MQTT's own files from there (`configuration.yaml`, `database.db`, state files) to
  fill in devices it saw before the app was installed. Nothing in that folder is written.

Its history, metrics and scans are stored in the app's own `/data` folder, which Home
Assistant includes in backups.

## Who can talk to it

The web interface listens on port 8099 inside the Supervisor's network and is not published
on the host. Requests are accepted only from Home Assistant's ingress proxy and from Home
Assistant itself (the integration); anything else gets a 403. Opening it from the sidebar
means you are already signed in to Home Assistant, and it has the same reach as any
administrator of that Home Assistant.

The app makes no requests to the internet. The battery library and the manufacturer (OUI)
list are carried in the image rather than fetched.

## The integration and the card

The integration talks only to the app's address on the Supervisor network
(`http://local-z2m-tracker:8099` unless you change it) and turns what it reads into sensors.
The card reads those sensors and draws them in your browser.

## Reporting something

Please report privately through [GitHub's advisory form][advisory] rather than opening an
issue, and give it a few days before saying anything publicly.

Useful things to include: what an attacker would gain, how to reproduce it, and the app,
integration and Home Assistant versions. A proof of concept is welcome but not required.

[advisory]: https://github.com/tempus2016/z2m-device-tracker/security/advisories/new
