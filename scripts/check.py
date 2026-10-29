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


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    cases = json.loads((ROOT / "tests" / "cases.json").read_text())
    rules = collect.Rules(cfg)
    now = datetime.now(timezone.utc)
    failures = 0

    def run_case(case):
        """Returns (label, problems): problems is empty when the screen does what the case expects."""
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
            got["city"] = bool(s and s["is_city"])
            got["label"] = (s["city_reasons"][0] if s and s["city_reasons"] else None)
            got["place"] = s["place"] if s else None

        bad = {k: (v, got.get(k)) for k, v in exp.items() if got.get(k) != v}
        return label, ", ".join(f"{k}: expected {v[0]!r}, got {v[1]!r}" for k, v in bad.items())

    for case in cases["headlines"]:
        label, problem = run_case(case)
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
            label, problem = run_case(case)
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

    # The lead shouldn't flip-flop on a near tie.
    a = {"_tok": ["council", "budget", "committee"], "city_rank": 10.0}
    b = {"_tok": ["vice", "mayor", "housing", "ordinance"], "city_rank": 11.0}
    kept, _ = collect.pick_lead([a, b], "city_rank", {"tok": a["_tok"]}, cfg["thresholds"].get("lead_switch_margin", 1.25))
    if kept is not a:
        failures += 1
        print("FAIL  lead should stay put when a new story is only slightly ahead")
    else:
        print("ok    lead stays put on a near tie")

    total = len(cases["headlines"]) + len(cases.get("same_story", [])) + 1
    print(f"\n{total - failures}/{total} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
