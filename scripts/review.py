"""
Morning review report: turns the latest run's results into one readable page for the
daily review (a person or the scheduled Claude reviewer).

    python3 scripts/review.py news.json items.json > report.md

news.json and items.json come from the "data" branch:
    git fetch origin data && git show origin/data:news.json > /tmp/news.json
                          && git show origin/data:items.json > /tmp/items.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

ET = ZoneInfo("America/New_York")


def et(iso):
    return datetime.fromisoformat(iso).astimezone(ET).strftime("%a %b %-d %-I:%M %p")


def main(news_path, items_path=None):
    d = json.loads(Path(news_path).read_text())
    out = []
    p = out.append
    now = datetime.fromisoformat(d["generated_at"])
    p(f"# Review of the {et(d['generated_at'])} run (code {d.get('site_version')})\n")

    # Source health
    bad = [s for s in d["sources_status"] if not s["ok"] or s.get("fail_streak")]
    p("## Sources")
    p("All sources OK." if not bad else "\n".join(
        f"- **{s['source']}**: {'OK now' if s['ok'] else 'FAILING'}, failed {s.get('fail_streak', 0)} run(s) in a row; "
        f"{'; '.join(s.get('errors') or []) or s.get('error') or ''}" for s in bad))
    dropped = [(s["source"], t) for s in d["sources_status"] for t in (s.get("junk_examples") or [])]
    if dropped:
        p("\n**Dropped as junk this run** (check none are real news):")
        p("\n".join(f"- {src}: {t}" for src, t in dropped[:40]))

    # Weather
    w = d.get("weather") or {}
    p("\n## Weather")
    p(f"{w.get('temp')}° {w.get('short')} · H {w.get('high')} / L {w.get('low')} · note: \"{(w.get('change') or {}).get('text')}\""
      if w.get("ok") else f"Weather failed: {w.get('error')}")

    stories = {s["id"]: s for s in d["stories"]}

    def line(s, rank_key):
        age = (now - datetime.fromisoformat(s["first_seen"])).total_seconds() / 3600
        tags = []
        if s["is_city"]:
            tags.append("CITY: " + ", ".join(s["city_reasons"]))
        if s["national"]:
            tags.append("national")
        if s["developing"]:
            tags.append("DEVELOPING")
        return (f"- {s[rank_key]:.1f} · {s['outlet_count']} outlet(s) · first seen {age:.0f}h ago · "
                f"{' · '.join(tags) or 'not City'} — **{s['title']}** ({s['outlet']})")

    # Leads
    p("\n## Leads")
    for mode in ("city", "general"):
        sid = d["lead"].get(mode)
        s = stories.get(sid)
        p(f"- **{mode}**: {s['title'] if s else '(none)'} — why: {d['lead']['why'].get(mode)}")

    # City Hall
    city = sorted([s for s in d["stories"] if s["is_city"]], key=lambda s: -s["city_rank"])
    p(f"\n## City Hall stories ({len(city)}; counts say {d['counts']})")
    for s in city:
        p(line(s, "city_rank"))
        for m in s["why"].get("matches", [])[:4]:
            p(f"    - matched {m['group']} via \"{m['term']}\" in {m['where']} ({m['outlet']}, {m['points']} pts)")
        p(f"    - place: {s['why'].get('place')} ({s['why'].get('place_evidence')})")

    # Biggest
    p("\n## Biggest (top 12 by score)")
    for s in sorted(d["stories"], key=lambda s: -s["score"])[:12]:
        p(line(s, "score"))

    # Groups: every story made of more than one distinct headline
    p("\n## Groups with several headlines (check they're really one story, and the title fits)")
    for s in sorted(d["stories"], key=lambda s: -s["outlet_count"]):
        heads = s["why"].get("versions") or []
        if len({t for _, t in heads}) > 1:
            p(f"- **{s['title']}** — {s['outlet_count']} outlets")
            for o, t in heads[:15]:
                p(f"    - {o}: {t}")

    # Replay the raw articles (exactly what this run grouped) to find big groups that didn't make the screen
    if items_path and Path(items_path).exists():
        raw = json.loads(Path(items_path).read_text())
        items = [dict(i, published=datetime.fromisoformat(i["published"])) for i in raw]
        groups = sorted(collect.cluster(items), key=lambda c: -len(c["items"]))
        p(f"\n## Replay: {len(items)} articles -> {len(groups)} groups; words treated as 'common today': "
          f"{sorted(collect.COMMON_TODAY) or 'none'}")
        p("Largest groups: " + ", ".join(str(len(c["items"])) for c in groups[:10]))
        if groups and len(groups[0]["items"]) >= 20:
            p(f"\n**Warning: one group has {len(groups[0]['items'])} articles — likely a chain of different stories:**")
            for it in groups[0]["items"][:25]:
                p(f"    - {it['outlet']}: {it['title']}")
    print("\n".join(out))


if __name__ == "__main__":
    main(*sys.argv[1:3])
