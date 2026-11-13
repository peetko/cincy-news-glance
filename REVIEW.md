# Morning review: instructions for the scheduled reviewer

Every weekday morning a scheduled Claude session reviews what Cincy News Glance chose and tunes the
rules. This file is its playbook. Peter (the owner) can edit it any time; the reviewer follows the
latest version on `main`.

## What the display is for

An ambient screen for Peter, who works at the **City of Cincinnati**. It should show the biggest
Cincinnati stories, with **City Hall / local-government relevance first**: the City Manager, Mayor,
Council members, City departments, and state or federal legislation that affects cities. It should be
calm and trustworthy: no junk, no wrong labels, no duplicate entries, no unrelated stories lumped
together. All of it must stay **free** (no paid APIs or services) and use **public news only**. The
repo is public, so never add non-public City information.

## Decisions already made (do not undo)

- Crime, shootings and fire count as City Hall (incident_rule factor 1 is Peter's choice).
- National or other-state policy stories appear only in the river, never as the lead or in the arrows.
- Dropped as junk: obituaries and name-only headlines, press releases, car listings, sportsbook promos,
  the City website's meeting paperwork (dockets, agendas, date-stamped pages), untitled or login pages.
- Other towns (Hamilton, Delhi Township, Norwood…) are not City of Cincinnati.
- City website pages count only from the City's news section (cincinnati-oh.gov/cityofcincinnati/news).
  Pages from elsewhere on the site (events, programs, park and guideline pages, closure notices) are
  informational, not stories, and are dropped by code (`official_news_section_only`). There is no
  whole-site backup; an empty news section is a quiet day, not a failing source. If one of these pages
  shows up anyway, fix it (Peter's call, Nov 5).
- Enquirer comes from its newsroom-only feed. Google News items must name somewhere local.
- Grouping: crime vocabulary, election vocabulary, neighborhood names, and words common in that day's
  headlines never prove two headlines are the same story. Groups join article-to-article, never on
  pooled words. A story's title must be typical of its group.
- In City Hall mode the arrows step through every City Hall story (matching the count); Biggest shows 3.
- A developing story from another town (place "other") leads only in Biggest mode, never in City Hall
  mode (Peter's call, Nov 13).

## Daily procedure

1. **Set up.** The repo is `peetko/cincy-news-glance`. If it isn't in the working directory, attach it
   with push access (the add_repo tool) and clone it. `git pull`. For GitHub API calls use
   `env -u GH_TOKEN gh …` (the plain `GH_TOKEN` in the environment is stale) or `curl` to api.github.com.
2. **Get the latest run.**
   `git fetch origin data && git show origin/data:news.json > /tmp/news.json && git show origin/data:items.json > /tmp/items.json`.
   If `generated_at` is more than 45 minutes old on a weekday after 7 AM Eastern, the outside timer
   (cron-job.org) may have stopped. Report that; don't try to fix the timer.
3. **Read the report.** `python3 scripts/review.py /tmp/news.json /tmp/items.json`. It lists source
   health, junk that was dropped, the leads, every City Hall story with why it matched, the biggest
   stories, every multi-headline group with its headlines, and a replay of the raw articles.
4. **Look at the page.** Copy `/tmp/news.json` into `site/`, serve `site/` locally
   (`python3 -m http.server 8765 -d site`), screenshot it with Playwright at 1600×900, and look at it
   in both modes (click "Biggest"). Delete the copied file afterwards; never commit `site/news.json`.
5. **Judge it** against the checklist below.
6. **Fix** what's wrong, following the tiers below.
7. **Report** (see the end of this file).

## Checklist

- **Wrong City Hall label.** Other towns, county-only items, or national stories labeled City Hall.
  Read each City Hall story's **summary and web address**, not just the headline. A headline can
  say "Greater Cincinnati" or "Cincinnati-area" when the story is about Middletown or Mason (Nov 6).
  Regional phrases go in `regional_phrases`; real City names containing them in `regional_exceptions`.
  Also: is a City story missing its label?
- **Not news.** Any City web page from outside the City's news section (event, program, park, guideline or closure pages),
  ads, promos, listings, obituaries, paperwork. Real news wrongly dropped (check the "Dropped as junk" list).
- **Grouping.** One story split across entries (same event, different wording), or different stories
  merged (look for groups whose headlines are about different people, places or events).
- **Title.** Does the shown headline describe what the group is about?
- **Ranking.** Is the lead in each mode a sensible pick? Is anything stale ("first seen" far older
  than the event)?
- **Sources.** Any source failing several runs in a row?
- **The page.** Anything overlapping, cut off, unreadable, or out of place in the screenshot.

## Fix tiers

**Small fixes: apply directly to `main`**, all of these must hold:
- Only **additions to lists in `config.json`**: exclude `title_terms`, `url_terms`,
  `official_paperwork_terms`, `cincinnati_anchors`, `other_municipalities`, `far_places`,
  `local_terms`, `source_aliases`, `downweight_terms`, or a group's `terms`.
- Each fix comes with **a new test in `tests/cases.json`** built from the real headline. It must
  fail before the fix and pass after.
- `python3 scripts/check.py` passes in full.
- At most **3 small fixes per day**, in **one commit and one push** (two pushes within a few
  minutes make GitHub Pages refuse the second deploy).
- Edit `config.json` as text, so its one-line lists stay on one line; never reformat the file.

**Bigger changes: open a pull request** from a branch named `review/YYYY-MM-DD`, never pushed to main:
- anything in `scripts/`, `site/`, `.github/`, or `README.md`
- any number in `config.json` (thresholds, weights, factors) or removing or renaming any entry
- anything that needs a new rule rather than a new list entry
Same requirements: tests from real headlines, `check.py` passing, and a plain-English PR description
(what was wrong, with the real headlines; what changed; any trade-off).

**Never:**
- delete, weaken, or edit an existing test so the checks pass
- change the schedule, the outside timer, tokens, or the workflow's permissions
- add paid services, API keys, or non-public information
- undo a decision listed above (if one seems wrong, say so in the report instead)
- **If unsure, don't change it. Report it.**

Commit messages explain what was wrong (with the real headline) and what changed. End them with the
attribution lines the session provides. Never start an issue title with "Case:" (that triggers
the wrong-call workflow).

## Report

Finish with a short summary as the final message of the session, written for Peter:
- **Applied:** each small fix in one line, with the headline that prompted it and the commit link.
- **Proposed:** the pull request link and one line on what it changes.
- **Noticed, not changed:** anything worth Peter's judgment.
- If nothing needed changing: one line saying so, plus the two leads.
Keep it brief. Peter reads it on his phone.
