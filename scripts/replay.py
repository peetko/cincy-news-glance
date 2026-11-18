#!/usr/bin/env python3
"""
Replays past days through the rules, to catch problems before they reach the screen.

    python3 scripts/replay.py --fetch                  # last 30 days with today's rules: things to check
    python3 scripts/replay.py --fetch --compare main   # what this branch's rule change does to the last
                                                       # 30 days, compared with main (for pull requests)
    python3 scripts/replay.py --days 7 --times 8,12,17

Where the past days come from (--fetch downloads both into --dir):
  - the "archive" branch: the exact articles of every real run (scripts/archive.py, kept 30 days);
  - the "archive-backfill" branch: past headlines rebuilt from public archives (scripts/backfill.py),
    used for days the real archive doesn't cover. Backfilled days are an approximation: mostly
    headlines without summaries, so summary-based City labels are tested less well.

For each weekday it replays the screen at a few times (default 8 AM, noon, 5 PM Eastern), in order,
carrying the story memory and leads from one replayed run to the next, as the real runs do.
Not replayed: the City meeting calendar (committee names add Cincinnati proof) and the weather.
"""
import argparse
import gzip
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_DIR = Path(tempfile.gettempdir()) / "cng-archive"

collect = None          # imported in main(), from --code (so a base branch's rules can be replayed)


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def git(*args, cwd=REPO, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def fetch_archives(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for branch in ("archive", "archive-backfill"):
        if git("fetch", "-q", "origin", f"{branch}:refs/remotes/origin/{branch}", check=False).returncode != 0:
            continue
        tar = subprocess.run(["git", "archive", f"origin/{branch}"], cwd=REPO, capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(out_dir)], input=tar.stdout, check=True)
        got.append(branch)
    return got


def load(dir_):
    live = {p.name[:10]: read_gz(p) for p in sorted((dir_ / "runs").glob("*.json.gz"))}
    back = {p.name[:10]: read_gz(p)["articles"] for p in sorted((dir_ / "backfill").glob("*.json.gz"))}
    return live, back


def as_item(a):
    i = dict(a)
    i["published"] = datetime.fromisoformat(a["published"])
    i.setdefault("summary", "")
    i.setdefault("link", "")
    return i


def items_at(t, live, back, all_runs_slack=timedelta(minutes=60)):
    """The articles the screen would have had at time t: a real run's if one is near, else backfill."""
    day = t.date().isoformat()
    if day in live:
        runs = live[day]["runs"]
        near = min(runs, key=lambda r: abs(datetime.fromisoformat(r["at"]) - t), default=None)
        if near and abs(datetime.fromisoformat(near["at"]) - t) <= all_runs_slack:
            arts = live[day]["articles"]
            return [as_item(arts[k]) for k in near["keys"] if k in arts], "live"
    pool = []
    for n in range(3):
        pool += back.get((t.date() - timedelta(days=n)).isoformat(), [])
    if not pool:
        return None, None
    items = [as_item(a) for a in pool]
    items = [i for i in items if t - timedelta(hours=36) < i["published"] <= t]
    return items, "backfill"


# ------------------------------------------------------------------ one replayed run

def replay_run(items, source, t, cfg, rules, state):
    items = [i for i in items if not collect.is_junk(i, cfg)]
    if source == "backfill":
        items, _ = collect.drop_misdated_pickups(items)
    stories = collect.score_clusters(collect.cluster(items), rules, t, memory=state.get("seen", []))
    th = cfg["thresholds"]
    margin = th.get("lead_switch_margin", 1.25)
    leadable = [s for s in stories if not s["national"]]
    prev = state.get("lead", {})
    lead_city, _ = collect.pick_lead([s for s in leadable if s["is_city"]], "city_rank", prev.get("city"), margin)
    lead_gen, _ = collect.pick_lead(leadable, "score", prev.get("general"), margin)

    # Memory for the next replayed run, as build() keeps it.
    keep_after = t - timedelta(hours=36)
    new_seen = [{"tok": s["_tok"], "first_seen": s["first_seen"], "title": s["title"]} for s in stories]
    for m in state.get("seen", []):
        if datetime.fromisoformat(m["first_seen"]) >= keep_after and not any(
                collect.same_story(set(m["tok"]), set(x["tok"])) for x in new_seen):
            new_seen.append(m)
    state["seen"] = new_seen[:400]
    state["lead"] = {"city": {"tok": lead_city["_tok"]} if lead_city else None,
                     "general": {"tok": lead_gen["_tok"]} if lead_gen else None}

    def brief(s):
        return {"title": s["title"], "outlets": s["outlets"], "is_city": s["is_city"], "national": s["national"],
                "label": (s["city_reasons"] or [None])[0], "place": s["place"],
                "first_seen": s["first_seen"], "updated": s["updated"],
                "versions": [f"{o}|{h}" for o, h in s["why"]["versions"]]}

    rec = {"at": t.isoformat(), "source": source, "articles": len(items), "stories": [brief(s) for s in stories],
           "lead_city": brief(lead_city) if lead_city else None, "lead_general": brief(lead_gen) if lead_gen else None}
    rec["flags"] = flags(rec, t)
    return rec


def flags(rec, t):
    """Things a reviewer should look at. Heuristics: a flag is a question, not a verdict."""
    out = []
    tok = lambda v: collect.tokens(v.split("|", 1)[1])          # noqa: E731
    for s in rec["stories"]:
        if s["is_city"] and not s["national"] and s["place"] != "yes":
            out.append({"kind": "city_unsure", "key": s["title"], "story": s["title"], "label": s["label"],
                        "place": s["place"], "outlets": s["outlets"]})
        vs = s["versions"]
        if len(vs) >= 2:
            for n, v in enumerate(vs):
                if len(vs) == 2 and n == 1:
                    break                       # a pair: one flag is enough
                best = max(len(collect.distinctive(tok(v) & tok(w))) for m, w in enumerate(vs) if m != n)
                if best == 0:
                    out.append({"kind": "loose_member", "key": f"{s['title']} || {v}", "story": s["title"], "odd": v,
                                "group": vs[:8]})
    shown = rec["stories"]
    for a_i, a in enumerate(shown):
        for b in shown[a_i + 1:]:
            gap = abs(datetime.fromisoformat(a["updated"]) - datetime.fromisoformat(b["updated"]))
            if gap > timedelta(hours=12):
                continue
            hit = None
            for va in a["versions"]:
                for vb in b["versions"]:
                    ta, tb = tok(va), tok(vb)
                    shared = ta & tb
                    if len(collect.distinctive(shared)) >= 2 and len(shared) / max(1, len(ta | tb)) >= 0.25:
                        hit = (va, vb)
                        break
                if hit:
                    break
            if hit:
                pair = sorted([a["title"], b["title"]])
                out.append({"kind": "maybe_split", "key": " <> ".join(pair), "a": hit[0], "b": hit[1]})
    for mode in ("lead_city", "lead_general"):
        s = rec.get(mode)
        if s and t - datetime.fromisoformat(s["first_seen"]) > timedelta(hours=24):
            out.append({"kind": "stale_lead", "key": f"{mode} {s['title']}", "mode": mode, "story": s["title"],
                        "first_seen": s["first_seen"]})
    return out


def replay(dir_, days, times, now=None):
    cfg = json.loads(collect.CONFIG_PATH.read_text())
    rules = collect.Rules(cfg)
    live, back = load(dir_)
    now = now or datetime.now(ET)
    state, runs = {}, []
    for n in range(days, -1, -1):
        d = (now - timedelta(days=n)).date()
        if d.weekday() >= 5:
            continue
        for h in times:
            t = datetime(d.year, d.month, d.day, h, tzinfo=ET)
            if t > now:
                continue
            items, source = items_at(t, live, back)
            if not items:
                continue
            rec = replay_run(items, source, t, cfg, rules, state)
            rec["key"] = t.strftime("%a %b %-d %-I %p")
            runs.append(rec)
    return {"days": days, "times": times, "live_days": sorted(live), "backfill_days": sorted(back), "runs": runs}


# ------------------------------------------------------------------ reports

def first_seen_list(runs, kind):
    agg = {}
    for r in runs:
        for f in r["flags"]:
            if f["kind"] == kind:
                a = agg.setdefault(f["key"], {"flag": f, "first": r["key"], "runs": 0})
                a["runs"] += 1
    return list(agg.values())


def report(res, limit):
    runs = res["runs"]
    p = []
    if not runs:
        return ("Nothing to replay: no archived days found. Run with --fetch, or check that the "
                "archive and archive-backfill branches exist.")
    src = {s: sum(1 for r in runs if r["source"] == s) for s in ("live", "backfill")}
    p.append(f"# Replay: {len(runs)} screens from {runs[0]['key']} to {runs[-1]['key']} "
             f"({src['live']} from real runs, {src['backfill']} from backfilled headlines)\n")

    def section(kind, title, fmt):
        rows = sorted(first_seen_list(runs, kind), key=lambda a: -a["runs"])
        p.append(f"## {title} ({len(rows)})")
        if not rows:
            p.append("None.")
        for a in rows[:limit]:
            p.append(fmt(a["flag"]) + f"  _(first {a['first']}; {a['runs']} screen(s))_")
        if len(rows) > limit:
            p.append(f"…and {len(rows) - limit} more (raise --max to see them)")
        p.append("")

    section("city_unsure", "City Hall label, but the place isn't confirmed as Cincinnati",
            lambda f: f"- **{f['story']}** — {f['label']}, place: {f['place']} ({', '.join(f['outlets'])})")
    section("maybe_split", "Possibly one story shown as two",
            lambda f: f"- {f['a']}\n  {f['b']}")
    section("loose_member", "Group with a headline that shares no specific word with the rest (different story?)",
            lambda f: f"- **{f['story']}**: odd one: {f['odd']}\n  group: " + " / ".join(f["group"][:5]))
    section("stale_lead", "Lead older than 24 hours",
            lambda f: f"- {'City Hall' if f['mode'] == 'lead_city' else 'Biggest'}: **{f['story']}** (first seen {f['first_seen'][:16]})")

    p.append("## Leads")
    by_day = {}
    for r in runs:
        day = r["key"].rsplit(" ", 2)[0]
        c = (r["lead_city"] or {}).get("title", "—")
        g = (r["lead_general"] or {}).get("title", "—")
        by_day.setdefault(day, []).append((r["key"].split(" ", 3)[3], c, g))
    for day, rows in by_day.items():
        cities = list(dict.fromkeys(c for _, c, _ in rows))
        gens = list(dict.fromkeys(g for _, _, g in rows))
        p.append(f"- **{day}** · City Hall: {' → '.join(cities)} · Biggest: {' → '.join(gens)}")
    return "\n".join(p)


def diff(base, head, limit):
    """What changes on the replayed screens between two versions of the rules."""
    b_runs = {r["key"]: r for r in base["runs"]}
    changes = {}

    def add(kind, key, text, run):
        a = changes.setdefault((kind, key), {"kind": kind, "text": text, "first": run, "runs": 0})
        a["runs"] += 1

    for h in head["runs"]:
        b = b_runs.get(h["key"])
        if not b:
            continue
        old = {frozenset(s["versions"]): s for s in b["stories"]}
        new = {frozenset(s["versions"]): s for s in h["stories"]}
        for members, s in new.items():
            if members in old:
                o = old[members]
                if (o["is_city"], o["label"]) != (s["is_city"], s["label"]):
                    was = f"CITY HALL · {o['label']}" if o["is_city"] else "not City"
                    now = f"CITY HALL · {s['label']}" if s["is_city"] else "not City"
                    add("label", s["title"], f"**{s['title']}**: {was} → {now}", h["key"])
                continue
            parts = [o for m, o in old.items() if m & members]
            if len(parts) >= 2:
                add("joined", s["title"], f"**{s['title']}** now joins: " + " + ".join(f"“{o['title']}”" for o in parts), h["key"])
            elif len(parts) == 1 and members < frozenset(parts[0]["versions"]):
                add("split", parts[0]["title"], f"“{parts[0]['title']}” split; one part is now **{s['title']}**", h["key"])
            elif not parts:
                add("appears", s["title"], f"**{s['title']}** now on screen", h["key"])
        for members, o in old.items():
            if not any(m & members for m in new):
                add("gone", o["title"], f"“{o['title']}” no longer on screen", h["key"])
        for mode, name in (("lead_city", "City Hall lead"), ("lead_general", "Biggest lead")):
            ot, nt = (b.get(mode) or {}).get("title"), (h.get(mode) or {}).get("title")
            if ot != nt:
                add("lead", f"{mode}{h['key']}", f"{h['key']} · {name}: “{ot}” → “{nt}”", h["key"])

    p = [f"# Replay comparison: {len(head['runs'])} screens, {head['runs'][0]['key'] if head['runs'] else ''} "
         f"to {head['runs'][-1]['key'] if head['runs'] else ''}\n"]
    if not changes:
        p.append("No differences: every replayed screen looks the same with both versions of the rules.")
        return "\n".join(p)
    titles = {"joined": "Stories now joined", "split": "Stories now split", "label": "Label changes",
              "lead": "Lead changes", "appears": "Now on screen", "gone": "No longer on screen"}
    for kind, title in titles.items():
        rows = sorted((c for c in changes.values() if c["kind"] == kind), key=lambda c: -c["runs"])
        if not rows:
            continue
        p.append(f"## {title} ({len(rows)})")
        for c in rows[:limit]:
            p.append(f"- {c['text']}" + ("" if kind == "lead" else f"  _(first {c['first']}; {c['runs']} screen(s))_"))
        if len(rows) > limit:
            p.append(f"…and {len(rows) - limit} more")
        p.append("")
    return "\n".join(p)


def main():
    global collect
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="where the archives are (default: %(default)s)")
    ap.add_argument("--fetch", action="store_true", help="download the archive branches into --dir first")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--times", default="8,12,17", help="Eastern hours to replay each weekday")
    ap.add_argument("--compare", metavar="REF", help="also replay REF's rules (e.g. main) and show the differences")
    ap.add_argument("--max", type=int, default=25, help="most rows per section")
    ap.add_argument("--save", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--code", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = ap.parse_args()
    times = [int(h) for h in args.times.split(",")]

    if args.fetch:
        got = fetch_archives(args.dir)
        print(f"Fetched: {', '.join(got) or 'nothing (no archive branches yet)'}", file=sys.stderr)

    if args.compare:
        wt = Path(tempfile.mkdtemp(prefix="cng-base-"))
        git("worktree", "add", "-q", "--detach", str(wt), args.compare)
        try:
            outs = []
            for code in (wt, args.code):
                f = Path(tempfile.mkstemp(suffix=".json")[1])
                subprocess.run([sys.executable, str(Path(__file__).resolve()), "--dir", str(args.dir), "--days",
                                str(args.days), "--times", args.times, "--code", str(code), "--save", str(f)],
                               check=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                outs.append(json.loads(f.read_text()))
                f.unlink()
        finally:
            git("worktree", "remove", "--force", str(wt), check=False)
        print(diff(outs[0], outs[1], args.max))
        return 0

    sys.path.insert(0, str(args.code / "scripts"))
    import collect as _c          # noqa: E402
    collect = _c
    res = replay(args.dir, args.days, times)
    if args.save:
        args.save.write_text(json.dumps(res))
        return 0
    print(report(res, args.max))
    return 0


if __name__ == "__main__":
    sys.exit(main())
