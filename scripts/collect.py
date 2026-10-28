#!/usr/bin/env python3
"""
Collects Cincinnati headlines + weather and writes site/news.json.

How it works, in short:
  1. Read every feed listed in config.json (RSS = a site's list of latest headlines).
  2. Group headlines that are about the same story, even across outlets.
  3. Score each story: more outlets covering it + City Hall keywords + newer = higher.
  4. Ask the National Weather Service (free, no key) for now + next 24 hours.
  5. Save it all as one small JSON file that the display page reads.

Run locally:   python scripts/collect.py
"""

import json
import math
import re
import sys
import time
import html
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUT_PATH = ROOT / "site" / "news.json"

USER_AGENT = "cincy-news-glance/1.0 (personal ambient news display; github.com)"
TIMEOUT = 20

STOPWORDS = set("""
a an the and or but of to in on at for from by with as is are was were be been being it its this that
these those after before over under into about amid against new says say said will would could can may
what why how who when where up down out off more most than then there their they he she his her you your
our we us not no yes vs via just still also first last next one two three four five six live update updates
video watch photos photo report reports cincinnati cincy ohio kentucky local news today tonight week
""".split())


# ---------------------------------------------------------------- fetching

def fetch(url, accept="application/rss+xml, application/xml, text/xml, */*"):
    """Download a URL and return its bytes (raises on failure)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch_json(url):
    return json.loads(fetch(url, accept="application/geo+json, application/json"))


# ---------------------------------------------------------------- text helpers

def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def tokens(title):
    words = re.findall(r"[a-z0-9']+", title.lower())
    out = set()
    for w in words:
        w = w.strip("'")
        if len(w) < 3 or w in STOPWORDS:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


def term_regex(term):
    """Acronyms / capitalised single words (CPD, Metro) match case-sensitively; phrases don't."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    single_word_with_caps = (" " not in term) and any(c.isupper() for c in term)
    return re.compile(pattern, 0 if single_word_with_caps else re.IGNORECASE)


# ---------------------------------------------------------------- source names

def make_alias_lookup(aliases):
    pairs = []
    for canonical, keys in aliases.items():
        for k in keys:
            pairs.append((k.lower(), canonical))
    return pairs


def canonical_source(raw, alias_pairs, default):
    low = (raw or "").lower()
    for key, canonical in alias_pairs:
        if key in low:
            return canonical
    return raw.strip() if raw else default


# ---------------------------------------------------------------- feeds → items

def parse_feed(data, source_name, is_google, alias_pairs, now):
    parsed = feedparser.parse(data)
    items = []
    for e in parsed.entries:
        title = clean_text(e.get("title", ""))
        if not title:
            continue
        outlet = source_name
        if is_google:
            # Google News titles end with " - Outlet name"; the outlet is also in <source>.
            src_title = ""
            if e.get("source") and isinstance(e.source, dict):
                src_title = e.source.get("title", "")
            m = re.match(r"^(.*)\s+-\s+([^-]+)$", title)
            if m:
                title, tail = m.group(1).strip(), m.group(2).strip()
                src_title = src_title or tail
            outlet = canonical_source(src_title, alias_pairs, source_name)
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else now
        if published > now + timedelta(minutes=10):
            published = now
        items.append({
            "title": title,
            "link": e.get("link", ""),
            "outlet": outlet,
            "summary": clean_text(e.get("summary", ""))[:280],
            "published": published,
        })
    return items


def collect_items(cfg, now):
    alias_pairs = make_alias_lookup(cfg.get("source_aliases", {}))
    all_items, status = [], []
    for src in cfg["sources"]:
        got, used, errors = [], None, []
        candidates = list(src.get("feeds", []))
        if src.get("fallback"):
            candidates.append(src["fallback"])
        for url in candidates:
            try:
                data = fetch(url)
                items = parse_feed(data, src["name"], "news.google.com" in url, alias_pairs, now)
                if items:
                    got, used = items, url
                    break
                errors.append(f"empty: {url}")
            except Exception as ex:  # network errors, bad XML, etc.
                errors.append(f"{type(ex).__name__}: {url}")
        status.append({
            "source": src["name"],
            "ok": bool(got),
            "items": len(got),
            "used": used,
            "used_fallback": bool(used and used == src.get("fallback")),
            "errors": errors[:3],
        })
        all_items.extend(got)
    return all_items, status


# ---------------------------------------------------------------- grouping + scoring

