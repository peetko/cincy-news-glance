#!/usr/bin/env python3
"""
Collects Cincinnati headlines, City meetings and weather, and writes site/news.json.

How it works, in short:
  1. Read every feed listed in config.json (RSS = a site's list of latest headlines),
     plus today's City meetings from the City's Legistar calendar.
  2. Group headlines that are about the same story, even across outlets.
  3. Score each story twice: overall (how many outlets + how new) and for City work
     (city staff, elected officials, departments, laws that affect local government).
  4. Remember the last run (read back from the live site) so leads don't flip-flop,
     "first seen" times stay put, and weather survives a hiccup.
  5. Ask the National Weather Service (free, no key) for now + next 24 hours.
  6. Save it all as one small JSON file that the display page reads.

Run locally:   python scripts/collect.py
"""

import json
import math
import re
import sys
import html
import os
import urllib.request
from urllib.parse import urlparse, quote
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUT_PATH = ROOT / "site" / "news.json"

USER_AGENT = "cincy-news-glance/1.1 (personal ambient news display; github.com)"
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


# "WATCH: …", "UPDATE: …", "… | WCPO 9 Cincinnati", "… (VIDEO)" — tags that make the same story look different.
HEADLINE_PREFIX = re.compile(r"^(?:(?:WATCH|VIDEO|UPDATE|UPDATED|LIVE|BREAKING|PHOTOS|GALLERY|LISTEN|NEW|EXCLUSIVE|"
                             r"LIVE UPDATES|DEVELOPING)\s*[:|\-–—]\s*)+", re.I)
HEADLINE_SUFFIX = re.compile(r"\s*(?:[|–—]\s*(?:WCPO|WLWT|FOX19|Local 12|WKRC|WVXU|Signal|Enquirer|Cincinnati)[^|]*"
                             r"|\((?:VIDEO|PHOTOS|WATCH|UPDATE)\))\s*$", re.I)


def clean_headline(t):
    t = HEADLINE_PREFIX.sub("", t.strip())
    return HEADLINE_SUFFIX.sub("", t).strip()


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


def first_hit(patterns, text):
    for rx in patterns:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------- source names

def make_alias_lookup(aliases):
    return [(k.lower(), canonical) for canonical, keys in aliases.items() for k in keys]


def canonical_source(raw, alias_pairs, default):
    low = (raw or "").lower()
    for key, canonical in alias_pairs:
        if key in low:
            return canonical
    return raw.strip() if raw else default


# ---------------------------------------------------------------- junk filter

# "Michael Elmer Bouchonnet", "Mary Ann (Smith) Jones", "Robert 'Bob' Lee Jr." — a bare name is an obituary.
NAME_WORD = r"(?:[A-Z][a-zA-Z'’.-]+|[A-Z]\.|\"[^\"]+\"|'[^']+'|\([^)]+\)|Jr\.?|Sr\.?|II|III|IV)"
NAME_ONLY = re.compile(rf"^{NAME_WORD}(?:,?\s+{NAME_WORD}){{1,5}}$")
NOT_NAMES = re.compile(r"\b(Cincinnati|Bengals|Reds|FC|Bearcats|Musketeers|Council|County|City|Ohio|Kentucky|"
                       r"Park|Parks|Street|Road|Bridge|School|Schools|Police|Fire|Day|Week|Weekend|News|Update|Live)\b")


_LOCAL_CACHE = {}


def looks_like_press_release(item, cfg):
    """
    cincinnati.com also hosts wire press releases ("Viirtue Now Live in OpenAI's Plugin Marketplace…").
    They come through the Google News search, never name a local place, and Capitalize Every Word
    or SHOUT. The Enquirer's own headlines are sentence case.
    """
    if not item.get("_google") or item.get("_official"):
        return False          # the City's own releases ("Taft Road and Collins Ave. Closed…") are never junk
    key = id(cfg)
    if key not in _LOCAL_CACHE:
        terms = list(cfg.get("local_terms", [])) + list(cfg.get("cincinnati_anchors", []))
        _LOCAL_CACHE[key] = [term_regex(t) for t in terms]
    title = item["title"]
    if first_hit(_LOCAL_CACHE[key], title):
        return False
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’.-]*", title) if len(w) > 3]
    if len(words) < 4:
        return False
    if title.upper() == title:
        return True
    capitalized = sum(1 for w in words if w[0].isupper())
    return capitalized / len(words) >= 0.8


