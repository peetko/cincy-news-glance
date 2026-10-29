# Cincy News Glance

A calm, always-on news screen for a spare laptop: the Cincinnati skyline by day, the lead story in the sky, and smaller headlines drifting down the Ohio. City Hall stories get a marigold dot. When a big story is breaking, City Hall's clock pulses coral and the horizon warms up a little.

It costs nothing to run: GitHub collects the headlines on a schedule and hosts the page for free.

## How it works (the 30-second version)

```
 news sites' RSS feeds ──┐
 Google News searches ───┤                           tests/cases.json
 City meeting calendar ──┼─►  scripts/collect.py  ◄── checked first by
 weather.gov (free) ─────┤    (runs on GitHub          scripts/check.py
 last run's news.json ───┘     every 15 minutes)
                                     │
                                     ▼
                              site/news.json  ─►  site/index.html (the page on your laptop)
```

- **RSS feed**: a list of a site's latest headlines that's meant for programs to read. Most local outlets publish one.
- **Why not have the page read the feeds itself?** Browsers block a page from reading other websites' data (a rule called CORS), so a small script fetches everything and saves it as `news.json` next to the page.
- **GitHub Actions**: GitHub's free scheduled computer. The file `.github/workflows/update.yml` tells it to run the script every 15 minutes on weekdays during work hours.
- **GitHub Pages**: GitHub's free web hosting. Your page will live at `https://peetko.github.io/cincy-news-glance/`.

## One-time setup

1. **Settings → Pages →** under "Build and deployment", set **Source** to **GitHub Actions**.
2. **Actions tab →** if GitHub asks, click to enable workflows. Pick **Update news → Run workflow**. Takes about a minute.
3. Open `https://peetko.github.io/cincy-news-glance/` on the laptop, then press **F11** for full screen.
4. Check what worked: open `…/cincy-news-glance/news.json` and scroll to `sources_status`. Each outlet shows `ok: true/false` and whether it fell back to Google News.

Preview without live data: add `?sample=calm` or `?sample=dev` to the page address.

## Reliable updates: the outside timer (one-time, ~10 minutes)

GitHub's built-in schedule often runs late or skips runs entirely. A free timer at cron-job.org fixes that by telling GitHub "run now" every 15 minutes.

**1. Make a key that can only start this one workflow**
- GitHub → your picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
- Name: `news glance timer`. Expiration: the longest offered (set a reminder to renew it).
- Repository access: **Only select repositories → cincy-news-glance**.
- Permissions → Repository permissions → **Actions: Read and write**. Nothing else.
- Generate, then copy the token (it starts with `github_pat_`). You won't see it again.