def same_story(a_tokens, b_tokens):
    if not a_tokens or not b_tokens:
        return False
    shared = len(a_tokens & b_tokens)
    jaccard = shared / len(a_tokens | b_tokens)
    return jaccard >= 0.34 or (shared >= 4 and jaccard >= 0.22)


def cluster(items):
    """Greedy grouping: each headline joins the first story it closely matches."""
    clusters = []
    for it in sorted(items, key=lambda x: x["published"]):
        it["_tok"] = tokens(it["title"])
        home = None
        for c in clusters:
            if same_story(it["_tok"], c["_tok"]):
                home = c
                break
        if home is None:
            clusters.append({"items": [it], "_tok": set(it["_tok"])})
        else:
            # Skip exact duplicates (same outlet re-listing the same headline/link).
            if any(x["link"] == it["link"] or (x["outlet"] == it["outlet"] and x["title"] == it["title"]) for x in home["items"]):
                continue
            home["items"].append(it)
            home["_tok"] |= it["_tok"]
    return clusters


def score_clusters(clusters, cfg, now):
    th = cfg["thresholds"]
    city_terms = [(int(w), term_regex(t)) for w, terms in cfg["city_keywords"].items() for t in terms]
    local_terms = [term_regex(t) for t in cfg["local_terms"]]
    down_terms = [term_regex(t) for t in cfg.get("downweight_terms", [])]
    breaking_terms = [term_regex(t) for t in cfg.get("breaking_terms", [])]
    local_outlets = {s["name"] for s in cfg["sources"] if not s.get("sweep")}

    stories = []
    for c in clusters:
        its = c["items"]
        outlets = sorted({i["outlet"] for i in its})
        first = min(i["published"] for i in its)
        latest = max(i["published"] for i in its)
        text = " ".join(i["title"] + " " + i["summary"] for i in its)

        matched = sorted({w for w, rx in city_terms if rx.search(text)}, reverse=True)
        city_score = sum(w for w, rx in city_terms if rx.search(text))
        is_local = any(rx.search(text) for rx in local_terms) or any(o in local_outlets for o in outlets)
        if not is_local:
            city_score *= 0.3            # another city's mayor/council
        city_score = min(city_score, 8)
        is_city = city_score >= 3

        n = len(outlets)
        age_h = max((now - latest).total_seconds() / 3600, 0)
        recency = math.exp(-age_h / 6)   # a story loses ~half its weight every 4 hours
        score = (2.0 * n + 1.2 * city_score + (1.0 if is_local else -2.0)) * recency
        if any(rx.search(text) for rx in down_terms):
            score *= 0.5

        # "Developing" = breaking news: many outlets fast, or a breaking-type story picked up quickly.
        fresh = (now - first) <= timedelta(hours=th["developing_window_hours"])
        breaking = any(rx.search(text) for rx in breaking_terms)
        developing = fresh and (
            n >= th["developing_outlets"]
            or (breaking and n >= th["big_story_outlets"])
        )

        # Headline to show: prefer a local outlet's version, then the most recent.
        lead = sorted(its, key=lambda i: (i["outlet"] not in local_outlets, -i["published"].timestamp()))[0]
        stories.append({
            "id": re.sub(r"[^a-z0-9]+", "-", lead["title"].lower())[:60].strip("-"),
            "title": lead["title"],
            "link": lead["link"],
            "outlet": lead["outlet"],
            "summary": lead["summary"],
            "outlets": outlets,
            "outlet_count": n,
            "first_seen": first.isoformat(),
            "updated": latest.isoformat(),
            "is_city": is_city,
            "is_big": n >= th["big_story_outlets"],
            "is_new": (now - first) <= timedelta(minutes=th["new_minutes"]),
            "developing": developing,
            "score": round(score, 3),
            "_age_h": age_h,
            "_local": is_local,
        })

    lookback = th["lookback_hours"]
    stories = [s for s in stories if s["_age_h"] <= lookback and s["_local"] and s["score"] > 0]
    stories.sort(key=lambda s: s["score"], reverse=True)
    for s in stories:
        s.pop("_age_h", None)
        s.pop("_local", None)
    return stories[: th["max_stories"]]


# ---------------------------------------------------------------- weather (National Weather Service)