def is_junk(item, cfg):
    ex = cfg.get("exclude", {})
    title, link = item["title"], item["link"].lower()
    low = (title + " " + item.get("summary", "")).lower()
    if any(t.lower() in low for t in ex.get("title_terms", [])):
        return True
    if any(t.lower() in link for t in ex.get("url_terms", [])):
        return True
    if cfg.get("exclude", {}).get("drop_press_releases", True) and looks_like_press_release(item, cfg):
        return True
    # Name-only check: only for Google News results (where obituaries slip in), and never
    # when the "name" is a City official or a Cincinnati place.
    if (ex.get("drop_name_only_titles", True) and item.get("_google")
            and NAME_ONLY.match(title) and not NOT_NAMES.search(title)
            and not any(a.lower() in title.lower() for a in cfg.get("cincinnati_anchors", []))):
        return True
    return False


# ---------------------------------------------------------------- feeds → items

def parse_feed(data, source_name, is_google, alias_pairs, now, official=False):
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
            outlet = source_name if official else canonical_source(src_title, alias_pairs, source_name)
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else now
        if published > now + timedelta(minutes=10):
            published = now
        items.append({
            "title": clean_headline(title),
            "link": e.get("link", ""),
            "outlet": outlet,
            # Google News "summaries" just repeat the headline + outlet name, so drop them.
            "summary": "" if is_google else clean_text(e.get("summary", ""))[:280],
            "published": published,
            "_google": is_google,
            "_official": official,
        })
    return items


def collect_items(cfg, now):
    alias_pairs = make_alias_lookup(cfg.get("source_aliases", {}))
    all_items, status = [], []
    for src in cfg["sources"]:
        got, used, errors, dropped, dropped_titles = [], None, [], 0, []
        candidates = list(src.get("feeds", []))
        if src.get("fallback"):
            candidates.append(src["fallback"])
        for url in candidates:
            try:
                data = fetch(url)
                items = parse_feed(data, src["name"], "news.google.com" in url, alias_pairs, now, src.get("official", False))
                kept = [i for i in items if not is_junk(i, cfg)]
                dropped = len(items) - len(kept)
                dropped_titles = [i["title"] for i in items if is_junk(i, cfg)][:12]
                if kept:
                    if src.get("max_items"):
                        kept = sorted(kept, key=lambda i: i["published"], reverse=True)[: src["max_items"]]
                    got, used = kept, url
                    break
                errors.append(f"empty: {url}")
            except Exception as ex:  # network errors, bad XML, etc.
                errors.append(f"{type(ex).__name__}: {url}")
        status.append({
            "source": src["name"], "ok": bool(got), "items": len(got), "junk_dropped": dropped,
            "junk_examples": dropped_titles,
            "used": used, "used_fallback": bool(used and used == src.get("fallback")), "errors": errors[:3],
        })
        all_items.extend(got)
    return all_items, status


# ---------------------------------------------------------------- City meetings (Legistar)

def http_error_text(ex):
    """'HTTP 403: Token is required' instead of a bare 'HTTPError'."""
    code = getattr(ex, "code", None)
    body = ""
    try:
        body = ex.read().decode("utf-8", "replace")[:160] if hasattr(ex, "read") else ""
    except Exception:
        pass
    body = clean_text(body)
    return f"HTTP {code}: {body}" if code else f"{type(ex).__name__}: {ex}"[:200]


