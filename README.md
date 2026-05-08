# MLB Closer Availability Alert (Evening)

Posts a Discord alert listing closers who pitched yesterday and have a game tomorrow,
plus a cascading next-man-up (NMU) chain capped at 2. Mirrors the Cowork SKILL.md
logic for the evening run — DEFINITELY UNAVAILABLE closers are excluded (handled
by the morning alert separately, if/when ported).

## Schedule

GitHub Actions cron triggers every day at **19:00 UTC**, which is **12:00 PT during PDT**
(most of the MLB season). It's also runnable on demand from the Actions tab via
`workflow_dispatch`.

## Setup

### 1. Push this repo to GitHub (public)

```bash
cd closer-alert-gh-actions
git init
git add .
git commit -m "Initial commit"
gh repo create closer-alert --public --source=. --push
```

### 2. Add the Discord webhook as a secret

Repo → Settings → Secrets and variables → Actions → New repository secret

- Name: `DISCORD_WEBHOOK_URL`
- Value: the full webhook URL (`https://discord.com/api/webhooks/…`)

### 3. (Optional) Test it manually

Repo → Actions → "Closer Alert (Evening)" → Run workflow

The job logs print the message that was posted, so you can verify the format.

## Local testing

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...' python closer_alert.py
```

## How it works

1. **Fangraphs scrape** — `closer_alert.py` opens
   `https://www.fangraphs.com/roster-resource/closer-depth-chart` in headless
   Chromium (Playwright) and pulls the embedded `__NEXT_DATA__` JSON, which
   contains the full `dataPlayers` list with each pitcher's `pitcherUsage`
   (one entry per appearance) and a `dateList` (the column dates shown on
   the page).

2. **Appearance grid** — for each pitcher, the script builds a 1/0 list aligned
   to `dateList`. If today's column is present, `startOffset = 1` so today's
   data (which is unreliable mid-day) is skipped, matching the JS logic in the
   Cowork SKILL.md.

3. **MLB schedule** — `statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD`
   is hit directly; no scraping. Postponed/cancelled games are filtered out.

4. **Availability rules** — same as the SKILL:
   - **DEF UNAVAIL**: pitched yesterday + 2 days ago, OR 3+ in last 4 days.
   - **AT RISK**: pitched yesterday, OR 2+ in last 3 days.
   - **NMU chain**: skip IL/Day/DL/Susp + DEF UNAVAIL pitchers; cascade through
     at-risk until first clear; capped at 2 displayed entries.

5. **Discord post** — formatted message via webhook.

## Cloudflare risk

Fangraphs sits behind Cloudflare. GitHub Actions IPs are sometimes flagged.
If you start seeing Playwright failures or empty pages in the logs, options
in escalating order:

- Add `playwright-stealth` for a stronger fingerprint mask.
- Switch to `camoufox` (Firefox-based stealth).
- Use a residential proxy (Bright Data, IPRoyal, etc.) — paid.
- Self-host the runner on a residential IP.

The current setup uses bare Playwright with hand-rolled init scripts that hide
the most obvious automation tells. This is usually enough for a once-daily run,
but is not bulletproof.

## File map

```
.
├── .github/workflows/closer-alert.yml   # Cron + workflow_dispatch
├── closer_alert.py                      # Main script
├── requirements.txt                     # Python deps
└── README.md                            # This file
```
