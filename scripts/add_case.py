#!/usr/bin/env python3
"""
Turns a "wrong call" report (a GitHub issue made by the buttons in the ?debug=1 panel)
into a pending test case in tests/cases.json.

Pending cases don't block publishing. They're the to-fix list: once the rules are tuned
so a pending case passes, it moves into the regular list and becomes part of the safety net.

Used by .github/workflows/cases.yml.  ISSUE_BODY must contain a ```json ...``` block.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "cases.json"
ALLOWED = {"title", "summary", "link", "outlet", "google", "narrow", "official", "expect", "note", "group"}


def main():
    body = os.environ.get("ISSUE_BODY", "")
    m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        print("No case found in the issue")
        return 1
    case = json.loads(m.group(1))
    case = {k: v for k, v in case.items() if k in ALLOWED}           # ignore anything unexpected
    if not isinstance(case.get("title"), str) or not isinstance(case.get("expect"), dict):
        print("Case needs a title and an expect block")
        return 1
    case["expect"] = {k: v for k, v in case["expect"].items() if k in ("city", "junk", "kept", "label", "place")}
    for k in ("title", "summary", "link", "outlet", "note"):
        if k in case:
            case[k] = str(case[k])[:400]

    data = json.loads(CASES.read_text())
    pending = data.setdefault("pending", [])
    known = [(c.get("title"), json.dumps(c.get("expect"), sort_keys=True)) for c in data["headlines"] + pending]
    if (case["title"], json.dumps(case["expect"], sort_keys=True)) in known:
        print("Already on the list")
        return 0
    pending.append(case)
    CASES.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"Added pending case: {case['title']!r} expect {case['expect']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