def get_meetings(cfg, now):
    """Today's City Council and committee meetings, or the next meeting day within a week."""
    lg = cfg.get("legistar")
    if not lg:
        return {"ok": False, "error": "not configured"}
    local = now.astimezone(timezone(timedelta(hours=-4 if is_dst(now) else -5)))
    start = local.date()
    end = start + timedelta(days=lg.get("look_ahead_days", 7))

    rows, errors, used = None, [], None
    # 1) Legistar's data service. The client name matches the calendar's web address:
    #    cincinnatioh.legistar.com  →  "cincinnatioh".
    clients = lg.get("clients") or [lg.get("client", "cincinnatioh")]
    token = os.environ.get("LEGISTAR_TOKEN") or lg.get("token") or ""
    flt = f"EventDate ge datetime'{start.isoformat()}' and EventDate lt datetime'{end.isoformat()}'"
    for client in clients:
        url = f"https://webapi.legistar.com/v1/{client}/events?$filter={quote(flt)}&$orderby=EventDate"
        if token:
            url += f"&token={quote(token)}"
        try:
            rows = legistar_rows(fetch_json(url))
            used = f"web API ({client})"
            break
        except Exception as ex:
            errors.append(f"{client}: {http_error_text(ex)}")
    # 2) Backup: the calendar's RSS link, copied from the City's Legistar calendar page.
    if rows is None and lg.get("rss"):
        try:
            rows = legistar_rss_rows(fetch(lg["rss"]))
            used = "calendar RSS"
        except Exception as ex:
            errors.append(f"rss: {http_error_text(ex)}")
    if rows is None:
        return {"ok": False, "error": " | ".join(errors)[:400]}

    rows = [r for r in rows if start.isoformat() <= r["date"] < end.isoformat()]
    rows.sort(key=lambda r: (r["date"], parse_clock(r["time"])))
    first_day = rows[0]["date"] if rows else None
    shown = [r for r in rows if r["date"] == first_day]
    return {
        "ok": True,
        "used": used,
        "day": first_day,
        "is_today": first_day == start.isoformat(),
        "meetings": shown[:6],
        "bodies": sorted({r["body"] for r in rows if r["body"]}),
    }


def legistar_rss_rows(data):
    """Calendar RSS items look like 'Budget, Finance & Governance Committee - 10/28/2026 - 1:00 PM'."""
    rows = []
    for e in feedparser.parse(data).entries:
        t = clean_text(e.get("title", ""))
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
        if not m:
            continue
        day = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        tm = re.search(r"\d{1,2}:\d{2}\s*[AP]M", t, re.I)
        body = re.split(r"\s+-\s+\d{1,2}/", t)[0].strip(" -")
        rows.append({"date": day, "time": tm.group(0).upper() if tm else "", "body": body,
                     "location": "", "agenda": "", "url": e.get("link", "")})
    return rows


def legistar_rows(events):
    rows = []
    for e in events:
        if (e.get("EventAgendaStatusName") or "").lower() in ("cancelled", "canceled"):
            continue
        day = (e.get("EventDate") or "")[:10]
        rows.append({
            "date": day,
            "time": (e.get("EventTime") or "").strip(),
            "body": (e.get("EventBodyName") or "").strip(),
            "location": (e.get("EventLocation") or "").strip(),
            "agenda": e.get("EventAgendaFile") or "",
            "url": e.get("EventInSiteURL") or "",
        })
    return rows


def parse_clock(t):
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", t or "", re.I)
    if not m:
        return 99 * 60
    h, mnt, ap = int(m.group(1)) % 12, int(m.group(2)), m.group(3).upper()
    return (h + (12 if ap == "PM" else 0)) * 60 + mnt


def is_dst(now):
    """US Eastern daylight time: 2nd Sunday of March to 1st Sunday of November."""
    y = now.year
    march = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    start = march + timedelta(days=(6 - march.weekday()) % 7)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return start <= now < end


# ---------------------------------------------------------------- grouping

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
        home = next((c for c in clusters if same_story(it["_tok"], c["_tok"])), None)
        if home is None:
            clusters.append({"items": [it], "_tok": set(it["_tok"])})
        else:
            # Skip exact duplicates (same outlet re-listing the same headline/link).
            if any(x["link"] == it["link"] or (x["outlet"] == it["outlet"] and x["title"] == it["title"]) for x in home["items"]):
                continue
            home["items"].append(it)
            home["_tok"] |= it["_tok"]
    return clusters


# ---------------------------------------------------------------- City relevance

# Outlet names contain "Cincinnati"; strip them before asking "is this story about Cincinnati?"
OUTLET_NAMES = re.compile(r"Cincinnati Enquirer|WCPO 9 Cincinnati|WCPO Cincinnati|cincinnati\.com|"
                          r"Cincinnati Public Radio|Signal Cincinnati|Cincinnati Business Courier|CityBeat", re.I)


