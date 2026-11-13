#!/usr/bin/env python3
"""
Checks the scoring rules against tests/cases.json before anything is published.

Each case is a real (or realistic) headline plus what the screen should do with it.
If a config edit breaks one, this exits with an error, GitHub stops the run, and the
live screen keeps its last good version (and GitHub emails you about the failed run).

Run locally:   python scripts/check.py
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_item(c, now):
    title = collect.clean_headline(c["title"])
    return {
        "title": title,
        "summary": c.get("summary", ""),
        "link": c.get("link", "https://example.com/story"),
        "outlet": c.get("outlet", "WCPO"),
        "published": now - timedelta(minutes=30),
        "_google": c.get("google", False),
        "_official": c.get("official", False),
        "_narrow": c.get("narrow", False),
    }


def run_case(case, cfg, rules, now):
    """Returns (label, problems): problems is empty when the screen does what the case expects.
    Also used by scripts/apply_rule.py (rules sent from the review page)."""
    exp = case["expect"]
    parts = case.get("group") or [case]
    items = [make_item(p, now) for p in parts]
    label = (case.get("note") or parts[0]["title"])[:70]
    got = {}

    got["junk"] = any(collect.is_junk(i, cfg) for i in items)
    if not got["junk"]:
        for i in items:
            i["_tok"] = collect.tokens(i["title"])
        clusters = [{"items": items, "_tok": set().union(*(i["_tok"] for i in items))}]
        stories = collect.score_clusters(clusters, rules, now)
        s = stories[0] if stories else None
        got["kept"] = bool(s)
        got["national"] = bool(s and s.get("national"))
        got["city"] = bool(s and s["is_city"])
        got["label"] = (s["city_reasons"][0] if s and s["city_reasons"] else None)
        got["place"] = s["place"] if s else None

    bad = {k: (v, got.get(k)) for k, v in exp.items() if got.get(k) != v}
    return label, ", ".join(f"{k}: expected {v[0]!r}, got {v[1]!r}" for k, v in bad.items())


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    cases = json.loads((ROOT / "tests" / "cases.json").read_text())
    rules = collect.Rules(cfg)
    now = datetime.now(timezone.utc)
    failures = 0
    run_case_here = lambda case: run_case(case, cfg, rules, now)  # noqa: E731

    for case in cases["headlines"]:
        label, problem = run_case_here(case)
        if problem:
            failures += 1
            print(f"FAIL  {label}\n      {problem}")
        else:
            print(f"ok    {label}")

    # Wrong-call reports from the screen: listed, but they never block publishing.
    pending = cases.get("pending", [])
    if pending:
        print(f"\nTo fix ({len(pending)} wrong-call report(s) from the screen; these don't block updates):")
        for case in pending:
            label, problem = run_case_here(case)
            print(f"  {'now passes' if not problem else 'to fix':<10} {label}" + (f"\n             {problem}" if problem else ""))

    for pair in cases.get("same_story", []):
        a = collect.tokens(collect.clean_headline(pair["a"]))
        b = collect.tokens(collect.clean_headline(pair["b"]))
        got = collect.same_story(a, b)
        name = f"same story? {pair['a'][:32]}… / {pair['b'][:32]}…"
        if got != pair["expect"]:
            failures += 1
            print(f"FAIL  {name}\n      expected {pair['expect']}, got {got}")
        else:
            print(f"ok    {name}")

    # Whole batches: which headlines should end up as one story (every other headline stays on its own).
    for case in cases.get("grouping", []):
        items = [{"title": collect.clean_headline(x["title"]), "summary": x.get("summary", ""), "link": x.get("link", ""),
                  "outlet": x.get("outlet", f"o{i}"), "published": datetime.fromisoformat(x["first_seen"]), "_i": i}
                 for i, x in enumerate(case["items"])]
        got = sorted(sorted(it["_i"] for it in c["items"]) for c in collect.cluster(items))
        name = f"grouping: {case.get('note', '')[:60]}…"
        if "apart" in case or "together" in case:   # only some pairs matter
            same = lambda p: any(p[0] in g and p[1] in g for g in got)
            wrong_apart = [p for p in case.get("apart", []) if same(p)]
            wrong_together = [p for p in case.get("together", []) if not same(p)]
            if wrong_apart or wrong_together:
                failures += 1
                print(f"FAIL  {name}\n      should be separate but grouped: {wrong_apart}"
                      f"\n      should be one story but split: {wrong_together}\n      got {got}")
            else:
                print(f"ok    {name}")
            continue
        groups = [sorted(g) for g in case["groups"]]
        grouped = {i for g in groups for i in g}
        expect = sorted(groups + [[i] for i in range(len(items)) if i not in grouped])
        name = f"grouping: {case.get('note', '')[:60]}…"
        if got != expect:
            failures += 1
            print(f"FAIL  {name}\n      expected {expect}\n      got      {got}")
        else:
            print(f"ok    {name}")

    # Old articles Google re-dates as new (checked against the newsroom's own feed).
    for case in cases.get("misdated", []):
        items = [{"title": collect.clean_headline(x["title"]), "outlet": x["outlet"], "_google": x.get("google", False),
                  "published": datetime.fromisoformat(x["published"]), "_i": n} for n, x in enumerate(case["items"])]
        _, dropped = collect.drop_misdated_pickups(items)
        got = sorted(i["_i"] for i in dropped)
        name = f"misdated: {case.get('note', '')[:60]}…"
        if got != sorted(case["dropped"]):
            failures += 1
            print(f"FAIL  {name}\n      expected dropped {case['dropped']}, got {got}")
        else:
            print(f"ok    {name}")

    # The lead shouldn't flip-flop on a near tie.
    a = {"_tok": ["council", "budget", "committee"], "city_rank": 10.0}
    b = {"_tok": ["vice", "mayor", "housing", "ordinance"], "city_rank": 11.0}
    kept, _ = collect.pick_lead([a, b], "city_rank", {"tok": a["_tok"]}, cfg["thresholds"].get("lead_switch_margin", 1.25))
    if kept is not a:
        failures += 1
        print("FAIL  lead should stay put when a new story is only slightly ahead")
    else:
        print("ok    lead stays put on a near tie")

    total = len(cases["headlines"]) + len(cases.get("same_story", [])) + len(cases.get("grouping", [])) + len(cases.get("misdated", [])) + 1
    print(f"\n{total - failures}/{total} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
