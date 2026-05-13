#!/usr/bin/env python3
"""Render an HTML comparison report from results.json."""
from __future__ import annotations

import html
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median


def percentile(data, p):
    if not data:
        return 0
    s = sorted(data)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def mapbox_str(mb: dict) -> tuple[str, str]:
    """Reproduce the format the rtirl-obs page uses by default:
        ${neighborhood ? neighborhood.text + ', ' : ''}${place?.text}
    Returns (neighborhood-or-best, place-or-empty)."""
    if "error" in mb:
        return ("ERROR", mb["error"][:80])
    e = mb["extract"]
    n = (e.get("neighborhood") or {}).get("text") or ""
    p = (e.get("place") or {}).get("text") or ""
    if not n:
        # Fall back to locality / district / region in that order — matches
        # what the existing page would gracefully degrade to.
        for k in ("locality", "district", "region"):
            if e.get(k):
                n = e[k]["text"]
                break
    return (n, p)


def best_name(m: dict) -> str:
    """Prefer the overlay translation when present; fall back to the local
    `name` baked into the .drt."""
    return m.get("translated_name") or m.get("name", "")


# Pick a "neighborhood" the way Mapbox would — prefer named places /
# fine-grained admin areas over POI-style features (parks, museums, squares).
# Without this, browser-district happily returns "Bondi Park" instead of
# "Bondi Beach" because the park's bbox is tighter.
_NEIGHBORHOOD_PRIORITY = [
    # (predicate, label) — first match wins.
    (lambda t: t.get("place") in ("neighbourhood", "quarter", "suburb",
                                  "borough", "city_district", "district"),
     "place"),
    (lambda t: t.get("admin_level") in ("10", "9"), "admin_fine"),
    (lambda t: t.get("admin_level") in ("8", "7"), "admin_city"),
    (lambda t: t.get("leisure") in ("park", "nature_reserve") or
               t.get("boundary") in ("national_park", "protected_area"),
     "leisure"),
]


def _pick_neighborhood(matches: list) -> dict | None:
    for pred, _label in _NEIGHBORHOOD_PRIORITY:
        candidates = [m for m in matches if pred(m["tags"])]
        if candidates:
            # Within tier, smallest bbox wins (already sorted that way).
            return candidates[0]
    return matches[0] if matches else None


def _pick_city(matches: list) -> dict | None:
    for m in matches:
        lvl = m["tags"].get("admin_level")
        if lvl and lvl.isdigit() and int(lvl) <= 8:
            return m
    return None


def district_str(dr: dict) -> tuple[str, str, list]:
    """Returns (top neighborhood-ish name, city/admin context, all-matches)."""
    if "error" in dr:
        return ("ERROR", dr["error"][:80], [])
    matches = dr.get("matches", [])
    nb_m = _pick_neighborhood(matches)
    city_m = _pick_city(matches)
    return (best_name(nb_m) if nb_m else "",
            best_name(city_m) if city_m else "",
            matches)


def render_row(r: dict) -> str:
    mb_n, mb_p = mapbox_str(r["mapbox"])
    dr_n, dr_city, dr_matches = district_str(r["district"])
    mb_combined = f"{mb_n}, {mb_p}" if mb_n and mb_p else (mb_n or mb_p or "—")
    dr_combined = f"{dr_n}, {dr_city}" if dr_n and dr_city else (dr_n or "—")

    # Soft agreement: case-insensitive substring either way (Mapbox uses
    # English by default; OSM may have local script — accept either as
    # "agreement" via tag fallback).
    agree = "—"
    if mb_n and dr_n:
        if (mb_n.lower() in dr_n.lower()) or (dr_n.lower() in mb_n.lower()):
            agree = "✓"
        else:
            agree = "≠"

    mb_ms = r["mapbox"].get("elapsed_ms", "—")
    dr_ms = r["district"].get("elapsed_ms", "—")
    dr_b = r["district"].get("bytes", 0)
    dr_q = r["district"].get("requests", 0)

    def esc(x):
        return html.escape(str(x)) if x is not None else ""

    extra_matches = ""
    if dr_matches[1:4]:
        extras = " · ".join(esc(best_name(m)) for m in dr_matches[1:4])
        extra_matches = f'<div class="extra">also: {extras}</div>'

    cls = {"✓": "ok", "≠": "diff", "—": "na"}[agree]
    return f"""<tr class="{cls}">
  <td class="loc">
    <div class="city">{esc(r['city'])}</div>
    <div class="ll">({r['lat']:.4f}, {r['lng']:.4f})</div>
    <div class="exp">expected: {esc(r['expected_neighborhood'])}</div>
  </td>
  <td class="agree">{agree}</td>
  <td>
    <div class="primary">{esc(mb_combined)}</div>
    <div class="meta">{mb_ms} ms</div>
  </td>
  <td>
    <div class="primary">{esc(dr_combined)}</div>
    {extra_matches}
    <div class="meta">{dr_ms} ms · {dr_q} req · {dr_b/1024:.1f} KiB</div>
  </td>
</tr>"""