class Rules:
    """Everything from config.json that scoring needs, compiled once."""

    def __init__(self, cfg, extra_anchors=()):
        self.cfg = cfg
        self.th = cfg["thresholds"]
        self.summary_weight = self.th.get("summary_weight", 0.8)
        self.groups = [{
            "label": g["label"],
            "kind": g.get("kind", "city"),
            "weight": g["weight"],
            "terms": [term_regex(t) for t in g["terms"]],
            "requires": [term_regex(t) for t in g.get("requires_any", [])],
            "national": g.get("national", False),
        } for g in cfg["city_relevance"]]
        self.local_terms = [term_regex(t) for t in cfg["local_terms"]]
        self.ohio_terms = [term_regex(t) for t in cfg.get("ohio_terms", ["Ohio"])]
        self.down_terms = [term_regex(t) for t in cfg.get("downweight_terms", [])]
        self.breaking_terms = [term_regex(t) for t in cfg.get("breaking_terms", [])]
        self.local_outlets = {s["name"] for s in cfg["sources"] if not s.get("sweep") and not s.get("statewide")}
        # Newsrooms that can trigger the breaking-news alert (not the City's own press releases).
        self.alert_outlets = {s["name"] for s in cfg["sources"]
                              if not s.get("sweep") and not s.get("statewide") and not s.get("official")}
        self.anchors = [term_regex(t) for t in list(cfg.get("cincinnati_anchors", ["Cincinnati"])) + list(extra_anchors)]
        self.anchors_i = [re.compile(rx.pattern, re.I) for rx in self.anchors]
        # "Hamilton" the city, not "Hamilton County" (which includes Cincinnati), and not
        # Cincinnati streets named after suburbs (Reading Road, Montgomery Road, Colerain Avenue...).
        # Townships (Delhi, Green, Anderson, Colerain...) are not part of the city, so they DO count as other places.
        tail = r"(?!\s+(County|Road|Rd|Avenue|Ave|Street|St|Pike|Boulevard|Blvd|Parkway))"
        self.others = [re.compile(term_regex(t).pattern + tail, term_regex(t).flags) for t in cfg.get("other_municipalities", [])]
        self.others_i = [re.compile(rx.pattern, re.I) for rx in self.others]


# TV stations start most summaries with "CINCINNATI (WKRC) —" even for suburban stories.
DATELINE = re.compile(r"^\s*[A-Z][A-Z .,'’-]{2,40}(?:\s*\([A-Za-z0-9 .]+\))?\s*[—–:-]+\s*")


def earliest(patterns, text):
    """(position, matched text) of the first match of any pattern, or (None, None)."""
    best = (None, None)
    for rx in patterns:
        m = rx.search(text)
        if m and (best[0] is None or m.start() < best[0]):
            best = (m.start(), m.group(0))
    return best


def place_check(titles, summaries, rules, url_words=""):
    """
    Is the story about the City of Cincinnati?  Returns (verdict, evidence).
      'yes'   - about Cincinnati (a neighborhood, a City official or department...)
      'other' - about a different place (Delhi Township, Hamilton, Covington, Norwood...)
      'maybe' - names no place at all ("city council will hear proposals...")
    The headline decides first; then whichever place the summary mentions first; then the
    story's web address. Datelines like "CINCINNATI (WKRC) —" are ignored.
    """
    for text, ci in ((titles, False), (" ".join(DATELINE.sub("", x) for x in summaries), False), (url_words, True)):
        a_pos, a_hit = earliest(rules.anchors_i if ci else rules.anchors, text)
        o_pos, o_hit = earliest(rules.others_i if ci else rules.others, text)
        if a_pos is None and o_pos is None:
            continue
        if o_pos is None or (a_pos is not None and a_pos <= o_pos):
            return "yes", a_hit
        return "other", o_hit
    return "maybe", None


def item_matches(item, rules):
    """Which City-relevance groups one headline matches, and whether in the headline or only the summary."""
    title = OUTLET_NAMES.sub(" ", item["title"])
    summary = OUTLET_NAMES.sub(" ", item.get("summary", ""))
    both = title + " " + summary
    found = []
    for g in rules.groups:
        if g["requires"] and not first_hit(g["requires"], both):
            continue
        t = first_hit(g["terms"], title)
        if t:
            found.append((g, t, "headline"))
            continue
        s = first_hit(g["terms"], summary)
        if s:
            found.append((g, s, "summary"))
    if item.get("_official") and not any(g["kind"] != "legislation" for g, _, _ in found):
        # The City's own news releases are City news by definition.
        hall = next((g for g in rules.groups if g["label"] == "City Hall"), None)
        if hall:
            found.append((hall, "City of Cincinnati release", "source"))
    return found


