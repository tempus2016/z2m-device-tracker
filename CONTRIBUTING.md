# Contributing

Thanks for looking. This page is the short version of what makes a change easy to accept.

## How the repository is laid out

There are two things in here, and they install separately:

- **The app** in `z2m_tracker/`. Python with no framework: `app/main.py` runs the MQTT client
  and the web server, `app/tracker.py` keeps the history, `app/health.py` decides what needs
  attention, and `app/www/index.html` is the whole sidebar page. Its Python packages are
  pinned in `z2m_tracker/Dockerfile`, and its base image in `z2m_tracker/build.yaml`.
- **The integration** in `custom_components/z2m_tracker/`, which reads the app and turns it
  into sensors, plus the dashboard cards in `www/`. Plain JavaScript, no
  build step.

## Checks

```bash
pip install ruff
ruff check custom_components z2m_tracker tests
ruff format --check custom_components z2m_tracker tests
```

The settings are in `pyproject.toml`. CI also checks the app definition with the add-on
linter, builds the app image for both architectures, and runs hassfest and HACS validation
against the integration.

## Tests

The app's logic has a `pytest` suite in `tests/`: the health checks, the tracker's handling
of Zigbee2MQTT messages, saving and loading history (including files from older versions),
the history import, the battery library and the metrics. It needs Python 3.13 and the
packages pinned in `z2m_tracker/Dockerfile`:

```bash
pip install pytest paho-mqtt==2.1.0 PyYAML==6.0.2 websocket-client==1.8.0
python -m pytest
```

Or, with [uv](https://docs.astral.sh/uv/), in one line:

```bash
uv run --no-project --python 3.13 --with pytest --with paho-mqtt==2.1.0 --with PyYAML==6.0.2 --with websocket-client==1.8.0 pytest
```

The tests use a fake clock rather than sleeping, and small made-up files in `tests/fixtures/`.
A change to a health check or to what the tracker stores should come with a test. Anything
that can be a pure function, such as a health check or a piece of history bookkeeping, should
be, so it can be tested without MQTT.

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

## Translations

The app and the integration ship in English, German, Dutch, French, Spanish, Italian, Polish,
Brazilian Portuguese, Swedish, Czech, Russian and Simplified Chinese. A language is three files,
each copied from the English one next to it:

| What | English source |
|---|---|
| The sidebar and the dashboard cards | `z2m_tracker/app/www/i18n/en.json` |
| The app's options in Home Assistant | `z2m_tracker/translations/en.yaml` |
| The integration: setup, options, actions, entities | `custom_components/z2m_tracker/translations/en.json` |

Name each copy with the language code Home Assistant uses (`de`, `fr`, `pt-BR`, `zh-Hans`).
The code must match `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`. In the sidebar file:

1. Set `language.name` to the language's own name (`Deutsch`), then translate the values.
   Keep every `{placeholder}` as it is. You can move it within the sentence.
2. A value can be an object of plural forms, picked with `Intl.PluralRules` from `{n}`:
   `{"one": "{n} device", "other": "{n} devices"}`. Use the categories your language needs
   (`zero`, `one`, `two`, `few`, `many`, `other`). `other` is required.
3. Leave out any key you don't want to translate. Missing keys fall back to English.

The app lists every file in `www/i18n/` automatically. Settings, Language picks it, and
Automatic follows the Home Assistant user's profile language. The dashboard cards ask the
integration for the `card.*`, `issue.*`, `unit.*`, `reading.*` and `net.*` keys in the user's
language, which reads them from the app, so the cards and the sidebar share one file. Dates,
times and "2 days ago" come from the browser's `Intl` support, so they need no strings.

`tests/test_translations.py` checks every file against English: no unknown keys, the same
placeholders, and valid plural forms. A missing key is fine.

Home Assistant notifications, the weekly summary and alert events stay in English. So do names
and data that come from Zigbee2MQTT, such as models, binding clusters and history sources, and
the names of the sensors the app adds to each device through MQTT discovery.

### Adding new UI text

Don't write English straight into `index.html` or the cards. Add a key to `en.json` and use one of these:

- `t("key", { name })` returns plain text. Use it for `textContent`, snackbars and confirm
  titles. Wrap it in `esc()` for attributes: `aria-label="${esc(t("common.close"))}"`.
- `th("key", { name: esc(name) })` returns HTML. It escapes the translation, but inserts
  `{vars}` as they are. That lets you pass links or `<b>`, so escape user data yourself.
- `tx("key", vars, fallback)` uses the server's English text when the key doesn't exist.
- Static markup uses `data-t="key"` for text and `data-ta="aria-label=key;title=key"` for attributes.
- In the cards, `T("card.key", vars)` returns plain text; wrap it in `esc()`. A new card key also
  goes in the built-in English list in `custom_components/z2m_tracker/www/z2m-tracker-i18n.js`.

New keys only need adding to `en.json`. The other languages show English for them until
someone translates them.

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
