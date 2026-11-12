# Contributing

Thanks for looking. This page is the short version of what makes a change easy to accept.

## How the repository is laid out

There are two things in here, and they install separately:

- **The app** in `z2m_tracker/`. Python with no framework: `app/main.py` runs the MQTT client
  and the web server, `app/tracker.py` keeps the history, `app/health.py` decides what needs
  attention, and `app/www/index.html` is the whole sidebar page. Its Python packages are
  pinned in `z2m_tracker/Dockerfile`, and its base image in `z2m_tracker/build.yaml`.
- **The integration** in `custom_components/z2m_tracker/`, which reads the app and turns it
  into sensors, plus the dashboard card in `www/z2m-tracker-card.js`. Plain JavaScript, no
  build step.

## Checks

```bash
pip install ruff
ruff check custom_components z2m_tracker
ruff format --check custom_components z2m_tracker
```

The settings are in `pyproject.toml`. CI also checks the app definition with the add-on
linter, builds the app image for both architectures, and runs hassfest and HACS validation
against the integration.

There is no test suite yet. Anything that can be a pure function, such as a health check or
a piece of history bookkeeping, should be, and a pull request that adds the first `pytest`
tests for one is very welcome.

## Trying a change

- **The app** needs a Home Assistant with the Supervisor (Home Assistant OS or Supervised).
  Copy `z2m_tracker/` into the `/addons` share as a local app, then install and rebuild it from
  the app store. It needs Zigbee2MQTT and an MQTT broker to have anything to show.
- **The integration** can go into any Home Assistant's `custom_components/`, but it has
  nothing to read without the app.

## Things to keep in step

- A new app option goes in both `options:` and `schema:` in `z2m_tracker/config.yaml`, and
  in the table in `z2m_tracker/DOCS.md`.
- The app and the integration share a version number. `z2m_tracker/config.yaml` and
  `custom_components/z2m_tracker/manifest.json` change together.
- Changes go in both changelogs: `CHANGELOG.md` and `z2m_tracker/CHANGELOG.md`, the copy
  Home Assistant shows in the app store.
- Stored history has to keep loading. A change to what is saved in `/data` must still read
  what the previous version wrote.

## What tends to get pushed back

- **Writing to Zigbee2MQTT without being asked.** The app watches, and changes the network
  only when somebody presses a button. Automatic renames, reconfigures or re-pairs belong in
  an automation the user owns.
- **Reaching the internet.** Everything the app needs ships in the image. A feature that
  needs an online lookup has to make the case for it.
- **Losing history.** A device that has left the network is the point of keeping its history,
  so nothing is pruned just because the device has gone.

## Reporting something

An [issue][issues] with the app version, the Zigbee2MQTT version, what you expected and what
you got is almost always enough. If the app log said anything, that is usually the answer.

[issues]: https://github.com/tempus2016/z2m-device-tracker/issues
