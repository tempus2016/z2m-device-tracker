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

## Translating the sidebar

The sidebar's text lives in `z2m_tracker/app/www/i18n/en.json`: flat keys grouped by area
(`tabs.devices`, `health.title`, `issue.offline.detail`, ...). To add a language:

1. Copy `en.json` to `xx.json`, where `xx` is the language code Home Assistant uses (`de`,
   `fr`, `pt-BR`, `zh-Hans`). The file name must match `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`.
2. Set `language.name` to the language's own name (`Deutsch`), then translate the values.
   Keep every `{placeholder}` as it is. You can move it within the sentence.
3. A value can be an object of plural forms, picked with `Intl.PluralRules` from `{n}`:
   `{"one": "{n} device", "other": "{n} devices"}`. Use the categories your language needs
   (`zero`, `one`, `two`, `few`, `many`, `other`). `other` is required.
4. Leave out any key you don't want to translate. Missing keys fall back to English.

The app lists every file in `www/i18n/` automatically. Settings, Language picks it, and
Automatic follows the Home Assistant user's profile language. Dates, times and "2 days ago"
come from the browser's `Intl` support, so they need no strings.

Home Assistant notifications, the weekly summary and alert events stay in English. So do names
and data that come from Zigbee2MQTT, such as models, binding clusters and history sources.

### Adding new UI text

Don't write English straight into `index.html`. Add a key to `en.json` and use one of these:

- `t("key", { name })` returns plain text. Use it for `textContent`, snackbars and confirm
  titles. Wrap it in `esc()` for attributes: `aria-label="${esc(t("common.close"))}"`.
- `th("key", { name: esc(name) })` returns HTML. It escapes the translation, but inserts
  `{vars}` as they are. That lets you pass links or `<b>`, so escape user data yourself.
- `tx("key", vars, fallback)` uses the server's English text when the key doesn't exist.
- Static markup uses `data-t="key"` for text and `data-ta="aria-label=key;title=key"` for attributes.

Text built in Python and shown in the sidebar (health checks, radio advice) also sends
`msg` and `params`. The sidebar looks up `issue.<msg>.title`, `issue.<msg>.detail` or
`radio.finding.<msg>`, and falls back to the English `title`, `detail` or `text`. A new check
should pass a `msg` and its params to `add()` in `health.py`, and add both keys to `en.json`.

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