**2. Set up the timer**
- Make a free account at cron-job.org → **Create cronjob**.
- URL: `https://api.github.com/repos/peetko/cincy-news-glance/actions/workflows/update.yml/dispatches`
- Schedule: **Custom** → every 15 minutes, hours 7–19, Monday–Friday, time zone **America/New_York**.
- **Advanced** tab:
  - Request method: **POST**
  - Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <your token>`, `X-GitHub-Api-Version: 2022-11-28`
  - Request body: `{"ref":"main"}`
- Save, then **Test run**. A response of **204** means it worked; check the Actions tab for a new run.

The key can only start this workflow, in this one repo: it can't read, change or delete anything else.

## Using the screen

- **The river:** hover to pause it. Drag it sideways or scroll it (mouse wheel or a two-finger swipe) to browse every story; a quick flick keeps it gliding. Click a headline to open it.
- **Today at City Hall:** under the lead story, today's council and committee meetings from the City's Legistar calendar, linked to the agenda when one is posted. If nothing is scheduled today, it shows the next meeting day.
- **Breaking news:** when a story is developing, City Hall's clock and the story's dot pulse coral. Click the "DEVELOPING" label to mark it seen: the pulsing stops and the story stays in the lead.
- **Staying current:** the open page checks for new headlines every 5 minutes, so you never need to refresh. When a new version of the page itself is published, it reloads itself once.
- **Weather:** if the weather service is down, the screen keeps the last reading and says "as of 2:15 PM".
- **Weather in the scene:** the background quietly follows the forecast: a breathing sun with faint rays when sunny, extra clouds when partly cloudy, a greyer sky when overcast, fine rain behind the hills with ripples on the river, a rare soft flash in storms, slow snowflakes, or haze drifting over the hills in fog. Preview any of them by adding `&weather=rain` (or `sunny`, `partly`, `cloudy`, `storm`, `snow`, `fog`) to the address.
- **Outlet labels:** under the lead story, each outlet that covered it is a link to that outlet's own version. A dashed label with a coral dot means that outlet's feed has failed 3+ updates in a row.
- **Time of day follows the real sun over Cincinnati** (calculated in the page, no outside data): dawn colors around sunrise, the sun sitting as high as it really is, dusk from an hour before sunset to half an hour after, then evening with a small moon. So in January it's dusk by about 4:30 PM; in June it stays daytime past 8. The why-panel shows today's sunrise and sunset. Preview with `&phase=dusk` or `&phase=evening`.
- **The river** drifts slower on quiet news days and a little quicker on busy ones. Older headlines fade slightly, so new ones stand out.
- **Games:** the top right lists Bengals, Reds and FC Cincinnati games in the next 5 days: day, opponent, home (vs., with a solid dot in the team's color) or away (at, with a hollow ring), and start time; live games show the score. A team that played in the last 3 days has its stadium (Paycor, Great American Ball Park, TQL) glowing in its colors; hover over it for the result. Home games also light the stadium from two hours before the start. Preview with `&preview=game`. Data: ESPN's public scoreboards.
- **Boats:** every 6–14 minutes a paddlewheeler or a towboat with barges crosses the river. Preview with `&boats=now`.
- **Timer reminder:** in the last 30 days before the cron-job.org token expires (`timer_token_expires` in `config.json`), a note appears in the top right.

## The City Hall / Biggest switch

Top right of the screen. It starts on **City Hall**, and the browser remembers your choice.

- **City Hall** leads with the biggest story for City work. The label says why, e.g. `CITY HALL · CITY MANAGER`, `CITY HALL · POLICE` or `CITY HALL · STATE LAW`.
- **Biggest** leads with the most-covered local story overall, whatever it's about.
- A **developing** story takes the lead in either mode.
- **‹ 1 / 3 ›** next to the label flips through the top three stories for the current mode (a breaking story stays #1). It returns to #1 after two minutes without a click.
- If there's no City news yet that day, City Hall mode shows the biggest story and says "no City Hall news yet".

## Making it yours: `config.json`

Edit it right in GitHub: open the file, click the pencil icon, then **Commit changes**. The page republishes on its own.

| Setting | What it does |
|---|---|
| `sources` | The outlets. Each has direct `feeds` (tried in order) and an optional Google News `fallback`. Statewide sources (Ohio Capital Journal and two Google News sweeps for state and federal legislation) only add stories that are local or City-relevant. National or other-state policy stories (e.g. a HUD rule from a national site) appear in the river with the City Hall icon but never lead. |
| `city_relevance` | What counts for City work, in labeled groups: City Manager, City staff, Mayor, Council, departments (Police, Fire, Buildings & Inspections, DOTE, Water Works…), and state or federal legislation that touches local government. Each group has a `weight`. Update names when people change roles. |
| `local_terms` | Words that make a story count as local. Stories with none of them are dropped. |
| `cincinnati_anchors` | Proof a story is about the City of Cincinnati: "Cincinnati", its neighborhoods, City officials' names, CPD, GCWW… Words like "city council" only count fully when one of these appears (in the text or the story's web address). |
| `other_municipalities` | Other cities and towns (Hamilton, Norwood, Covington…). A council or police story naming one of these, and not Cincinnati, gets no City credit. Streets like Reading Road or Colerain Avenue don't count as other towns. |
| `site_url` | The live page's address, used to read last run's results. |
| `legistar` | The City's meeting calendar. Committee names from it also count as proof a story is about Cincinnati. |
| `exclude` | Dropped entirely: obituaries, death notices, and Google News results whose headline is only a person's name. |
| `downweight_terms` | Topics to push down (sports, by default). |
| `breaking_terms` | Words that help flag breaking news. |
| `thresholds` | `big_story_outlets` (3) = when a story moves up; `developing_outlets` (5) = when the screen gets your attention. |

## When it picks something wrong

1. **Click the crown of the Great American Tower** in the skyline (or add `?debug=1` to the address). A panel lists every story with its points, the exact words that matched (and whether they were in the headline or only the summary), which place it thinks the story is about and why, and which headlines were grouped together. It also shows each source's status.
2. Fix the cause in `config.json`: add a town to `other_municipalities`, a name to `cincinnati_anchors`, a word to `exclude`, and so on.
3. Or skip steps 2–3: under each story in the panel, click **Should be City Hall**, **Not City Hall** or **Not news (junk)**. That opens a ready-made GitHub issue; click **Create** and a workflow adds it to the to-fix list (`pending` in `tests/cases.json`) and closes the issue. Pending cases never block updates. Once the rules are tuned so one passes, it moves into the regular list and becomes part of the safety net.

**The safety net:** before every publish, GitHub runs `scripts/check.py`, which checks every case in `tests/cases.json`. If an edit breaks one, the run stops, the screen keeps its last good version, and GitHub emails you about the failed run. Click into the failed run on the Actions tab to see which case failed and why.

## How a story is scored

- Headlines about the same event are grouped, even when each outlet words them differently.
- **More newsrooms covering it** counts the most. That's the stand-in for "most read", which feeds don't report.
- **City relevance** is scored separately. The City Manager counts most, then the mayor and council, city staff, and departments. State or federal bills count only when they mention cities, local government or home rule. Police, mayors and councils from other towns barely count.
- **Age**: a story loses about half its weight every 4 hours.
- **Headline vs. summary:** words in the headline count fully; words only in the summary count half. If several outlets covered a story and only one mentions a City term, only in its summary, the story gets no City credit.
- **Developing** = 5+ of our own newsrooms within 3 hours, or 3+ on a breaking-type story (fire, closures, outage…). Copies of a story through Google News (Yahoo, MSN…) and City press releases don't count toward the alert.
- **Memory:** each run reads the previous `news.json` from the live site. A story keeps the time it was *first* seen, so it can't come back as "NEW". The lead only changes when a new story beats it by 25% (`lead_switch_margin`), so the screen doesn't flip back and forth between two close stories.

## Laptop tips

- The page asks the browser to keep the screen awake while it's showing (Chrome or Edge).
- Lower the laptop's brightness. The page is designed to sit quietly at the edge of your vision.
- Hover over the river to pause it. Click any headline to open the story; your Enquirer login works as usual.
- After about 7 PM and on weekends, updates pause. The top-right corner then shows "last update …".

## Cost and limits

- Public repos get free GitHub Actions minutes and free Pages hosting. This setup uses roughly 1,000 short runs a month.
- Only headlines, links and short summaries are used, which is what RSS is for. No articles are copied.
- The repo is public, so anyone could see the keyword list. Nothing in it is private.
