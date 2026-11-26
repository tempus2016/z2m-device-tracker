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
    "card.community.title": "Community ratings",
    "card.community.avoid": "Avoid",
    "card.community.mixed": "Mixed",
    "card.community.reports": { one: "{n} report", other: "{n} reports" },
    "card.community.you_have": { one: "you have {n}", other: "you have {n}" },
    "card.community.recommended": { one: "{n} of your models is recommended", other: "{n} of your models are recommended" },
    "card.community.none": "None of your models has enough ratings yet.",
    "card.editor.show_community": "Show community ratings",
    "card.editor.show_community_help": "Lists your models that other users rate Avoid or Mixed.",
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
  // Some messages need a value worked out first (a duration, a list of names). The rules below are
  // the sidebar's (ISSUE_VARS in the app's index.html): keep the two in step. A message this file
  // has no rule for, or a language file without one of the small pieces, shows the app's English
  // text rather than a half-filled sentence.
  let incomplete = false;
  const piece = (key, vars) => { if (!has(key) && !Object.hasOwn(EN, key)) { incomplete = true; return ""; } return t(key, vars); };
  const num1 = (n) => intl((l) => new Intl.NumberFormat(l, { maximumFractionDigits: 1 }).format(n));
  const day = (iso) => { const d = new Date(`${iso}T00:00:00`); return Number.isNaN(d.getTime()) ? String(iso ?? "") : intl((l) => d.toLocaleDateString(l, { day: "numeric", month: "short", year: "numeric" })); };
  const names = (p) => list([...(p.names || []), ...(p.more ? [piece("issue.router_offline.more", { n: p.more })] : [])]);
  const unit = (u) => (u === "%" ? "%" : ` ${u}`);
  const suggested = (p) => { p.extra = p.suggested ? piece("issue.naming.suggested", { name: p.suggested }) : ""; };
  const ISSUE_VARS = {
    flapping: (p) => { p.what = list([p.outages ? piece("issue.flapping.outages", { n: p.outages }) : null, p.leaves ? piece("issue.flapping.leaves", { n: p.leaves }) : null].filter(Boolean)); },
    single_point: (p) => { p.extra = p.devices ? piece("issue.single_point.extra", { n: p.devices }) : ""; },
    late: (p) => { p.every = span(p.every); },
    late_learned: (p) => { p.every = span(p.every); p.longest = span(p.longest); },
    lqi_stuck: (p) => { p.seconds = span(p.seconds); },
    impossible_reading: (p) => { p.value = num1(p.value); p.lo = num1(p.lo); p.hi = num1(p.hi); p.unit = unit(p.unit); },
    impossible_reading_no_range: (p) => { p.value = num1(p.value); p.unit = unit(p.unit); },
    impossible_battery: (p) => { p.value = num1(p.value); },
    humidity_stuck: (p) => { p.cause = piece(p.value === 100 ? "issue.humidity_stuck.cause_wet" : "issue.humidity_stuck.cause_dry"); p.value = num1(p.value); },
    payload_changed: (p) => { p.lead = p.firmware ? piece("issue.payload_changed.since", { version: p.firmware }) : ""; p.what = list([piece("issue.payload_changed.gone", { keys: p.gone }), p.new ? piece("issue.payload_changed.new", { keys: p.new }) : null].filter(Boolean)); },
    router_offline: (p) => { p.names = names(p); },
    power_loss: (p) => { p.names = names(p); },
    router_hopping: (p) => { p.quality = p.lqi_min === null || p.lqi_min === undefined ? "" : p.lqi_min === p.lqi_max ? piece("issue.router_hopping.lqi", { lqi: p.lqi_min }) : piece("issue.router_hopping.lqi_range", { min: p.lqi_min, max: p.lqi_max }); },
    pairing_open: (p) => {
      const parts = [p.origin === "frontend" || p.origin === "mqtt" ? piece(`issue.pairing_open.${p.origin}`) : null, p.device ? piece("issue.pairing_open.device", { device: p.device }) : null, p.left != null ? piece("issue.pairing_open.left", { duration: span(p.left) }) : null];
      p.extra = parts.filter(Boolean).map((x) => ` · ${x}`).join("");
    },
    warranty_ending: (p) => { p.date = day(p.date); },
    warranty_ending_today: (p) => { p.date = day(p.date); },
    signal_dip: (p) => { p.period = has(`issue.signal_dip.${p.period}`) ? t(`issue.signal_dip.${p.period}`) : p.period; },
    naming_ieee: suggested,
    naming_pattern: suggested,
  };
  const issueVars = (i) => {
    const p = { ...(i.params || {}) };
    if (p.seconds !== undefined) p.duration = span(p.seconds);
    if (p.since) p.since = date(p.since);
    if (p.reading) p.reading = t(`reading.${p.reading}`, null, cap(String(p.reading).replace(/_/g, " ")));
    ISSUE_VARS[i.msg]?.(p);
    return p;
  };
  const issueText = (i, part, english) => {
    const key = `issue.${i.msg}.${part}`;
    if (!i.msg || !has(key)) return english;
    incomplete = false;
    let out;
    try { out = t(key, issueVars(i)); } catch (e) { return english; }
    return incomplete || /\{\w+\}/.test(out) ? english : out;
  };
  const issueLabel = (i) => (has(`issue.label.${i.kind}`) ? t(`issue.label.${i.kind}`) : i.label);
  const issueTitle = (i) => issueText(i, "title", i.title);
  const issueDetail = (i) => issueText(i, "detail", i.detail);

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
