#!/usr/bin/env python3
"""
One-time (or occasional) backfill of past headlines from free public archives, so the rules can be
replayed against weeks of real news before the display's own archive (scripts/archive.py) builds up.

    python scripts/backfill.py OUT_DIR [--days 30] [--force]

For each past day it rebuilds roughly what each configured source would have returned:
  - Google News sources (Enquirer, City Hall sweep, City of Cincinnati, Ohio/Federal sweeps): the same
    search from config.json, with "when:1d" swapped for that day's date range.
  - Newsrooms we read directly (WCPO, WLWT, FOX19, Local 12, WVXU, Signal, Ohio Capital Journal), from
    three archives, best first:
      1. Internet Archive (Wayback Machine) copies of the newsroom's own RSS feed: real headlines,
         summaries and web addresses, but only on days the Archive saved a copy.
      2. GDELT (gdeltproject.org), which logs news articles every 15 minutes: headline, web address, time.
      3. The newsroom's Google News search (its "fallback" in config.json): headline only.

Writes OUT_DIR/backfill/YYYY-MM-DD.json.gz = {"day", "articles": [...], "counts": {...}} per Eastern day,
plus OUT_DIR/backfill/log.json. Junk is NOT removed here: scripts/replay.py applies the current rules,
so changes to the junk lists can be replayed too. Days already present are skipped unless --force.

Free and public only; no keys. Polite: one request every few seconds (GDELT asks for one per 5 s).
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402
from archive import write_gz  # noqa: E402

ET = ZoneInfo("America/New_York")
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
CDX = "https://web.archive.org/cdx/search/cdx"
LOG = []
_last = {}


def polite(kind, gap):
    wait = _last.get(kind, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[kind] = time.time()


def get(url, kind, gap, tries=3):
    for n in range(tries):
        polite(kind, gap)
        try:
            return collect.fetch(url)
        except Exception as ex:
            err = collect.http_error_text(ex) if hasattr(collect, "http_error_text") else type(ex).__name__
            if n == tries - 1:
                LOG.append(f"{kind}: {err}: {url[:160]}")
                return None
            time.sleep(10 * (n + 1))


def to_text(data):
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


def clean_article(a):
    return {k: iso(v) for k, v in a.items() if not k.startswith("_") or k in ("_google", "_official", "_narrow", "_via", "_from", "_source_site")}


# ------------------------------------------------------------------ Google News (dated searches)

def dated_google_url(url, day):
    """'...+when:1d&hl=...' -> '...+after:2026-11-02+before:2026-11-03&hl=...' (Google's dates are UTC-ish)."""
    after, before = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    if re.search(r"when:\d+[dh]", url):
        return re.sub(r"when:\d+[dh]", f"after:{after}+before:{before}", url)
    return url.replace("&hl=", f"+after:{after}+before:{before}&hl=", 1)


def google_items(url, src, day, alias_pairs, as_newsroom=False):
    data = get(dated_google_url(url, day), "google", 2.5)
    if data is None:
        return None
    now = datetime.combine(day + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
    narrow = bool(re.search(r"site:[a-z0-9.-]+/[a-z]", url))
    items = collect.parse_feed(data, src["name"], True, alias_pairs, now, src.get("official", False), narrow)
    for i in items:
        i["_from"] = "google"
        if as_newsroom:
            # Stand-in for the newsroom's own feed: treat it like one (local by definition).
            i["outlet"], i["_google"] = src["name"], False
    return items


# ------------------------------------------------------------------ GDELT

def site_of(url):
    m = re.search(r"site:([a-z0-9.\-]+)(/[a-z0-9/_\-]*)?", url or "")
    if m:
        return m.group(1), (m.group(2) or "")
    host = urllib.parse.urlparse(url).hostname or ""
    return re.sub(r"^www\.", "", host), ""


def gdelt_items(src, domain, path_prefix, day, alias_names):
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    q = urllib.parse.urlencode({"query": f"domainis:{domain}", "mode": "artlist",
                                "maxrecords": 250, "format": "json", "sort": "datedesc",
                                "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                                "enddatetime": end.strftime("%Y%m%d%H%M%S")})
    data = get(f"{GDELT}?{q}", "gdelt", 5.5)
    if data is None:
        return None
    try:
        arts = json.loads(to_text(data)).get("articles", [])
    except ValueError:
        LOG.append(f"gdelt: not JSON for {domain} {day}: {to_text(data)[:120]}")
        return None
    out = []
    for a in arts:
        link = a.get("url", "")
        if path_prefix and path_prefix not in urllib.parse.urlparse(link).path:
            continue
        title = collect.clean_text(a.get("title", ""))
        # Page titles often end with the site name: "… | WCPO 9 Cincinnati", "… - FOX19".
        m = re.match(r"^(.*?)\s+[|\-–]\s+([^|\-–]+)$", title)
        if m and any(n in m.group(2).lower() for n in alias_names):
            title = m.group(1)
        if not title:
            continue
        try:
            published = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append({"title": collect.clean_headline(title), "link": link, "outlet": src["name"], "summary": "",
                    "published": published, "_google": False, "_official": False, "_narrow": False,
                    "_via": src["name"], "_from": "gdelt"})
    return out


# ------------------------------------------------------------------ Wayback Machine

def wayback_snapshots(feed_url, first, last):
    q = urllib.parse.urlencode({"url": feed_url, "from": first.strftime("%Y%m%d"), "to": last.strftime("%Y%m%d"),
                                "output": "json", "filter": "statuscode:200", "collapse": "timestamp:10",
                                "fl": "timestamp,original"})
    data = get(f"{CDX}?{q}", "wayback", 2)
    if not data:
        return {}
    try:
        rows = json.loads(to_text(data))[1:]
    except ValueError:
        return {}
    by_day = {}
    for ts, orig in rows:
        t = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        by_day.setdefault(t.astimezone(ET).date(), []).append((t, ts, orig))
    return by_day


def wayback_items(src, snaps, alias_pairs, per_day=2):
    """Up to per_day copies of the feed saved that day: the late-morning and evening ones."""
    out = []
    targets = [16, 23]          # ~11 AM and ~6 PM Eastern, in UTC hours
    picked = []
    for h in targets[:per_day]:
        best = min(snaps, key=lambda s: abs(s[0].hour + s[0].minute / 60 - h))
        if best not in picked:
            picked.append(best)
    for t, ts, orig in picked:
        data = get(f"https://web.archive.org/web/{ts}id_/{orig}", "wayback", 2)
        if not data:
            continue
        items = collect.parse_feed(data, src["name"], False, alias_pairs, t, src.get("official", False), False)
        for i in items:
            i["_from"] = "wayback"
        out.extend(items)
    return out


# ------------------------------------------------------------------ merge

def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def merge(lists):
    """Keep the first copy of each headline per outlet (lists come best-source first)."""
    seen, out = set(), []
    for items in lists:
        for i in items or []:
            k = (i["outlet"], norm(i["title"]))
            if k in seen or (i.get("link") and ("link", i["link"]) in seen):
                continue
            seen.add(k)
            if i.get("link"):
                seen.add(("link", i["link"]))
            out.append(i)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-wayback", action="store_true")
    ap.add_argument("--no-gdelt", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(collect.CONFIG_PATH.read_text())
    alias_pairs = collect.make_alias_lookup(cfg.get("source_aliases", {}))
    out = Path(args.out_dir) / "backfill"
    today = datetime.now(ET).date()
    days = [today - timedelta(days=n) for n in range(args.days, 0, -1)]
    todo = [d for d in days if args.force or not (out / f"{d.isoformat()}.json.gz").exists()]
    if not todo:
        print("Nothing to backfill: every day is already there.")
        return 0
    print(f"Backfilling {len(todo)} day(s): {todo[0]} to {todo[-1]}", flush=True)

    # Wayback: one listing per newsroom feed for the whole range, then a few copies per day.
    snaps = {}
    if not args.no_wayback:
        for src in cfg["sources"]:
            for feed in src.get("feeds", [])[:1]:
                if "news.google.com" not in feed:
                    snaps[src["name"]] = wayback_snapshots(feed, todo[0], todo[-1] + timedelta(days=1))
                    print(f"  Wayback: {src['name']}: copies on {len(snaps[src['name']])} day(s)", flush=True)

    for day in todo:
        lists, counts = {"wayback": [], "gdelt": [], "google": []}, {}
        for src in cfg["sources"]:
            name = src["name"]
            google_feeds = [f for f in src.get("feeds", []) if "news.google.com" in f]
            got = {}
            if google_feeds:
                items = []
                for f in google_feeds:
                    items += google_items(f, src, day, alias_pairs) or []
                if src.get("max_items"):
                    items = sorted(items, key=lambda i: i["published"], reverse=True)[: src["max_items"] * 2]
                got["google"] = items
            else:
                day_snaps = (snaps.get(name) or {}).get(day)
                if day_snaps:
                    got["wayback"] = wayback_items(src, day_snaps, alias_pairs)
                if not args.no_gdelt:
                    domain, prefix = site_of(src["feeds"][0])
                    names = [a for a in cfg.get("source_aliases", {}).get(name, [])] + [name.lower()]
                    got["gdelt"] = gdelt_items(src, domain, prefix, day, names) or []
                if src.get("fallback"):
                    got["google"] = google_items(src["fallback"], src, day, alias_pairs, as_newsroom=True) or []
            for k, v in got.items():
                for i in v:
                    i.setdefault("_via", name)
            for k, v in got.items():
                lists[k].append(v)
            counts[name] = {k: len(v) for k, v in got.items()}
        # Articles dated within this Eastern day (feeds and searches also return neighbors' days).
        start = datetime.combine(day, datetime.min.time(), tzinfo=ET)
        # Best source first for every outlet: the newsroom's own feed, then GDELT, then Google.
        ordered = lists["wayback"] + lists["gdelt"] + lists["google"]
        articles = [i for i in merge(ordered) if start <= i["published"] < start + timedelta(days=1)]
        write_gz(out / f"{day.isoformat()}.json.gz",
                 {"day": day.isoformat(), "articles": [clean_article(a) for a in articles], "counts": counts})
        print(f"  {day}: {len(articles)} articles  " + ", ".join(
            f"{n} {'/'.join(f'{k} {v}' for k, v in c.items())}" for n, c in counts.items()), flush=True)

    log_path = out / "log.json"
    old = json.loads(log_path.read_text()) if log_path.exists() else []
    log_path.write_text(json.dumps((old + [{"at": datetime.now(timezone.utc).isoformat(), "problems": LOG[:200]}])[-10:], indent=1))
    print(f"Done. {len(LOG)} request problem(s); see backfill/log.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
