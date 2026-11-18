#!/usr/bin/env python3
"""
Keeps a rolling 30-day archive of the exact articles each run saw, so rule changes can be
replayed against past days (scripts/replay.py) instead of only today's run.

    python scripts/archive.py ITEMS_JSON ARCHIVE_DIR [--at ISO_TIME] [--keep-days 30]

ITEMS_JSON is the run's debug/items.json. The archive is one gzip file per day (Eastern time):
    ARCHIVE_DIR/runs/2026-11-17.json.gz = {"day": ..., "articles": {key: article}, "runs": [{"at": ..., "keys": [...]}]}
Each article is stored once per day; each run lists which articles it had. Files older than
--keep-days are deleted. The update workflow saves ARCHIVE_DIR to the "archive" branch
(one commit, replaced each run), so the repo doesn't grow.
"""
import argparse
import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def article_key(a):
    return hashlib.sha1(f"{a.get('outlet')}|{a.get('title')}|{a.get('link')}".encode()).hexdigest()[:16]


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_gz(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def add_run(items, archive_dir, at, keep_days=30):
    day = at.astimezone(ET).date().isoformat()
    path = Path(archive_dir) / "runs" / f"{day}.json.gz"
    data = read_gz(path) if path.exists() else {"day": day, "articles": {}, "runs": []}
    keys = []
    for a in items:
        k = article_key(a)
        data["articles"].setdefault(k, a)
        keys.append(k)
    data["runs"] = [r for r in data["runs"] if r["at"] != at.isoformat()] + [{"at": at.isoformat(), "keys": keys}]
    write_gz(path, data)

    cutoff = (at.astimezone(ET) - timedelta(days=keep_days)).date().isoformat()
    for old in (Path(archive_dir) / "runs").glob("*.json.gz"):
        if old.name[:10] < cutoff:
            old.unlink()
    return path, len(keys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("items_json")
    ap.add_argument("archive_dir")
    ap.add_argument("--at", help="the run's time (ISO); default: generated_at in site/news.json, else now")
    ap.add_argument("--keep-days", type=int, default=30)
    args = ap.parse_args()
    items = json.loads(Path(args.items_json).read_text())
    if not items:
        print("No articles in this run (feeds failed?): nothing archived.")
        return
    at = None
    if args.at:
        at = datetime.fromisoformat(args.at)
    else:
        try:
            news = json.loads((Path(__file__).resolve().parent.parent / "site" / "news.json").read_text())
            at = datetime.fromisoformat(news["generated_at"])
        except Exception:
            at = datetime.now(timezone.utc)
    path, n = add_run(items, args.archive_dir, at, args.keep_days)
    print(f"archived {n} articles from the {at.isoformat()} run in {path}")


if __name__ == "__main__":
    main()
