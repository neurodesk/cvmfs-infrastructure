#!/usr/bin/env python3
"""
Aggregate the public, k-anonymous usage statistics of every CVMFS Stratum 1
in the inventory into one fleet-wide dashboard (static site for GitHub Pages).

Inputs are each server's public /stats/stats.json (already anonymised by
roles/access_stats: continent/country only, >= k distinct clients per group).
Summing k-anonymous groups keeps them k-anonymous, so no privacy property is
lost here. Fetched JSON is treated as untrusted: every value is type-checked
and every string is HTML-escaped on output.

Usage:
  aggregate.py --inventory inventory/hosts.yml --out site
Per-host inventory variables (optional):
  stats_label  display name (default: inventory host name)
  stats_url    default: http://<ansible_host>/stats/stats.json
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.request

import yaml

MAX_BYTES = 20 * 1024 * 1024
STALE_HOURS = 72
MAX_STR = 200

# Categorical slots (validated reference palette), assigned to servers in
# inventory order so a server's colour never changes when another is down.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#6250d6", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#0ca30c", "#9085e9", "#e66767"]


# ------------------------------------------------------------ inputs --

def servers_from_inventory(path):
    with open(path) as f:
        inv = yaml.safe_load(f)
    hosts = (((inv or {}).get("all") or {}).get("children") or {}) \
        .get("stratum1", {}).get("hosts") or {}
    out = []
    for name, hv in hosts.items():
        hv = hv or {}
        host = hv.get("ansible_host", name)
        out.append({
            "id": name,
            "label": str(hv.get("stats_label", name))[:MAX_STR],
            "url": hv.get("stats_url", "http://%s/stats/stats.json" % host),
        })
    return out


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cvmfs-stats-aggregator"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("response too large")
    return json.loads(data.decode("utf-8"))


def num(v):
    """Non-negative int from untrusted input."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return max(int(v), 0)
    return 0


def s(v):
    return str(v)[:MAX_STR] if v is not None else ""


def day(v):
    v = s(v)
    try:
        dt.date.fromisoformat(v)
        return v
    except ValueError:
        return None


def rows(v):
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


# ------------------------------------------------------- aggregation --

def add_into(target, key, metrics):
    t = target.setdefault(key, {})
    for k, v in metrics.items():
        t[k] = t.get(k, 0) + num(v)