def city_relevance(items, rules, is_local, is_ohio, place):
    """
    Score how much a story matters for City work.
      - headline matches count fully, summary-only matches count a bit less (summary_weight)
      - generic words count fully only when the story is clearly about Cincinnati
      - in a story several outlets covered, a lone summary mention doesn't count
    """
    place_factor = {"yes": 1.0, "maybe": 0.4, "other": 0.0}[place]
    per_item = [(it, item_matches(it, rules)) for it in items]
    matched_items = [(it, m) for it, m in per_item if m]
    if len(items) > 1 and len(matched_items) == 1 and all(where == "summary" for _, _, where in matched_items[0][1]):
        return 0.0, [], [{"note": "only one outlet mentions it, and only in the summary", "outlet": matched_items[0][0]["outlet"]}]

    best = {}       # group label -> (weight, term, where, outlet)
    for it, found in matched_items:
        for g, term, where in found:
            w = g["weight"] * (rules.summary_weight if where == "summary" else 1.0)
            if g["kind"] == "legislation":
                if not (is_local or is_ohio or g["national"]):
                    w *= 0.6
            else:
                w *= 1.0 if it.get("_official") else place_factor
                if not is_local:
                    w *= 0.25          # not even a Tri-State story
            if w > 0 and w > best.get(g["label"], (0,))[0]:
                best[g["label"]] = (w, term, where, it["outlet"])
    order = [g["label"] for g in rules.groups]
    labels = sorted(best, key=order.index)
    score = min(sum(best[k][0] for k in labels), 10.0)
    why = [{"group": k, "term": best[k][1], "where": best[k][2], "outlet": best[k][3], "points": round(best[k][0], 2)} for k in labels]
    return score, labels, why


# ---------------------------------------------------------------- memory between runs

def match_memory(tok, memory):
    for m in memory:
        if same_story(tok, set(m["tok"])):
            return m
    return None