def render_html(results: list) -> str:
    # Aggregate stats.
    mb_ms = [r["mapbox"]["elapsed_ms"] for r in results if "elapsed_ms" in r["mapbox"]]
    dr_ms = [r["district"]["elapsed_ms"] for r in results if "elapsed_ms" in r["district"]]
    dr_bytes = [r["district"]["bytes"] for r in results if "bytes" in r["district"]]
    dr_reqs = [r["district"]["requests"] for r in results if "requests" in r["district"]]

    agree_counts = Counter()
    coverage = {"mapbox_neighborhood": 0, "district_top": 0,
                "either": 0, "both": 0}
    for r in results:
        mb_n, _ = mapbox_str(r["mapbox"])
        dr_n, _, _ = district_str(r["district"])
        mb_has = bool(mb_n) and mb_n != "ERROR"
        dr_has = bool(dr_n) and dr_n != "ERROR"
        if mb_has: coverage["mapbox_neighborhood"] += 1
        if dr_has: coverage["district_top"] += 1
        if mb_has or dr_has: coverage["either"] += 1
        if mb_has and dr_has: coverage["both"] += 1
        if mb_has and dr_has:
            if mb_n.lower() in dr_n.lower() or dr_n.lower() in mb_n.lower():
                agree_counts["agree"] += 1
            else:
                agree_counts["differ"] += 1
        else:
            agree_counts["one-or-none"] += 1

    n = len(results)
    summary_html = f"""
<table class="summary">
  <tr><th colspan="3">Coverage (returned <i>some</i> name)</th></tr>
  <tr><td>Mapbox</td><td class="num">{coverage['mapbox_neighborhood']}</td><td class="pct">{coverage['mapbox_neighborhood']/n*100:.1f}%</td></tr>
  <tr><td>browser-district</td><td class="num">{coverage['district_top']}</td><td class="pct">{coverage['district_top']/n*100:.1f}%</td></tr>
  <tr><td>both</td><td class="num">{coverage['both']}</td><td class="pct">{coverage['both']/n*100:.1f}%</td></tr>
  <tr><td>either</td><td class="num">{coverage['either']}</td><td class="pct">{coverage['either']/n*100:.1f}%</td></tr>
  <tr><th colspan="3">Agreement (when both returned a name)</th></tr>
  <tr><td>names overlap (substring either way)</td><td class="num">{agree_counts['agree']}</td><td class="pct">{agree_counts['agree']/max(coverage['both'],1)*100:.1f}%</td></tr>
  <tr><td>names differ</td><td class="num">{agree_counts['differ']}</td><td class="pct">{agree_counts['differ']/max(coverage['both'],1)*100:.1f}%</td></tr>
  <tr><th colspan="3">Latency (ms per query)</th></tr>
  <tr><td>Mapbox</td><td colspan="2">p50 {percentile(mb_ms,50)} · p95 {percentile(mb_ms,95)} · max {max(mb_ms) if mb_ms else 0}</td></tr>
  <tr><td>browser-district (incl. retries on 429)</td><td colspan="2">p50 {percentile(dr_ms,50)} · p95 {percentile(dr_ms,95)} · max {max(dr_ms) if dr_ms else 0}</td></tr>
  <tr><th colspan="3">browser-district HTTP cost per query</th></tr>
  <tr><td>requests</td><td colspan="2">p50 {percentile(dr_reqs,50)} · p95 {percentile(dr_reqs,95)} · max {max(dr_reqs) if dr_reqs else 0}</td></tr>
  <tr><td>bytes</td><td colspan="2">p50 {percentile(dr_bytes,50)/1024:.1f} KiB · p95 {percentile(dr_bytes,95)/1024:.1f} KiB · total {sum(dr_bytes)/1024/1024:.1f} MiB</td></tr>
</table>"""

    rows = "".join(render_row(r) for r in results)
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8"><title>browser-district vs Mapbox eval</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif;
          margin: 24px; color: #222; max-width: 1200px; }}
  h1 {{ margin: 0 0 4px; }}
  .lede {{ color: #666; margin-bottom: 24px; }}
  table.summary {{ border-collapse: collapse; margin-bottom: 32px; min-width: 480px; }}
  table.summary th {{ text-align: left; padding: 6px 12px; background: #eef; border-bottom: 1px solid #ccd; }}
  table.summary td {{ padding: 4px 12px; border-bottom: 1px solid #eee; }}
  table.summary td.num {{ text-align: right; font-family: ui-monospace, monospace; }}
  table.summary td.pct {{ text-align: right; color: #666; font-size: 12px; font-family: ui-monospace, monospace; }}
  table.cmp {{ border-collapse: collapse; width: 100%; }}
  table.cmp th, table.cmp td {{ padding: 8px 10px; vertical-align: top; border-bottom: 1px solid #eee; font-size: 13px; }}
  table.cmp th {{ background: #f4f4f4; text-align: left; position: sticky; top: 0; }}
  tr.diff {{ background: #fff8e6; }}
  tr.na   {{ background: #f9f9f9; color: #777; }}
  tr.ok   td.agree {{ color: #2a7; font-weight: 600; }}
  tr.diff td.agree {{ color: #c80; font-weight: 600; }}
  td.loc .city {{ font-weight: 600; }}
  td.loc .ll   {{ color: #888; font-size: 11px; font-family: ui-monospace, monospace; }}
  td.loc .exp  {{ color: #666; font-size: 11px; font-style: italic; }}
  td .primary  {{ font-weight: 500; }}
  td .extra    {{ color: #888; font-size: 11px; margin-top: 2px; }}
  td .meta     {{ color: #aaa; font-size: 11px; font-family: ui-monospace, monospace; }}
  td.agree {{ text-align: center; font-size: 18px; }}
</style>
</head>
<body>
<h1>browser-district vs Mapbox</h1>
<p class="lede">Reverse geocode {n} curated lat/lng points across {len(set(r['country'] for r in results))} countries
through both APIs, side by side. Mapbox returns its &quot;neighborhood, place&quot; pair (the format
<code>rtirl-obs/neighborhood.html</code> uses). browser-district returns the highest-priority OSM
feature at the point (place&nbsp;tag &gt; admin_level&nbsp;9/10 &gt; admin_level&nbsp;7/8 &gt; leisure)
plus the admin_level&nbsp;&le;&nbsp;8 context.</p>

<p class="lede"><b>About the agreement metric:</b> &quot;agree&quot; means a case-insensitive
substring match either way. It's <i>conservative</i> &mdash; OSM and Mapbox often label the
same point with names that are both correct but at different granularity (e.g. Mapbox &quot;Bondi
Beach&quot; vs OSM &quot;Bondi Park&quot;), or in different scripts when overlay coverage is
thin. Skim the yellow rows; many are both-correct.</p>

<p class="lede"><b>Latency caveat:</b> browser-district numbers are from a sequential Python
client doing one Range request at a time against R2 from a residential connection, with
exponential-backoff retries on R2's 429s. The browser <code>LookupClient</code> issues
same-level page fetches in parallel and sees roughly 3&ndash;4 RTTs total
(~300&ndash;500&nbsp;ms instead of ~1.5&nbsp;s).</p>

{summary_html}

<table class="cmp">
<thead>
<tr><th>Location</th><th>=</th><th>Mapbox</th><th>browser-district</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body></html>"""


def main():
    src = Path(__file__).parent / "results.json"
    if not src.exists():
        print(f"missing {src}; run run.py first", file=sys.stderr)
        sys.exit(1)
    results = json.loads(src.read_text())
    out = Path(__file__).parent / "report.html"
    out.write_text(render_html(results))
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
