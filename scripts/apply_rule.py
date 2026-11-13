#!/usr/bin/env python3
"""
Applies one rule sent from the review page (site/review.html) and decides what to do with it.

The page opens a GitHub issue titled "Rule: …" with a ```json``` block like:
    {"rule": {"kind": "other_town", "value": "Mason"},
     "story": {"title": "...", "summary": "...", "link": "...", "outlet": "WCPO", "google": false}}

kind is one of:
    other_town  add value to other_municipalities              (the story should not be City Hall)
    junk        add value to exclude.title_terms or url_terms  (the story should be dropped)
                ("where": "title_terms" | "url_terms")
    city        add value to a City department's terms         (the story should be City Hall)
                ("where": the department's label, e.g. "Public Services")

What it does (used by .github/workflows/rules.yml):
  1. Builds a test case from the real story and checks it fails with today's rules.
  2. Adds the value to the list in config.json (as text, so one-line lists stay on one line)
     and the test case to tests/cases.json.
  3. Runs scripts/check.py (every existing test must still pass, and so must the new one).
  4. Replays the latest run's articles (items.json from the "data" branch) before and after,
     and lists every story whose City label, place, junk status or grouping changed.
  5. Writes the outcome for the workflow:
        commit  all checks pass and nothing but the flagged story changed → straight to main
        pr      all checks pass, but other stories changed too            → pull request to look at
        none    nothing to apply (already right, already listed, or the rule breaks a check)

    python3 scripts/apply_rule.py ISSUE_NUMBER ITEMS_JSON NEWS_JSON OUT_DIR    (ISSUE_BODY in env)
    python3 scripts/apply_rule.py --replay ITEMS_JSON NEWS_JSON               (prints a JSON snapshot)
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
CASES = ROOT / "tests" / "cases.json"
KINDS = {"other_town", "junk", "city"}
WANT = {"city": "labeled City Hall", "other_town": "free of the City Hall label", "junk": "dropped as junk"}
STORY_KEYS = {"title", "summary", "link", "outlet", "google", "narrow", "official"}


# ---------------------------------------------------------------- reading the request

def parse_request(body):
    m = re.search(r"```json\s*(\{.*\})\s*```", body or "", re.S)
    if not m:
        raise ValueError("No ```json``` block found in the issue.")
    req = json.loads(m.group(1))
    rule, story = req.get("rule") or {}, req.get("story") or {}
    kind = rule.get("kind")
    if kind not in KINDS:
        raise ValueError(f"Unknown rule kind {kind!r}.")
    value = re.sub(r"\s+", " ", str(rule.get("value", ""))).strip()
    if not (2 <= len(value) <= 60):
        raise ValueError("The word or phrase must be 2–60 characters.")
    if not isinstance(story.get("title"), str) or not story["title"].strip():
        raise ValueError("The story's headline is missing.")
    story = {k: v for k, v in story.items() if k in STORY_KEYS}
    for k in ("title", "summary", "link", "outlet"):
        if k in story:
            story[k] = str(story[k])[:400]
    for k in ("google", "narrow", "official"):
        story[k] = bool(story.get(k, False))
    return {"kind": kind, "value": value, "where": str(rule.get("where", "")).strip()}, story


def target(rule, cfg):
    """(description, path into config.json, expect for the test case)."""
    kind, where = rule["kind"], rule["where"]
    if kind == "other_town":
        return "other towns (other_municipalities)", ("other_municipalities",), {"city": False}
    if kind == "junk":
        if where not in ("title_terms", "url_terms"):
            raise ValueError("Junk rules go in title_terms or url_terms.")
        what = "headline/summary words" if where == "title_terms" else "web-address words"
        return f"junk {what} (exclude.{where})", ("exclude", where), {"junk": True}
    labels = [g["label"] for g in cfg["city_relevance"] if g.get("kind", "city") == "city"]
    if where not in labels:
        raise ValueError(f"Unknown City department {where!r}. Choose one of: {', '.join(labels)}.")
    return f"the {where} words", ("city_relevance", where), {"city": True}


def get_list(cfg, path):
    if path[0] == "city_relevance":
        return next(g for g in cfg["city_relevance"] if g["label"] == path[1])["terms"]
    node = cfg
    for k in path:
        node = node[k]
    return node


# ---------------------------------------------------------------- editing files as text

def add_to_config_text(text, path, value):
    """Insert value at the end of a one-line list, leaving every other line as it was."""
    lines = text.split("\n")
    if path[0] == "city_relevance":
        idx = [i for i, ln in enumerate(lines) if f'{{"label": {json.dumps(path[1])},' in ln]
    elif path[0] == "exclude":
        start = next(i for i, ln in enumerate(lines) if ln.strip().startswith('"exclude": {'))
        idx = [next(i for i in range(start, len(lines)) if lines[i].strip().startswith(f'"{path[1]}": ['))]
    else:
        idx = [i for i, ln in enumerate(lines) if ln.strip().startswith(f'"{path[0]}": [')]
    if len(idx) != 1:
        raise ValueError(f"Couldn't find exactly one line for {'.'.join(path)} in config.json.")
    i = idx[0]
    key = "terms" if path[0] == "city_relevance" else path[-1]
    rx = re.compile(rf'("{re.escape(key)}": \[)([^\]]*)(\])')
    m = rx.search(lines[i])
    if not m:
        raise ValueError(f"{'.'.join(path)} isn't a one-line list in config.json; edit it by hand.")
    sep = ", " if m.group(2).strip() else ""
    lines[i] = lines[i][:m.end(2)] + sep + json.dumps(value, ensure_ascii=False) + lines[i][m.end(2):]
    return "\n".join(lines)


def add_case_text(text, case):
    """Append a case to the end of the "headlines" list, leaving the rest of the file untouched."""
    anchor = '\n  }\n ],\n "same_story": ['
    if text.count(anchor) != 1:
        raise ValueError("Couldn't find the end of the headlines list in tests/cases.json.")
    body = "\n".join("  " + ln for ln in json.dumps(case, indent=1).split("\n"))
    return text.replace(anchor, "\n  },\n" + body + '\n ],\n "same_story": [')


# ---------------------------------------------------------------- replay

def replay(items_path, news_path):
    """
    Every article from the latest run with what the current config.json makes of it: dropped as junk,
    kept or left off, City or not, place, and which other headlines it's grouped with. No time cut-off
    or story limit, so a rule's effect shows even on stories that have scrolled off the screen.
    """
    import collect
    cfg = json.loads(CONFIG.read_text())
    news = json.loads(Path(news_path).read_text())
    now = datetime.fromisoformat(news["generated_at"])
    raw = json.loads(Path(items_path).read_text())
    items = [dict(i, published=datetime.fromisoformat(i["published"])) for i in raw]
    out = {}
    key = lambda i: f"{i['outlet']} | {i['title']}"  # noqa: E731
    live = []
    for i in items:
        if collect.is_junk(i, cfg):
            out[key(i)] = {"junk": True}
        else:
            live.append(i)
    cfg["thresholds"] = dict(cfg["thresholds"], lookback_hours=10 ** 6, max_stories=10 ** 6)
    rules = collect.Rules(cfg)
    clusters = collect.cluster(live)
    for c in clusters:
        stories = collect.score_clusters([c], rules, now)
        s = stories[0] if stories else None
        group = sorted({i["title"] for i in c["items"]})
        for i in c["items"]:
            out[key(i)] = {"junk": False, "kept": bool(s),
                           "city": bool(s and s["is_city"]), "labels": (s or {}).get("city_reasons", []),
                           "place": (s or {}).get("place"), "group": group}
    return out


def run_replay(items_path, news_path):
    """In a fresh process: collect.py caches word lists from config.json, so before/after must not share one."""
    r = subprocess.run([sys.executable, __file__, "--replay", str(items_path), str(news_path)],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        raise RuntimeError("Replay failed:\n" + r.stderr[-2000:])
    return json.loads(r.stdout)


def describe(state):
    if not state:
        return "not in this run"
    if state.get("junk"):
        return "dropped as junk"
    if not state.get("kept"):
        return "left off (not local)"
    if state.get("city"):
        return "City Hall (" + ", ".join(state["labels"]) + f"), place: {state['place']}"
    return f"not City, place: {state['place']}"


def impact(before, after, flagged_title):
    """Changes story by story. Returns (flagged changes, other changes) as lists of text lines."""
    flagged_keys = set()
    for k, st in list(before.items()) + list(after.items()):
        if k.split(" | ", 1)[1] == flagged_title or flagged_title in (st.get("group") or []):
            flagged_keys.add(k)
    mine, others = [], []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b == a:
            continue
        lines = []
        if describe(b) != describe(a):
            lines.append(f"{describe(b)} → **{describe(a)}**")
        if (b or {}).get("group") != (a or {}).get("group") and not (a or {}).get("junk"):
            lines.append(f"now grouped with: {', '.join(t for t in (a or {}).get('group', []) if t != k.split(' | ', 1)[1]) or 'nothing (on its own)'}")
        if lines:
            (mine if k in flagged_keys else others).append(f"- {k}: " + "; ".join(lines))
    return mine, others


# ---------------------------------------------------------------- main

def main(issue, items_path, news_path, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    comment = []

    def finish(outcome, extra_commit_msg=""):
        (out_dir / "comment.md").write_text("\n".join(comment) + "\n")
        (out_dir / "outcome").write_text(outcome)
        (out_dir / "commit.txt").write_text(extra_commit_msg)
        print("\n".join(comment))
        return 0

    try:
        rule, story = parse_request(os.environ.get("ISSUE_BODY", ""))
        cfg_text = CONFIG.read_text()
        cfg = json.loads(cfg_text)
        desc, path, expect = target(rule, cfg)
    except Exception as ex:  # bad request: explain and stop
        comment.append(f"Couldn't read this rule: {ex}")
        return finish("none")

    value = rule["value"]
    comment.append(f"**Rule:** add \"{value}\" to {desc}\n**Story:** {story['title']} ({story.get('outlet', '?')})\n")
    if any(value.lower() == t.lower() for t in get_list(cfg, path)):
        comment.append(f"\"{value}\" is already on that list, so nothing changed. If the story is still wrong, "
                       "the list isn't the reason; it needs a closer look.")
        return finish("none")

    import check
    import collect
    case = dict(story, expect=expect,
                note=f"Review page #{issue}: \"{value}\" → {'.'.join(path)}"[:120])
    now = datetime.now().astimezone()
    _, before_problem = check.run_case(case, cfg, collect.Rules(cfg), now)
    if not before_problem:
        comment.append(f"With today's rules this headline is already {WANT[rule['kind']]}, so nothing changed. "
                       "If the screen showed it differently, the difference comes from the other headlines "
                       "grouped with it or from its age; it needs a closer look.")
        return finish("none")

    have_replay = Path(items_path).exists() and Path(news_path).exists()
    before = run_replay(items_path, news_path) if have_replay else {}

    new_cfg_text = add_to_config_text(cfg_text, path, value)
    expected = json.loads(cfg_text)
    get_list(expected, path).append(value)
    if json.loads(new_cfg_text) != expected:
        comment.append("The config.json edit didn't come out as expected, so nothing changed.")
        return finish("none")
    cases_text = CASES.read_text()
    new_cases_text = add_case_text(cases_text, case)
    CONFIG.write_text(new_cfg_text)
    CASES.write_text(new_cases_text)

    chk = subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py")], capture_output=True, text=True, cwd=ROOT)
    fails = [ln for ln in chk.stdout.splitlines() if ln.startswith("FAIL") or ln.startswith("      ")]
    if chk.returncode:
        CONFIG.write_text(cfg_text)
        CASES.write_text(cases_text)
        own = case["note"][:70]
        only_own = all(own in ln for ln in fails if ln.startswith("FAIL"))
        if only_own:
            comment.append(f"**Not applied: the rule isn't enough on its own.** With it, this headline still isn't {WANT[rule['kind']]}.")
            if rule["kind"] == "city":
                g = next(g for g in cfg["city_relevance"] if g["label"] == rule["where"])
                comment.append(f"(Words in {g['label']} count {g['weight']} point(s), and a story needs "
                               f"{cfg['thresholds'].get('city_min_score', 3)} to be City Hall. A department worth more points, "
                               "or a second matching word, would get it there.)")
            else:
                comment.append("It probably needs a different word, or a rule change rather than a list entry.")
        else:
            comment.append("**Not applied: the rule breaks existing checks.** These tests fail with it:\n")
            comment.append("```\n" + "\n".join(fails[:30]) + "\n```")
            comment.append("Try a more specific word or phrase.")
        return finish("none")

    after = run_replay(items_path, news_path) if have_replay else {}
    mine, others = impact(before, after, story["title"]) if have_replay else ([], [])
    comment.append("All checks pass, including a new test made from this headline.\n")
    if have_replay:
        comment.append("**What it changes in the latest run:**")
        comment.extend(mine or ["- (the flagged story isn't in the latest run, so it can't be compared)"])
        if others:
            comment.append(f"\n**It also changes {len(others)} other headline(s):**")
            comment.extend(others[:40])
    else:
        comment.append("(Couldn't replay the latest run, so other stories weren't compared.)")

    msg = (f"Review page: add \"{value}\" to {'.'.join(path)}\n\n"
           f"Flagged on the review page (#{issue}): \"{story['title']}\" ({story.get('outlet', '?')})\n"
           f"should be {WANT[rule['kind']]}; before this rule it wasn't ({before_problem}).\n\n"
           "New test from the real headline; all checks pass.\n")
    if others or not have_replay:
        comment.append("\nBecause it changes more than the story you flagged, it's in a pull request for you to look over.")
        return finish("pr", msg)
    comment.append("\nIt changes only the story you flagged, so it went straight to main. "
                   "The screen picks it up on its next update.")
    return finish("commit", msg)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--replay":
        print(json.dumps(replay(sys.argv[2], sys.argv[3])))
        sys.exit(0)
    sys.exit(main(*sys.argv[1:5]))