def score_clusters(clusters, rules, now, memory=None):
    th = rules.th
    memory = memory or []
    stories = []
    for c in clusters:
        its = c["items"]
        outlets = sorted({i["outlet"] for i in its})
        first = min(i["published"] for i in its)
        latest = max(i["published"] for i in its)
        remembered = match_memory(c["_tok"], memory)
        if remembered:
            # Keep the time we first saw this story, so it can't come back as "new".
            first = min(first, datetime.fromisoformat(remembered["first_seen"]))
        text = OUTLET_NAMES.sub(" ", " ".join(i["title"] + " " + i["summary"] for i in its))
        # Story web addresses often name the place: local12.com/news/local/hamilton-ohio-...
        url_words = " ".join(re.sub(r"[-_/.]+", " ", urlparse(i["link"]).path) for i in its if "news.google.com" not in i["link"])
        place, place_hint = place_check(OUTLET_NAMES.sub(" ", " | ".join(i["title"] for i in its)),
                                        [OUTLET_NAMES.sub(" ", i["summary"]) for i in its], rules, url_words)
        if any(i.get("_official") for i in its):
            place, place_hint = "yes", "City of Cincinnati release"

        is_local = bool(first_hit(rules.local_terms, text)) or any(o in rules.local_outlets for o in outlets)
        is_ohio = bool(first_hit(rules.ohio_terms, text))
        city_score, reasons, why = city_relevance(its, rules, is_local, is_ohio, place)
        is_legislation = any(r in ("State law", "Federal") for r in reasons)
        is_city = city_score >= th.get("city_min_score", 3)

        n = len(outlets)
        age_h = max((now - latest).total_seconds() / 3600, 0)
        recency = math.exp(-age_h / 6)   # a story loses ~half its weight every 4 hours
        down = 0.5 if first_hit(rules.down_terms, text) else 1.0

        # Biggest overall: mostly how many newsrooms have it, and how fresh it is.
        score = (2.5 * n + (1.0 if is_local else -1.0)) * recency * down
        # Biggest for City work: relevance first, coverage second.
        city_rank = (1.6 * city_score + 1.2 * n) * recency * down if is_city else 0.0

        # Breaking news only counts our own newsrooms, so one outlet syndicated through
        # Google News (Yahoo, MSN...) can't inflate the count.
        dev_outlets = sorted(set(outlets) & rules.alert_outlets)
        fresh = (now - first) <= timedelta(hours=th["developing_window_hours"])
        breaking = first_hit(rules.breaking_terms, text)
        developing = is_local and fresh and (
            len(dev_outlets) >= th["developing_outlets"]
            or (bool(breaking) and len(dev_outlets) >= th["big_story_outlets"])
        )

        # Headline to show: prefer a version that carries the City angle, then a local outlet's, then newest.
        city_items = {id(it) for it in its if is_city and item_matches(it, rules)}
        lead = sorted(its, key=lambda i: (id(i) not in city_items, i["outlet"] not in rules.local_outlets,
                                          -i["published"].timestamp()))[0]
        stories.append({
            "id": re.sub(r"[^a-z0-9]+", "-", lead["title"].lower())[:60].strip("-"),
            "title": lead["title"],
            "link": lead["link"],
            "outlet": lead["outlet"],
            "summary": lead["summary"],
            "outlets": outlets,
            # Each outlet's own version of the story, for the clickable outlet labels.
            "links": {o: max((i for i in its if i["outlet"] == o), key=lambda i: i["published"])["link"] for o in outlets},
            "outlet_count": n,
            "first_seen": first.isoformat(),
            "updated": latest.isoformat(),
            "is_city": is_city,
            "city_reasons": reasons if is_city else [],
            "city_score": round(city_score, 2),
            "place": place,
            "is_big": n >= th["big_story_outlets"],
            "is_new": (now - first) <= timedelta(minutes=th["new_minutes"]),
            "developing": developing,
            "score": round(score, 3),
            "city_rank": round(city_rank, 3),
            # Shown only in the ?debug=1 view: why the story scored the way it did.
            "why": {
                "matches": why,
                "place": place, "place_evidence": place_hint,
                "local": is_local, "ohio": is_ohio,
                "breaking_word": breaking, "newsrooms_for_alert": dev_outlets,
                "remembered": bool(remembered), "downweighted": down < 1,
                "versions": [[i["outlet"], i["title"]] for i in its][:8],
            },
            "_tok": sorted(c["_tok"]),
            "_age_h": age_h,
            "_keep": is_local or (is_legislation and is_city),
        })

    lookback = th["lookback_hours"]
    stories = [s for s in stories if s["_age_h"] <= lookback and s["_keep"] and s["score"] > 0]
    stories.sort(key=lambda s: s["score"], reverse=True)
    top = stories[: th["max_stories"]]
    # Never let the overall cut push out City-relevant stories.
    extra_city = [s for s in stories[th["max_stories"]:] if s["is_city"]][:15]
    out = top + extra_city
    for s in out:
        s.pop("_age_h", None)
        s.pop("_keep", None)
    return out


def pick_lead(candidates, rank_key, prev_lead, margin):
    """Top story by rank, but keep the previous lead unless the newcomer clearly beats it."""
    if not candidates:
        return None, "no candidates"
    best = max(candidates, key=lambda s: s[rank_key])
    if prev_lead:
        incumbent = next((s for s in candidates if same_story(set(s["_tok"]), set(prev_lead["tok"]))), None)
        if incumbent and incumbent is not best and best[rank_key] < incumbent[rank_key] * margin:
            return incumbent, f"kept previous lead (challenger {best[rank_key]:.2f} < {margin}× {incumbent[rank_key]:.2f})"
        if incumbent is best:
            return best, "same lead as last run"
    return best, "highest score"


def load_previous(cfg):
    """Last run's output, read back from the live site (or a local copy when testing)."""
    url = cfg.get("site_url")
    if url:
        try:
            return json.loads(fetch(url.rstrip("/") + "/news.json", accept="application/json"))
        except Exception:
            pass
    if OUT_PATH.exists():
        try:
            return json.loads(OUT_PATH.read_text())
        except Exception:
            pass
    return None


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
        return datetime.fromisoformat(iso).strftime("%-I %p")

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


def get_weather(cfg, now):
    loc = cfg["location"]
    point = fetch_json(f"https://api.weather.gov/points/{loc['lat']},{loc['lon']}")
    hourly_raw = fetch_json(point["properties"]["forecastHourly"])["properties"]["periods"]
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
        "ok": True,
        "as_of": now.isoformat(),
        "temp": now_h.get("temp"),
        "short": now_h.get("short", ""),
        "is_day": now_h.get("is_day", True),
        "change": describe_change(hourly, alerts, now_h.get("temp")),
        "alerts": alerts,
    }


