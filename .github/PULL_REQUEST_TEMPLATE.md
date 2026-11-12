## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## Why

<!-- The problem being solved. Link the issue if there is one: Fixes #123 -->

## How it was verified

<!--
Delete what does not apply. "Lint passes" is not verification on its own. The app and the
integration run inside Home Assistant, so say what you actually looked at.
-->

- [ ] `ruff check` and `ruff format --check` pass
- [ ] App: rebuilt and started on a Home Assistant with Zigbee2MQTT, and the sidebar page loads
- [ ] Integration / card: tried in a real Home Assistant, sensors update and the card renders
- [ ] Checked the app log and the browser console for errors
- [ ] New app option added to `options:` and `schema:` in `config.yaml`, and to `DOCS.md`

## Anything to watch

<!-- Changes to stored history, new minimum HA version, docs that need updating. -->