def aggregate(servers, results, now):
    agg = {"daily": {}, "regions_30d": {}, "regions_365d": {},
           "continents_30d": {}, "continents_365d": {},
           "tools": {}, "containers": {}}
    status = []
    first, last, k = None, None, 0
    for srv in servers:
        st = results.get(srv["id"])
        entry = {"label": srv["label"], "status": "unavailable",
                 "generated": "", "data_from": "", "opens_30d": 0,
                 "requests_30d": 0, "bytes_30d": 0}
        if isinstance(st, dict):
            gen = s(st.get("generated"))
            entry["generated"] = gen
            entry["data_from"] = day(st.get("data_from")) or ""
            entry["status"] = "ok"
            try:
                g = dt.datetime.strptime(gen, "%Y-%m-%d %H:%M UTC") \
                    .replace(tzinfo=dt.timezone.utc)
                if now - g > dt.timedelta(hours=STALE_HOURS):
                    entry["status"] = "stale"
            except ValueError:
                entry["status"] = "stale"
            k = max(k, num(st.get("min_networks_per_group")))
            d_from, d_to = day(st.get("data_from")), day(st.get("data_to"))
            if d_from and (first is None or d_from < first):
                first = d_from
            if d_to and (last is None or d_to > last):
                last = d_to

            daily = rows(st.get("daily"))
            for r in daily:
                d = day(r.get("day"))
                if not d:
                    continue
                a = agg["daily"].setdefault(d, {"per_server": {}, "bytes_per_server": {},
                                                "requests": 0, "bytes": 0})
                a["per_server"][srv["id"]] = (a["per_server"].get(srv["id"], 0)
                                              + num(r.get("fetches")))
                a["bytes_per_server"][srv["id"]] = (
                    a["bytes_per_server"].get(srv["id"], 0) + num(r.get("bytes")))
                a["requests"] += num(r.get("requests"))
                a["bytes"] += num(r.get("bytes"))
            cutoff = (now.date() - dt.timedelta(days=30)).isoformat()
            for r in daily:
                if (day(r.get("day")) or "") >= cutoff:
                    entry["opens_30d"] += num(r.get("fetches"))
                    entry["requests_30d"] += num(r.get("requests"))
                    entry["bytes_30d"] += num(r.get("bytes"))

            for w in ("30d", "365d"):
                for r in rows(st.get("regions_" + w)):
                    add_into(agg["regions_" + w],
                             (s(r.get("region")), s(r.get("continent"))),
                             {m: r.get(m) for m in
                              ("networks", "fetches", "requests", "bytes")})
                for r in rows(st.get("continents_" + w)):
                    add_into(agg["continents_" + w], s(r.get("continent")),
                             {m: r.get(m) for m in
                              ("networks", "requests", "bytes")})
            for r in rows(st.get("tools")):
                t = agg["tools"].setdefault(s(r.get("tool")),
                                            {"versions": 0, "fetches_30d_per_server": {}})
                t["versions"] = max(t["versions"], num(r.get("versions")))
                ps = t["fetches_30d_per_server"]
                ps[srv["id"]] = ps.get(srv["id"], 0) + num(r.get("fetches_30d"))
                add_into(agg["tools"], s(r.get("tool")),
                         {m: r.get(m) for m in ("fetches_30d", "networks_30d",
                                                "fetches_365d", "networks_365d")})
            for r in rows(st.get("containers")):
                key = s(r.get("container"))
                c = agg["containers"].setdefault(
                    key, {"tool": s(r.get("tool")), "version": s(r.get("version")),
                          "build": s(r.get("build")), "continents_365d": {}})
                add_into(agg["containers"], key,
                         {m: r.get(m) for m in ("fetches_30d", "fetches_365d",
                                                "networks_30d", "networks_365d")})
                conts = r.get("continents_365d")
                if isinstance(conts, dict):
                    for ck, cv in conts.items():
                        c["continents_365d"][s(ck)] = (
                            c["continents_365d"].get(s(ck), 0) + num(cv))
        status.append(entry)

    def region_list(d):
        out = [{"region": rk[0], "continent": rk[1], **m} for rk, m in d.items()]
        # suppressed buckets always last, then by requests
        return sorted(out, key=lambda r: (r["region"].startswith("Undisclosed"),
                                          -r.get("requests", 0)))

    return {
        "generated": now.strftime("%Y-%m-%d %H:%M UTC"),
        "data_from": first, "data_to": last,
        "min_networks_per_group": k,
        "servers": status,
        "daily": [{"day": d, "fetches_per_server": v["per_server"],
                   "bytes_per_server": v["bytes_per_server"],
                   "fetches": sum(v["per_server"].values()),
                   "requests": v["requests"], "bytes": v["bytes"]}
                  for d, v in sorted(agg["daily"].items())],
        "continents_30d": sorted(({"continent": c, **m} for c, m in
                                  agg["continents_30d"].items()),
                                 key=lambda r: -r.get("requests", 0)),
        "continents_365d": sorted(({"continent": c, **m} for c, m in
                                   agg["continents_365d"].items()),
                                  key=lambda r: -r.get("requests", 0)),
        "regions_30d": region_list(agg["regions_30d"]),
        "regions_365d": region_list(agg["regions_365d"]),
        "tools": sorted(({"tool": t, **m} for t, m in agg["tools"].items()),
                        key=lambda r: (-r.get("fetches_30d", 0),
                                       -r.get("fetches_365d", 0))),
        "containers": sorted(({"container": c, **m} for c, m in
                              agg["containers"].items()),
                             key=lambda r: (-r.get("fetches_30d", 0),
                                            -r.get("fetches_365d", 0))),
    }


