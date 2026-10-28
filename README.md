# Cincy News Glance

A calm, always-on news screen for a spare laptop: the Cincinnati skyline by day, the biggest local story in the sky, and smaller headlines drifting down the Ohio. City Hall stories get a marigold dot. When a big story is breaking, City Hall's clock pulses coral and the horizon warms up a little.

It costs nothing to run: GitHub collects the headlines on a schedule and hosts the page for free.

## How it works (the 30-second version)

```
 news sites' RSS feeds ─┐
 Google News searches ──┼─►  scripts/collect.py  ─►  site/news.json  ─►  site/index.html
 weather.gov (free) ────┘    (runs on GitHub          (one small          (the page on your
                              every 15 minutes)        data file)          laptop reads it)
```

- **RSS feed**: a list of a site's latest headlines that's meant for programs to read. Most local outlets publish one.
- **Why not have the page read the feeds itself?** Browsers block a page from reading other websites' data (a rule called CORS), so a small script fetches everything and saves it as `news.json` next to the page.
- **GitHub Actions**: GitHub's free scheduled computer. The file `.github/workflows/update.yml` tells it to run the script every 15 minutes on weekdays during work hours.
- **GitHub Pages**: GitHub's free web hosting. Your page will live at `https://peterkobak.github.io/cincy-news-glance/`.

## One-time setup

1. **Settings → Pages →** under "Build and deployment", set **Source** to **GitHub Actions**.
2. **Actions tab →** if GitHub asks, click to enable workflows. Pick **Update news → Run workflow**. Takes about a minute.
3. Open `https://peterkobak.github.io/cincy-news-glance/` on the laptop, then press **F11** for full screen.
4. Check what worked: open `…/cincy-news-glance/news.json` and scroll to `sources_status`. Each outlet shows `ok: true/false` and whether it fell back to Google News.

Preview without live data: add `?sample=calm` or `?sample=dev` to the page address.

## Making it yours: `config.json`

Edit it right in GitHub: open the file, click the pencil icon, then **Commit changes**. The page republishes on its own.

| Setting | What it does |
|---|---|
| `sources` | The outlets. Each has direct `feeds` (tried in order) and an optional Google News `fallback`. |
| `city_keywords` | Words that make a story "City Hall". Bigger number = stronger signal. Add names of people or programs you track. |
| `local_terms` | Words that make a story count as local. Stories with none of them are dropped. |
| `downweight_terms` | Topics to push down (sports, by default). |
| `breaking_terms` | Words that help flag breaking news. |
| `thresholds` | `big_story_outlets` (3) = when a story moves up; `developing_outlets` (5) = when the screen gets your attention. |

## How a story is scored

- Headlines about the same event are grouped, even when each outlet words them differently.
- **More newsrooms covering it** counts the most. That's the stand-in for "most read", which feeds don't report.
- **City Hall keywords** add points.
- **Age**: a story loses about half its weight every 4 hours.
- **Developing** = 5+ newsrooms within 3 hours, or 3+ newsrooms on a breaking-type story (fire, closures, outage…).

## Laptop tips

- The page asks the browser to keep the screen awake while it's showing (Chrome or Edge).
- Lower the laptop's brightness. The page is designed to sit quietly at the edge of your vision.
- Hover over the river to pause it. Click any headline to open the story; your Enquirer login works as usual.
- After about 7 PM and on weekends, updates pause. The top-right corner then shows "paused since…".

## Cost and limits

- Public repos get free GitHub Actions minutes and free Pages hosting. This setup uses roughly 1,000 short runs a month.
- Only headlines, links and short summaries are used, which is what RSS is for. No articles are copied.
- The repo is public, so anyone could see the keyword list. Nothing in it is private.
