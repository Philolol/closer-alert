"""
MLB Closer Availability Alert - Evening Run

Scrapes the Fangraphs Closer Depth Chart, cross-references the MLB schedule,
and posts an at-risk closer report to a Discord webhook.

Logic mirrors the Cowork SKILL.md: shows closers who pitched yesterday and
have a game tomorrow (AT RISK), with a cascading NMU chain capped at 2.
DEFINITELY UNAVAILABLE closers are excluded (handled by morning alert).
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

FG_URL = "https://www.fangraphs.com/roster-resource/closer-depth-chart"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# MLB Stats API team-name -> Fangraphs abbreviation
MLB_TO_FG = {
    "Arizona Diamondbacks": "ARI",
    "Atlanta Braves": "ATL",
    "Athletics": "ATH",
    "Oakland Athletics": "ATH",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CHW",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KCR",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SDP",
    "San Francisco Giants": "SFG",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TBR",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSN",
}

IL_TOKENS = ("IL", "Day", "DL", "Susp")


def fetch_fangraphs_data():
    """Use Camoufox to load the depth chart and pull __NEXT_DATA__."""
    from camoufox.sync_api import Camoufox

    cf = Camoufox(headless=True, os=("windows", "macos", "linux"))
    browser = cf.__enter__()
    try:
        page = browser.new_page()
        page.goto(FG_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("#__NEXT_DATA__", state="attached", timeout=45_000)
        html = page.content()
    finally:
        try:
            cf.__exit__(None, None, None)
        except Exception:
            pass

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        raise RuntimeError("Could not locate __NEXT_DATA__ on Fangraphs page")
    data = json.loads(m.group(1))
    payload = data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
    return payload["dataPlayers"], payload["dateList"]


def fetch_tomorrow_teams(target_date):
    """Hit the MLB Stats API for tomorrow's schedule. Returns FG abbrev set."""
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date.isoformat()}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    payload = r.json()
    fg_teams = set()
    for d in payload.get("dates", []):
        for g in d.get("games", []):
            status = (g.get("status") or {}).get("detailedState", "")
            if "Postponed" in status or "Cancelled" in status:
                continue
            for side in ("away", "home"):
                name = g["teams"][side]["team"]["name"]
                fg = MLB_TO_FG.get(name)
                if fg:
                    fg_teams.add(fg)
                else:
                    print(f"WARN: unmapped MLB team name: {name!r}", file=sys.stderr)
    return fg_teams


def build_pitcher_rows(players, date_list):
    """Convert raw player dicts into rows with an appearances list (1/0 per column)."""
    rows = []
    seen = set()
    column_dates = [d["gameDate"] for d in date_list]
    for p in players:
        team = p.get("TeamAbbName") or ""
        name = p.get("playerName") or ""
        role = p.get("Role") or ""
        key = (team, name)
        if not team or not name or key in seen:
            continue
        seen.add(key)
        pitched = {u["gameDate"] for u in (p.get("pitcherUsage") or [])}
        appearances = [1 if d in pitched else 0 for d in column_dates]
        rows.append({"team": team, "player": name, "role": role, "appearances": appearances})
    return rows


def detect_start_offset(date_list, today_iso):
    """Match the JS logic: if today's column exists, skip it (index 0 unreliable)."""
    if not date_list:
        return 0
    return 1 if date_list[0]["gameDate"] == today_iso else 0


def is_definitely_unavailable(app, s):
    """Closer DEF UNAVAIL: pitched yesterday + 2 days ago, OR 3+ in last 4 days."""
    y = app[s] if s < len(app) else 0
    d2 = app[s + 1] if s + 1 < len(app) else 0
    d3 = app[s + 2] if s + 2 < len(app) else 0
    d4 = app[s + 3] if s + 3 < len(app) else 0
    return (y == 1 and d2 == 1) or (y + d2 + d3 + d4 >= 3)


def is_at_risk(app, s):
    """Closer AT RISK: pitched yesterday, OR 2+ in last 3 days."""
    y = app[s] if s < len(app) else 0
    d2 = app[s + 1] if s + 1 < len(app) else 0
    d3 = app[s + 2] if s + 2 < len(app) else 0
    return y == 1 or (y + d2 + d3 >= 2)