# ---------------------------------------------------------------- main

def build(cfg, now=None, previous=None):
    now = now or datetime.now(timezone.utc)
    prev_mem = (previous or {}).get("memory", {})
    th = cfg["thresholds"]

    # City meetings first: committee names become extra proof a story is about Cincinnati.
    try:
        meetings = get_meetings(cfg, now)
    except Exception as ex:
        meetings = {"ok": False, "error": type(ex).__name__}
    bodies = [b for b in meetings.get("bodies", []) if "&" in b or len(b.split()) >= 4]

    items, status = collect_items(cfg, now)
    status.append({"source": "City meetings (Legistar)", "ok": meetings.get("ok", False),
                   "items": len(meetings.get("meetings") or []), "used": meetings.get("used"),
                   "error": meetings.get("error")})
    rules = Rules(cfg, extra_anchors=bodies)
    seen = prev_mem.get("seen", [])
    stories = score_clusters(cluster(items), rules, now, memory=seen)

    try:
        weather = get_weather(cfg, now)
    except Exception as ex:
        old = (previous or {}).get("weather") or {}
        if old.get("ok"):
            weather = dict(old, stale=True)        # keep the last good reading, marked with its time
        else:
            weather = {"ok": False, "error": type(ex).__name__}

    margin = th.get("lead_switch_margin", 1.25)
    prev_lead = prev_mem.get("lead", {})
    dev_list = [s for s in stories if s["developing"]]
    developing = max(dev_list, key=lambda s: (len(s["why"]["newsrooms_for_alert"]), s["first_seen"])) if dev_list else None
    lead_city, why_city = pick_lead([s for s in stories if s["is_city"]], "city_rank", prev_lead.get("city"), margin)
    lead_general, why_general = pick_lead(stories, "score", prev_lead.get("general"), margin)

    # Memory for next run: every story seen in the last ~36 hours.
    keep_after = now - timedelta(hours=36)
    new_seen = [{"tok": s["_tok"], "first_seen": s["first_seen"], "title": s["title"]} for s in stories]
    for m in seen:
        if datetime.fromisoformat(m["first_seen"]) >= keep_after and not any(same_story(set(m["tok"]), set(x["tok"])) for x in new_seen):
            new_seen.append(m)
    memory = {
        "seen": new_seen[:400],
        "lead": {
            "city": {"tok": lead_city["_tok"], "id": lead_city["id"]} if lead_city else None,
            "general": {"tok": lead_general["_tok"], "id": lead_general["id"]} if lead_general else None,
        },
    }
    for s in stories:
        s.pop("_tok", None)

    cutoff = now - timedelta(hours=12)
    today = [s for s in stories if datetime.fromisoformat(s["first_seen"]) >= cutoff]
    return {
        "generated_at": now.isoformat(),
        "weather": weather,
        "meetings": {k: meetings.get(k) for k in ("ok", "day", "is_today", "meetings")},
        "developing": developing["id"] if developing else None,
        "lead": {
            "city": lead_city["id"] if lead_city else None,
            "general": lead_general["id"] if lead_general else None,
            "why": {"city": why_city, "general": why_general},
        },
        "counts": {"stories": len(today), "city": sum(1 for s in today if s["is_city"])},
        "stories": stories,
        "sources_status": status,
        "memory": memory,
    }


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    previous = load_previous(cfg)
    out = build(cfg, previous=previous)
    news_sources = [s for s in out["sources_status"] if not s["source"].startswith("City meetings")]
    ok = sum(1 for s in news_sources if s["ok"])
    if ok == 0 and previous:
        # Every feed failed (network hiccup): keep the last good file instead of blanking the screen.
        out = previous
        print("All news sources failed; republishing the previous news.json")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=1))
    print(f"{len(out['stories'])} stories from {ok}/{len(news_sources)} sources; weather ok={out['weather'].get('ok')}; "
          f"developing={out['developing']}; lead={out['lead']}")
    for s in out["sources_status"]:
        flag = "OK " if s["ok"] else "-- "
        detail = s.get("used") or "; ".join(s.get("errors") or []) or s.get("error") or ""
        print(f"  {flag}{s['source']:<26} {s['items']:>3} items  {detail}")


if __name__ == "__main__":
    sys.exit(main())