# --------------------------------------------------------------- html --

def e(v):
    return html.escape(str(v), quote=True)


def fmt_n(n):
    return "{:,}".format(int(n)).replace(",", " ")


def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000:
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1000.0
    return "%.1f EB" % n


def nice_max(v):
    if v <= 0:
        return 1
    import math
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * p:
            return m * p
    return 10 * p


def table(headers, body, cls=""):
    th = "".join("<th scope='col'%s>%s</th>" % (" class='num'" if n else "", e(h))
                 for h, n in headers)
    tr = "".join("<tr>%s</tr>" % "".join(
        "<td%s>%s</td>" % (" class='num'" if n else "", v)
        for v, (_, n) in zip(r, headers)) for r in body)
    return ("<div class='tw'><table class='%s'><thead><tr>%s</tr></thead>"
            "<tbody>%s</tbody></table></div>" % (cls, th, tr))


def column_chart(days, series, value_of, fmt, label, chart_id):
    """Columns over time; stacked when len(series) > 1. series: [(id, label, slot)]"""
    if not days:
        return "<p class='muted'>No data yet.</p>"
    W, H, L, B, T = 960, 240, 56, 26, 10
    totals = [sum(value_of(d, sid) for sid, _, _ in series) for d in days]
    ymax = nice_max(max(totals) or 1)
    n = len(days)
    step = (W - L) / n
    bw = max(step - 2, 1)          # 2px surface gap between adjacent columns
    ph = H - B - T
    out = []
    for i in range(5):              # recessive grid + y labels
        v = ymax * i / 4
        y = T + ph - ph * i / 4
        out.append("<line x1='%d' x2='%d' y1='%.1f' y2='%.1f' class='grid'/>"
                   % (L, W, y, y))
        out.append("<text x='%d' y='%.1f' class='ax' text-anchor='end'>%s</text>"
                   % (L - 6, y + 4, e(fmt(v))))
    for i, d in enumerate(days):
        x = L + i * step + 1
        y0 = T + ph
        segs = []
        tip = ["<b>%s</b>" % e(d["day"])]
        for sid, slabel, slot in series:
            v = value_of(d, sid)
            if len(series) > 1:
                tip.append("<span class='sw s%d'></span>%s: %s" % (slot, e(slabel), e(fmt(v))))
            if v <= 0:
                continue
            h = ph * v / ymax
            gap = 2 if segs else 0     # 2px surface gap between stacked segments
            segs.append("<rect class='s%d' x='%.1f' y='%.1f' width='%.1f' "
                        "height='%.1f' rx='1.5'/>"
                        % (slot, x, y0 - h, bw, max(h - gap, 0.5)))
            y0 -= h
        tip.append(("Total: %s" if len(series) > 1 else "%s") % e(fmt(totals[i])))
        out.append("<g class='col' tabindex='0' data-tip=\"%s\">"
                   "<rect class='hit' x='%.1f' y='%d' width='%.1f' height='%d'/>%s</g>"
                   % (e("<br>".join(tip)), L + i * step, T, step, ph, "".join(segs)))
    # sparse x labels: first, middle, last
    for i in sorted({0, n // 2, n - 1}):
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        x = L + i * step + (0 if i == 0 else step if i == n - 1 else step / 2)
        out.append("<text x='%.1f' y='%d' class='ax' text-anchor='%s'>%s</text>"
                   % (x, H - 6, anchor, e(days[i]["day"])))
    out.append("<line x1='%d' x2='%d' y1='%d' y2='%d' class='base'/>"
               % (L, W, T + ph, T + ph))
    return ("<svg id='%s' class='chart' viewBox='0 0 %d %d' role='img' "
            "aria-label='%s'>%s</svg>" % (chart_id, W, H, e(label), "".join(out)))


def hbar_chart(items, fmt, label):
    """Horizontal bars, single series, as HTML so text stays legible on phones.
    items: [(label, value)]"""
    if not items:
        return "<p class='muted'>No data yet.</p>"
    vmax = max(v for _, v in items) or 1
    out = []
    for lab, v in items:
        out.append(
            "<div class='hb col' tabindex='0' data-tip=\"%s\">"
            "<span class='hl'>%s</span>"
            "<span class='ht'><span class='hf' style='width:%.2f%%'></span></span>"
            "<span class='hv'>%s</span></div>"
            % (e("<b>%s</b><br>%s" % (e(lab), e(fmt(v)))), e(lab),
               max(100.0 * v / vmax, 0.4), e(fmt(v))))
    return ("<div class='hbars' role='list' aria-label='%s'>%s</div>"
            % (e(label), "".join(out)))


def stacked_hbar_chart(items, series, fmt, label):
    """Horizontal bars stacked by series (same slots/legend as the column charts).
    items: [(label, {series_id: value})]; series: [(id, label, slot)]"""
    if not items:
        return "<p class='muted'>No data yet.</p>"
    totals = [sum(vals.get(sid, 0) for sid, _, _ in series) for _, vals in items]
    vmax = max(totals) or 1
    out = []
    for (lab, vals), tot in zip(items, totals):
        segs, tip = [], ["<b>%s</b>" % e(lab)]
        for sid, slabel, slot in series:
            v = vals.get(sid, 0)
            if v <= 0:
                continue
            tip.append("<span class='sw s%d'></span>%s: %s (%.0f%%)"
                       % (slot, e(slabel), e(fmt(v)), 100.0 * v / (tot or 1)))
            segs.append("<span class='hs s%d' style='flex-grow:%d'></span>" % (slot, v))
        tip.append("Total: %s" % e(fmt(tot)))
        out.append(
            "<div class='hb col' tabindex='0' data-tip=\"%s\">"
            "<span class='hl'>%s</span>"
            "<span class='ht'><span class='hf hstack' style='width:%.2f%%'>%s</span></span>"
            "<span class='hv'>%s</span></div>"
            % (e("<br>".join(tip)), e(lab), max(100.0 * tot / vmax, 0.4),
               "".join(segs), e(fmt(tot))))
    return ("<div class='hbars' role='list' aria-label='%s'>%s</div>"
            % (e(label), "".join(out)))


def render(a, servers):
    k = a["min_networks_per_group"] or 3
    days = a["daily"][-90:]
    sid_slot = {srv["id"]: i % len(SERIES_LIGHT) for i, srv in enumerate(servers)}
    series = [(srv["id"], srv["label"], sid_slot[srv["id"]]) for srv in servers
              if any(d["fetches_per_server"].get(srv["id"]) for d in days)]
    bseries = [(srv["id"], srv["label"], sid_slot[srv["id"]]) for srv in servers
               if any(d.get("bytes_per_server", {}).get(srv["id"]) for d in days)]
    all_series = [(srv["id"], srv["label"], sid_slot[srv["id"]]) for srv in servers]
    last30 = a["daily"][-30:]
    tiles = [
        (fmt_n(sum(d["fetches"] for d in last30)), "container opens, last 30 days"),
        (fmt_n(sum(1 for t in a["tools"] if t.get("fetches_30d"))), "tools used, last 30 days"),
        (fmt_bytes(sum(d["bytes"] for d in last30)), "data served, last 30 days"),
        ("%d / %d" % (sum(1 for x in a["servers"] if x["status"] == "ok"),
                      len(a["servers"])), "servers reporting"),
    ]

    legend = "".join("<span class='li'><span class='sw s%d'></span>%s</span>"
                     % (slot, e(lab)) for _, lab, slot in series)
    opens_chart = column_chart(
        days, series, lambda d, sid: d["fetches_per_server"].get(sid, 0),
        lambda v: fmt_n(v), "Container opens per day, stacked by server", "c-opens")
    opens_table = table(
        [("Day", False)] + [(lab, True) for _, lab, _ in series] + [("Total", True)],
        [[e(d["day"])] + [fmt_n(d["fetches_per_server"].get(sid, 0)) for sid, _, _ in series]
         + [fmt_n(d["fetches"])] for d in reversed(days)])
    blegend = "".join("<span class='li'><span class='sw s%d'></span>%s</span>"
                      % (slot, e(lab)) for _, lab, slot in bseries)
    bytes_chart = column_chart(
        days, bseries, lambda d, sid: d.get("bytes_per_server", {}).get(sid, 0),
        fmt_bytes, "Data served per day, stacked by server", "c-bytes")
    bytes_table = table(
        [("Day", False)] + [(lab, True) for _, lab, _ in bseries]
        + [("Total", True), ("Requests", True)],
        [[e(d["day"])] + [e(fmt_bytes(d.get("bytes_per_server", {}).get(sid, 0)))
                          for sid, _, _ in bseries]
         + [e(fmt_bytes(d["bytes"])), fmt_n(d["requests"])] for d in reversed(days)])
    top = [t for t in a["tools"][:15] if t.get("fetches_30d")]
    tseries = [x for x in all_series
               if any(t.get("fetches_30d_per_server", {}).get(x[0]) for t in top)]
    tlegend = "".join("<span class='li'><span class='sw s%d'></span>%s</span>"
                      % (slot, e(lab)) for _, lab, slot in tseries)
    tools_chart = stacked_hbar_chart(
        [(t["tool"], t.get("fetches_30d_per_server", {})) for t in top], tseries, fmt_n,
        "Top tools by container opens in the last 30 days, stacked by server")
    tools_split_table = table(
        [("Tool", False)] + [(lab, True) for _, lab, _ in tseries] + [("Total", True)],
        [[e(t["tool"])] + [fmt_n(t.get("fetches_30d_per_server", {}).get(sid, 0))
                           for sid, _, _ in tseries] + [fmt_n(t.get("fetches_30d", 0))]
         for t in top])
    cont_bars = [(c["continent"], c.get("bytes", 0)) for c in a["continents_30d"]]

    srv_rows = []
    for x in a["servers"]:
        badge = {"ok": "&#10003; reporting", "stale": "&#9888; stale",
                 "unavailable": "&#10007; unavailable"}[x["status"]]
        srv_rows.append([e(x["label"]), "<span class='st %s'>%s</span>" % (x["status"], badge),
                         e(x["generated"] or "–"), e(x["data_from"] or "–"),
                         fmt_n(x["opens_30d"]), fmt_n(x["requests_30d"]),
                         e(fmt_bytes(x["bytes_30d"]))])

    def region_rows(rs):
        return [[e(r["region"]), e(r["continent"]), fmt_n(r.get("networks", 0)),
                 fmt_n(r.get("fetches", 0)), fmt_n(r.get("requests", 0)),
                 e(fmt_bytes(r.get("bytes", 0)))] for r in rs]
    rhdr = [("Region", False), ("Continent", False), ("Clients*", True),
            ("Container opens", True), ("Requests", True), ("Data served", True)]

    tool_rows = [[e(t["tool"]), fmt_n(t.get("versions", 0)), fmt_n(t.get("fetches_30d", 0)),
                  fmt_n(t.get("networks_30d", 0)), fmt_n(t.get("fetches_365d", 0)),
                  fmt_n(t.get("networks_365d", 0))] for t in a["tools"]]
    ver_rows = []
    for c in a["containers"][:100]:
        conts = ", ".join("%s (%s)" % (e(ck), fmt_n(cv)) for ck, cv in
                          sorted(c["continents_365d"].items(), key=lambda x: -x[1]))
        ver_rows.append([e(c["tool"]), e(c["version"]), e(c["build"]),
                         fmt_n(c.get("fetches_30d", 0)), fmt_n(c.get("fetches_365d", 0)),
                         fmt_n(c.get("networks_365d", 0)), conts or "–"])

    slot_css = "".join(".s%d{fill:var(--s%d)}.sw.s%d,.hs.s%d{background:var(--s%d)}"
                       % (i, i, i, i, i) for i in range(len(SERIES_LIGHT)))
    light_vars = "".join("--s%d:%s;" % (i, c) for i, c in enumerate(SERIES_LIGHT))
    dark_vars = "".join("--s%d:%s;" % (i, c) for i, c in enumerate(SERIES_DARK))

    return TEMPLATE.format(
        css_light=light_vars, css_dark=dark_vars, slot_css=slot_css,
        generated=e(a["generated"]), dfrom=e(a["data_from"] or "–"),
        dto=e(a["data_to"] or "–"), k=k,
        tiles="".join("<div class='tile'><b>%s</b><span>%s</span></div>" % (e(v), e(t))
                      for v, t in tiles),
        legend=legend, opens_chart=opens_chart, opens_table=opens_table,
        blegend=blegend, bytes_chart=bytes_chart, bytes_table=bytes_table,
        tlegend=tlegend, tools_chart=tools_chart, tools_split_table=tools_split_table,
        cont_chart=hbar_chart(cont_bars, fmt_bytes, "Data served by continent, last 30 days"),
        servers=table([("Server", False), ("Status", False), ("Last report", False),
                       ("Data since", False), ("Opens 30d", True), ("Requests 30d", True),
                       ("Data 30d", True)], srv_rows),
        r30=table(rhdr, region_rows(a["regions_30d"])),
        r365=table(rhdr, region_rows(a["regions_365d"])),
        tools=table([("Tool", False), ("Versions", True), ("Opens 30d", True),
                     ("Clients* 30d", True), ("Opens 365d", True), ("Clients* 365d", True)],
                    tool_rows, "filterable"),
        vers=table([("Tool", False), ("Version", False), ("Build", False),
                    ("Opens 30d", True), ("Opens 365d", True), ("Clients* 365d", True),
                    ("Continents (clients*, 365d)", False)], ver_rows),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neurodesk CVMFS Usage</title>
<meta name="description" content="Fleet-wide, privacy-preserving usage statistics of the Neurodesk CVMFS container repository.">
<style>
:root{{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--fg:#0b0b0b;--fg2:#52514e;--muted:#6f6e69;--line:#e6e5e0;--grid:#efeee9;{css_light}--ok:#008300;--warn:#a86b00;--bad:#c62f2e}}
@media (prefers-color-scheme:dark){{:root:where(:not([data-theme="light"])){{color-scheme:dark;--bg:#141413;--card:#1a1a19;--fg:#f0efec;--fg2:#c3c2b7;--muted:#9a998f;--line:#2e2e2b;--grid:#252523;{css_dark}--ok:#0ca30c;--warn:#e0a020;--bad:#e66767}}}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#141413;--card:#1a1a19;--fg:#f0efec;--fg2:#c3c2b7;--muted:#9a998f;--line:#2e2e2b;--grid:#252523;{css_dark}--ok:#0ca30c;--warn:#e0a020;--bad:#e66767}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:1120px;margin:0 auto;padding:28px 16px 64px}}
h1{{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}}h2{{font-size:18px;margin:40px 0 4px}}
.sub,.muted,.note{{color:var(--fg2)}}.sub{{margin:0 0 22px}}.note{{font-size:13px;margin:0 0 10px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}}
.tile b{{display:block;font-size:26px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}}
.tile span{{color:var(--fg2);font-size:13px}}
.chart{{width:100%;height:auto;display:block;overflow:visible}}
.chart .grid{{stroke:var(--grid);stroke-width:1}}.chart .base{{stroke:var(--line);stroke-width:1}}
.chart .ax{{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}}
.chart .lab{{fill:var(--fg);font-size:13px}}.chart .val{{fill:var(--fg2);font-size:12px;font-variant-numeric:tabular-nums}}
.chart .hit{{fill:transparent}}.chart .col:hover .hit,.chart .col:focus .hit{{fill:var(--grid)}}
.chart .col:focus{{outline:none}}.chart .col:focus-visible .hit{{stroke:var(--fg2);stroke-width:1}}
{slot_css}
.legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin:6px 0 10px;font-size:13px;color:var(--fg2)}}
.li{{display:inline-flex;align-items:center;gap:6px}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}}
#tip{{position:fixed;pointer-events:none;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px;line-height:1.45;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:10;max-width:280px;font-variant-numeric:tabular-nums}}
.tw{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-weight:600;color:var(--fg2);font-size:12px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}}
tr:last-child td{{border-bottom:0}}.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
details{{margin-top:8px}}summary{{cursor:pointer;color:var(--fg2);font-size:13px}}details .tw{{margin-top:8px;max-height:420px;overflow:auto}}
input{{width:100%;max-width:320px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;margin-bottom:10px}}
.st.ok{{color:var(--ok)}}.st.stale{{color:var(--warn)}}.st.unavailable{{color:var(--bad)}}
.hbars{{display:grid;gap:4px}}.hb{{display:grid;grid-template-columns:minmax(90px,190px) 1fr auto;align-items:center;gap:10px;padding:2px 4px;border-radius:6px}}.hb:hover,.hb:focus-visible{{background:var(--grid);outline:none}}.hl{{text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}}.ht{{height:16px;display:block}}.hf{{display:block;height:100%;background:var(--s0);border-radius:0 4px 4px 0}}.hstack{{display:flex;gap:2px;background:none}}.hs{{display:block;height:100%;flex-basis:0;min-width:0}}.hs:first-child{{border-radius:0}}.hs:last-child{{border-radius:0 4px 4px 0}}.hv{{font-size:12px;color:var(--fg2);font-variant-numeric:tabular-nums;white-space:nowrap}}.cw{{overflow-x:auto}}.cw .chart{{min-width:640px}}
footer{{margin-top:44px;font-size:13px;color:var(--fg2)}}a{{color:var(--s0)}}
</style></head><body><main>
<h1>Neurodesk CVMFS usage</h1>
<p class="sub">All Stratum&nbsp;1 servers of <code>neurodesk.ardc.edu.au</code> &middot; data {dfrom} &ndash; {dto} &middot; updated {generated}</p>

<div class="tiles">{tiles}</div>

<h2>Container opens per day</h2>
<p class="note">Last 90 days, stacked by the Stratum&nbsp;1 server that served them.</p>
<div class="card"><div class="legend">{legend}</div><div class="cw">{opens_chart}</div>
<details><summary>Show data table</summary>{opens_table}</details></div>

<h2>Data served per day</h2>
<p class="note">Last 90 days, all object types, stacked by the Stratum&nbsp;1 server that served them.</p>
<div class="card"><div class="legend">{blegend}</div><div class="cw">{bytes_chart}</div><details><summary>Show data table</summary>{bytes_table}</details></div>

<h2>Most used tools</h2>
<p class="note">Container opens in the last 30 days, top 15, split by the Stratum&nbsp;1 server that served them.
Clients normally use the server closest to them, so the split shows roughly where each tool is used.</p>
<div class="card"><div class="legend">{tlegend}</div>{tools_chart}
<details><summary>Show data table</summary>{tools_split_table}</details></div>

<h2>By continent</h2>
<p class="note">Data served in the last 30 days.</p>
<div class="card">{cont_chart}</div>

<h2>By region</h2>
<p class="note">Last 30 days. Regions with fewer than {k} distinct clients on a server are merged into coarser groups.</p>
{r30}
<details><summary>Last 365 days</summary>{r365}</details>

<h2>Tools</h2>
<input id="q" type="search" placeholder="Filter tools&hellip;" aria-label="Filter tools">
{tools}

<h2>Top container versions</h2>
{vers}
<p class="note">All versions: <a href="stats.json">stats.json</a></p>

<h2>Servers</h2>
{servers}

<h2>Methodology &amp; privacy</h2>
<div class="card">
<p><b>Container opens</b> count downloads of a container's file catalog from a Stratum&nbsp;1. Site caches
keep catalogs, so one open usually stands for many runs at a site: the numbers show reach, not executions.</p>
<p><b>Clients*</b> are distinct client addresses as seen by each server (often site proxies or short-lived
cloud/CI machines, not individual users), summed across servers &mdash; an upper bound, since a client
may use more than one server.</p>
<p><b>Privacy:</b> servers never store or publish IP addresses, host names or cities. Each server publishes only
continent/country aggregates in which every group has at least {k} distinct clients; this dashboard only sums
those published files.</p>
</div>
<footer>Generated by <code>aggregate/aggregate.py</code> in neurodesk/cvmfs-infrastructure.
<a href="https://db-ip.com">IP Geolocation by DB-IP</a> (CC BY 4.0).</footer>
</main>
<div id="tip" role="tooltip"></div>
<script>
(function(){{
var tip=document.getElementById('tip');
function show(g,x,y){{tip.innerHTML=g.getAttribute('data-tip');tip.style.display='block';
var r=tip.getBoundingClientRect(),px=x+14,py=y+14;
if(px+r.width>innerWidth-8)px=x-r.width-14;if(py+r.height>innerHeight-8)py=y-r.height-14;
tip.style.left=Math.max(8,px)+'px';tip.style.top=Math.max(8,py)+'px';}}
document.querySelectorAll('.chart .col, .hbars .col').forEach(function(g){{
g.addEventListener('mousemove',function(ev){{show(g,ev.clientX,ev.clientY);}});
g.addEventListener('mouseleave',function(){{tip.style.display='none';}});
g.addEventListener('focus',function(){{var b=g.getBoundingClientRect();show(g,b.left+b.width/2,b.top);}});
g.addEventListener('blur',function(){{tip.style.display='none';}});}});
var q=document.getElementById('q'),t=document.querySelector('table.filterable');
if(q&&t){{var rows=t.querySelectorAll('tbody tr');q.addEventListener('input',function(){{
var v=q.value.toLowerCase();rows.forEach(function(r){{r.style.display=r.cells[0].textContent.toLowerCase().indexOf(v)>=0?'':'none';}});}});}}
}})();
</script>
</body></html>
"""


# --------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="inventory/hosts.yml")
    ap.add_argument("--out", default="site")
    ap.add_argument("--require", type=int, default=1,
                    help="fail if fewer servers than this report (default 1)")
    args = ap.parse_args()

    servers = servers_from_inventory(args.inventory)
    if not servers:
        sys.exit("no stratum1 hosts in inventory")
    results = {}
    for srv in servers:
        try:
            results[srv["id"]] = fetch(srv["url"])
            print("ok           %s" % srv["label"])
        except Exception as ex:            # one broken server must not stop the rest
            print("unavailable  %s (%s)" % (srv["label"], type(ex).__name__))
    if len(results) < args.require:
        sys.exit("only %d server(s) reported, %d required" % (len(results), args.require))

    now = dt.datetime.now(dt.timezone.utc)
    agg = aggregate(servers, results, now)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "stats.json"), "w") as f:
        json.dump(agg, f, indent=1, ensure_ascii=False)
    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(render(agg, servers))
    open(os.path.join(args.out, ".nojekyll"), "w").close()
    print("wrote %s (%d servers reporting)" % (args.out, len(results)))


if __name__ == "__main__":
    main()
