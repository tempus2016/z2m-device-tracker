// /config/custom_components/z2m_tracker/www/z2m-tracker-i18n.js
// Translations for the Zigbee Tracker cards. The strings come from the app, the same files the
// sidebar uses, through the z2m_tracker/strings websocket command. English is built in, so the
// cards still work with an older app or while the app is unreachable.
(() => {
  if (window.Z2MTrackerI18n) return;

  const EN = {
    "card.unreachable": "The Zigbee Tracker app isn't reachable",
    "card.loading": "Loading…",
    "card.more": "and {n} more",
    "card.nothing": "Nothing needs attention.",
    "card.summary.title": "Zigbee devices",
    "card.summary.active": "Active",
    "card.summary.offline": "Offline",
    "card.summary.attention": "Need attention",
    "card.summary.batteries_due": "Batteries due",
    "card.summary.needs_attention": "Needs attention",
    "card.summary.to_buy": "Batteries to buy",
    "card.summary.to_buy_for": { one: "For {n} device running low within 60 days", other: "For {n} devices running low within 60 days" },
    "card.summary.unknown_type": "Unknown type",
    "card.summary.open": "Open Zigbee Tracker",
    "card.battery.title": "Batteries",
    "card.battery.days_left": { one: "about {n} day left", other: "about {n} days left" },
    "card.battery.low": "Low",
    "card.battery.low_count": { one: "{n} low", other: "{n} low" },
    "card.battery.no_reading": "No reading yet",
    "card.battery.none": "No battery devices found.",
    "card.health.title": "Zigbee health",
    "card.health.attention": "Need attention",
    "card.health.look": "Worth a look",
    "card.health.fine": "Fine",
    "card.health.line": "{title}. {detail}",
    "card.network.title": "Zigbee network",
    "card.network.online": "Online",
    "card.network.offline": "Offline",
    "card.network.offline_count": "{n} offline",
    "card.network.devices": "Devices",
    "card.network.routers": "Routers",
    "card.network.weak_spots": { one: "Weak spot", other: "Weak spots" },
    "card.network.section": "{name} network",
    "card.network.section_offline": "{name} network · offline",
    "card.network.channel": "Channel",
    "card.network.backup": "Coordinator backup",
    "card.network.last_scan": "Last scan",
    "card.network.not_known": "Not known yet",
    "card.network.wifi_overlap": "{channel} · overlaps Wi-Fi",
    "card.network.wifi_near": "{channel} · close to Wi-Fi",
    "card.network.wifi_clear": "{channel} · clear of Wi-Fi",
    "card.network.wifi_unset": "{channel} · Wi-Fi not set",
    "card.network.backup_no_folder": "Folder not found",
    "card.network.backup_missing": "Missing",
    "card.network.backup_unreadable": "Unreadable",
    "card.network.backup_no_key": "No network key",
    "card.network.unknown_date": "Unknown date",
    "card.network.scanning": "Scanning now",
    "card.network.never": "Never",
    "card.network.today": "Today {time}",
    "card.network.yesterday": "Yesterday {time}",
    "card.network.days_ago": { one: "{n} day ago", other: "{n} days ago" },
    "card.network.single_points": "Single points of failure",
    "card.network.only_route": "Only route for {what}",
    "card.network.only_route_part": "Only route for part of the mesh",
    "card.network.routers_n": { one: "{n} router", other: "{n} routers" },
    "card.network.devices_n": { one: "{n} device", other: "{n} devices" },
    "card.network.none": "No Zigbee2MQTT network seen yet.",
    "card.editor.title": "Title",
    "card.editor.max_items": "Rows to show",
    "card.editor.show_all": "Show every battery device",
    "card.editor.show_all_help": "Ignores the row limit.",
    "card.editor.show_info": "Include \"worth a look\" items",
    "card.editor.show_info_help": "Off shows only devices that need attention.",
    "card.editor.show_weak_spots": "List single-point-of-failure routers",
    "net.main": "Main",
    "unit.minutes": "{n} min",
    "unit.hours": "{n} h",
    "unit.days": { one: "{n} day", other: "{n} days" },
  };
  const LANG_RE = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;
  const RETRY_MS = 60000;

  const S = { lang: "en", want: null, cur: {}, failedAt: 0, loading: null, listeners: new Set() };
  let plurals = new Intl.PluralRules("en");
  const intl = (make) => { try { return make(S.lang); } catch (e) { return make("en"); } };

  const lookup = (key) => (Object.hasOwn(S.cur, key) ? S.cur[key] : Object.hasOwn(EN, key) ? EN[key] : undefined);
  const pick = (v, vars) => (v && typeof v === "object" ? v[plurals.select(Number(vars?.n ?? 0))] ?? v.other ?? "" : String(v));
  const fill = (s, vars) => (vars ? s.replace(/\{(\w+)\}/g, (m, k) => (Object.hasOwn(vars, k) ? String(vars[k] ?? "") : m)) : s);
  // t(): plain text, escape it before putting it in HTML. fallback is used when no language has the key.
  const t = (key, vars, fallback) => { const v = lookup(key); return v === undefined ? (fallback ?? key) : fill(pick(v, vars), vars); };
  const has = (key) => Object.hasOwn(S.cur, key);

  const cap = (s) => (s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : s);
  const list = (items) => intl((l) => new Intl.ListFormat(l, { type: "conjunction" }).format(items.map(String)));
  const span = (s) => (s < 3600 ? t("unit.minutes", { n: Math.max(1, Math.round(s / 60)) }) : s < 172800 ? t("unit.hours", { n: Math.round(s / 3600) }) : t("unit.days", { n: Math.round(s / 86400) }));
  const date = (iso) => { const d = new Date(typeof iso === "number" ? iso * 1000 : iso); return Number.isNaN(d.getTime()) ? "" : intl((l) => d.toLocaleString(l, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" })); };
  const netLabel = (bt) => (bt === "zigbee2mqtt" ? t("net.main") : cap(String(bt).replace(/^zigbee2mqtt[_-]?/, "").replace(/[_-]/g, " ").trim()) || bt);

  // The app sends English issue text plus msg + params; the same keys as the sidebar translate it.
  const ISSUE_VARS = {
    flapping: (p) => { p.what = list([p.outages ? t("issue.flapping.outages", { n: p.outages }) : null, p.leaves ? t("issue.flapping.leaves", { n: p.leaves }) : null].filter(Boolean)); },
    single_point: (p) => { p.extra = p.devices ? t("issue.single_point.extra", { n: p.devices }) : ""; },
  };
  const issueVars = (i) => {
    const p = { ...(i.params || {}) };
    if (p.seconds !== undefined) p.duration = span(p.seconds);
    if (p.since) p.since = date(p.since);
    if (p.reading) p.reading = t(`reading.${p.reading}`, null, cap(String(p.reading).replace(/_/g, " ")));
    ISSUE_VARS[i.msg]?.(p);
    return p;
  };
  const issueLabel = (i) => (has(`issue.label.${i.kind}`) ? t(`issue.label.${i.kind}`) : i.label);
  const issueTitle = (i) => (i.msg && has(`issue.${i.msg}.title`) ? t(`issue.${i.msg}.title`, issueVars(i)) : i.title);
  const issueDetail = (i) => (i.msg && has(`issue.${i.msg}.detail`) ? t(`issue.${i.msg}.detail`, issueVars(i)) : i.detail);

  const notify = () => S.listeners.forEach((fn) => { try { fn(); } catch (e) { /* a card failed to render */ } });
  const apply = (lang, strings) => {
    S.lang = lang;
    S.cur = strings || {};
    try { plurals = new Intl.PluralRules(lang); } catch (e) { plurals = new Intl.PluralRules("en"); }
  };

  // Call from each card's hass setter. Fetches when the user's language changes, and retries a failed
  // fetch after a minute. onChange runs once the strings arrive.
  const use = (hass, onChange) => {
    if (onChange) S.listeners.add(onChange);
    const want = hass?.locale?.language || hass?.language || "en";
    const retry = S.failedAt && Date.now() - S.failedAt > RETRY_MS;
    if ((want === S.want && !retry) || S.loading) return;
    S.want = want;
    S.failedAt = 0;
    if (!LANG_RE.test(want) || want.split("-")[0].toLowerCase() === "en" || !hass?.callWS) {
      apply(LANG_RE.test(want) ? want : "en", {});
      notify();
      return;
    }
    S.loading = hass
      .callWS({ type: "z2m_tracker/strings", language: want })
      .then((r) => apply(want, r?.strings))
      .catch(() => { S.failedAt = Date.now(); apply(want, {}); })
      .finally(() => { S.loading = null; notify(); });
  };
  const forget = (onChange) => S.listeners.delete(onChange);

  window.Z2MTrackerI18n = { t, use, forget, issueLabel, issueTitle, issueDetail, netLabel, get lang() { return S.lang; } };
})();