def describe_change(hourly, alerts, now_temp):
    """One short line about anything notable in the next 24 hours."""
    if alerts:
        a = alerts[0]
        return {"text": a["event"] + (f" until {a['ends']}" if a.get("ends") else ""), "level": "alert"}

    next24 = hourly[:24]
    if not next24:
        return {"text": "", "level": "none"}

    def hour_label(iso):
        t = datetime.fromisoformat(iso)
        return t.strftime("%-I %p")

    first_pop = next24[0]["pop"] or 0
    for h in next24:
        pop = h["pop"] or 0
        if pop >= 50 and first_pop < 40:
            sf = h["short"].lower()
            kind = "Snow" if "snow" in sf else "Storms" if "thunder" in sf else "Rain"
            return {"text": f"{kind} likely around {hour_label(h['time'])}", "level": "notable"}

    temps = [h["temp"] for h in next24 if h["temp"] is not None]
    if temps and now_temp is not None:
        lo, hi = min(temps), max(temps)
        if now_temp - lo >= 15:
            when = next(h for h in next24 if h["temp"] == lo)
            return {"text": f"Turning colder — {lo}° by {hour_label(when['time'])}", "level": "notable"}
        if hi - now_temp >= 15:
            when = next(h for h in next24 if h["temp"] == hi)
            return {"text": f"Warming up — {hi}° by {hour_label(when['time'])}", "level": "notable"}
        if lo <= 32 < now_temp:
            when = next(h for h in next24 if h["temp"] <= 32)
            return {"text": f"Freezing by {hour_label(when['time'])}", "level": "notable"}

    return {"text": "No big changes in the next 24 hours", "level": "none"}


def get_weather(cfg):
    loc = cfg["location"]
    point = fetch_json(f"https://api.weather.gov/points/{loc['lat']},{loc['lon']}")
    props = point["properties"]
    hourly_raw = fetch_json(props["forecastHourly"])["properties"]["periods"]
    hourly = [{
        "time": p["startTime"],
        "temp": p.get("temperature"),
        "short": p.get("shortForecast", ""),
        "pop": (p.get("probabilityOfPrecipitation") or {}).get("value"),
        "is_day": p.get("isDaytime", True),
    } for p in hourly_raw]

    alerts = []
    try:
        al = fetch_json(f"https://api.weather.gov/alerts/active?point={loc['lat']},{loc['lon']}")
        for f in al.get("features", [])[:3]:
            p = f["properties"]
            ends = p.get("ends") or p.get("expires")
            ends_label = datetime.fromisoformat(ends).strftime("%-I %p") if ends else ""
            alerts.append({"event": p.get("event", "Weather alert"), "headline": p.get("headline", ""), "ends": ends_label})
    except Exception:
        pass

    now_h = hourly[0] if hourly else {}
    return {
        "temp": now_h.get("temp"),
        "short": now_h.get("short", ""),
        "is_day": now_h.get("is_day", True),
        "change": describe_change(hourly, alerts, now_h.get("temp")),
        "alerts": alerts,
    }


# ---------------------------------------------------------------- main

def build(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    items, status = collect_items(cfg, now)
    stories = score_clusters(cluster(items), cfg, now)

    try:
        weather = get_weather(cfg)
        weather["ok"] = True
    except Exception as ex:
        weather = {"ok": False, "error": f"{type(ex).__name__}"}

    dev_list = [s for s in stories if s["developing"]]
    developing = max(dev_list, key=lambda s: (s["outlet_count"], s["first_seen"])) if dev_list else None
    cutoff = now - timedelta(hours=12)
    today = [s for s in stories if datetime.fromisoformat(s["first_seen"]) >= cutoff]
    return {
        "generated_at": now.isoformat(),
        "weather": weather,
        "developing": developing["id"] if developing else None,
        "counts": {"stories": len(today), "city": sum(1 for s in today if s["is_city"])},
        "stories": stories,
        "sources_status": status,
    }


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    out = build(cfg)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = OUT_PATH.read_text() if OUT_PATH.exists() else None
    OUT_PATH.write_text(json.dumps(out, indent=1))
    ok = sum(1 for s in out["sources_status"] if s["ok"])
    print(f"{len(out['stories'])} stories from {ok}/{len(out['sources_status'])} sources; "
          f"weather ok={out['weather'].get('ok')}; developing={out['developing']}")
    for s in out["sources_status"]:
        flag = "OK " if s["ok"] else "-- "
        print(f"  {flag}{s['source']:<16} {s['items']:>3} items  {s['used'] or '; '.join(s['errors'])}")
    if ok == 0 and previous:
        # Every feed failed (network hiccup): keep the last good file instead of blanking the screen.
        OUT_PATH.write_text(previous)
        print("All sources failed; kept previous news.json")


if __name__ == "__main__":
    sys.exit(main())