def rested_days(app, s):
    """Days since last appearance (cap '5+')."""
    for i in range(s, s + 5):
        if i < len(app) and app[i] == 1:
            return i - s
    return "5+"


def build_nmu_chain(roster, start_idx, s):
    """Cascading NMU: skip IL/DEF; cascade through at-risk until first clear."""
    chain = []
    for i in range(start_idx, len(roster)):
        p = roster[i]
        role = p["role"] or ""
        if any(t in role for t in IL_TOKENS):
            continue
        if is_definitely_unavailable(p["appearances"], s):
            continue
        risk = is_at_risk(p["appearances"], s)
        chain.append({"player": p["player"], "rested": rested_days(p["appearances"], s), "at_risk": risk})
        if not risk:
            break
    return chain


def build_alerts(rows, teams_with_games, s):
    by_team = {}
    for r in rows:
        by_team.setdefault(r["team"], []).append(r)

    alerts = []
    for team, roster in by_team.items():
        ci = next((i for i, p in enumerate(roster) if p["role"] == "Closer"), -1)
        if ci < 0:
            continue
        closer = roster[ci]
        if is_definitely_unavailable(closer["appearances"], s):
            continue
        if not is_at_risk(closer["appearances"], s):
            continue
        if team not in teams_with_games:
            continue
        alerts.append(
            {
                "team": team,
                "closer": closer["player"],
                "nmu_chain": build_nmu_chain(roster, ci + 1, s),
            }
        )
    return alerts


def format_message(alerts, header_date_str):
    if not alerts:
        return (
            f"\U0001F319 Closer Alert — {header_date_str}\n"
            f"✅ No closers at risk for tomorrow. All clear."
        )

    lines = [
        f"\U0001F319 Closer Alert — {header_date_str}",
        f"⏰ If your closer pitched today, add the ✅ before 11:59 PM PST",
        "",
    ]
    for a in alerts:
        display = a["nmu_chain"][:2]
        parts = []
        for p in display:
            if p["at_risk"]:
                parts.append(f"⚠️ {p['player']} ({p['rested']}d)")
            else:
                parts.append(f"{p['player']} ({p['rested']}d) ✅")
        line = f"**{a['team']}** {a['closer']}"
        if parts:
            line += " → " + " → ".join(parts)
        lines.append(line)
    return "\n".join(lines)


def post_to_discord(message):
    if not DISCORD_WEBHOOK:
        print("ERROR: DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        sys.exit(2)
    chunks = []
    while len(message) > 2000:
        cut = message.rfind("\n", 0, 1990)
        if cut <= 0:
            cut = 1990
        chunks.append(message[:cut])
        message = message[cut:].lstrip("\n")
    chunks.append(message)
    for c in chunks:
        r = requests.post(DISCORD_WEBHOOK, json={"content": c}, timeout=20)
        if r.status_code >= 300:
            print(f"Discord POST failed: {r.status_code} {r.text}", file=sys.stderr)
            sys.exit(3)


def _format_header_date(d):
    # Cross-platform "May 6, 2026" without leading zero on day
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def main():
    pt_now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = pt_now.date()
    tomorrow = today + timedelta(days=1)
    header_date = _format_header_date(today)

    print(f"Run start: PT={pt_now.isoformat()}  today={today}  tomorrow={tomorrow}")

    players, date_list = fetch_fangraphs_data()
    print(f"Fetched Fangraphs: {len(players)} players, dateList={[d['gameDate'] for d in date_list]}")

    s = detect_start_offset(date_list, today.isoformat())
    print(f"startOffset={s} (today col {'present' if s == 1 else 'absent'})")

    rows = build_pitcher_rows(players, date_list)
    teams_with_games = fetch_tomorrow_teams(tomorrow)
    print(f"Teams with games on {tomorrow}: {sorted(teams_with_games)}")

    alerts = build_alerts(rows, teams_with_games, s)
    print(f"AT RISK closers: {len(alerts)}")
    for a in alerts:
        print(" ", a["team"], a["closer"], "->",
              [(p["player"], p["rested"], "AR" if p["at_risk"] else "CL") for p in a["nmu_chain"][:2]])

    message = format_message(alerts, header_date)
    print("---MESSAGE---")
    print(message)
    print("---/MESSAGE---")

    post_to_discord(message)
    print("Discord posted OK")


if __name__ == "__main__":
    main()
