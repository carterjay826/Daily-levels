#!/usr/bin/env python3
"""Daily levels post -> Discord webhook.

Sections: index/watchlist 9 & 21 EMA levels, sector rotation, earnings,
FOMC / macro calendar, top market news.

Env vars:
  DISCORD_WEBHOOK_URL   (required) webhook for #daily-levels
  FINNHUB_KEY           (optional) free key from finnhub.io, enables earnings
  LOG_WEBHOOK_URL       (optional) webhook for #bot-logs, gets error messages

Run `python daily_levels.py --dry` to print instead of posting.
"""
import os
import sys
import datetime as dt
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

DRY = "--dry" in sys.argv
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
LOG_WEBHOOK = os.environ.get("LOG_WEBHOOK_URL", "")

# ---- edit these -----------------------------------------------------------
INDEXES = ["SPY", "QQQ", "IWM", "DIA"]
WATCHLIST = ["MRVL", "BE", "CRDO", "LITE"]
BIG_EARNINGS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
                "JPM", "NFLX", "AMD", "ORCL", "CRM", "COST", "WMT"]
SECTORS = {
    "XLK": "Tech", "XLC": "Comm", "XLY": "Cons Disc", "XLF": "Financials",
    "XLI": "Industrials", "XLV": "Health", "XLP": "Staples", "XLE": "Energy",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate",
}
# FOMC decision days (second day of each meeting). Verify at federalreserve.gov.
FOMC = ["2026-12-09", "2027-01-27", "2027-03-17"]
# Add CPI, jobs report, etc. as "YYYY-MM-DD": "label"
EXTRA_EVENTS = {"2026-11-25": "PCE inflation 8:30am ET",
    "2026-11-26": "Market closed (Thanksgiving)",
    "2026-11-27": "Early close 1pm ET"}

# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": "Mozilla/5.0"}
ET_TZ = ZoneInfo("America/New_York")


def log_error(msg):
    print(msg, file=sys.stderr)
    if LOG_WEBHOOK and not DRY:
        try:
            requests.post(LOG_WEBHOOK, json={"content": f"daily_levels error: {msg}"[:1900]}, timeout=15)
        except Exception:
            pass


def history(sym, rng="6mo"):
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
        params={"range": rng, "interval": "1d"},
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    q = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
    rows = [(c, h, l) for c, h, l in zip(q["close"], q["high"], q["low"]) if c is not None]
    return rows


def ema(values, n):
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def pct(a, b):
    return (a / b - 1) * 100


def trend(px, e9, e21):
    if px > e9 > e21:
        return "🟢"
    if px < e9 < e21:
        return "🔴"
    return "🟡"


def levels_section():
    lines = []
    for sym in INDEXES + WATCHLIST:
        try:
            rows = history(sym)
            closes = [r[0] for r in rows]
            px, prev = closes[-1], closes[-2]
            hi, lo = rows[-1][1], rows[-1][2]
            e9, e21 = ema(closes, 9), ema(closes, 21)
            lines.append(f"{trend(px, e9, e21)} {sym:<5}{px:>8.2f} {pct(px, prev):+.1f}%")
            lines.append(f"     H {hi:.2f}  L {lo:.2f}")
            lines.append(f"     9EMA {e9:.2f}  21EMA {e21:.2f}")
        except Exception as e:
            log_error(f"levels {sym}: {e}")
    if not lines:
        return None
    body = "```\n" + "\n".join(lines) + "\n```\n🟢 above 9 & 21 (9>21)  🟡 mixed  🔴 below both"
    return {"title": "📊 Levels: last close, prior-day H/L, 9 & 21 EMA", "description": body, "color": 0x2ECC71}


def sectors_section():
    out = []
    for sym, name in SECTORS.items():
        try:
            closes = [r[0] for r in history(sym, "1mo")]
            out.append((name, pct(closes[-1], closes[-2]), pct(closes[-1], closes[-6])))
        except Exception as e:
            log_error(f"sector {sym}: {e}")
    if not out:
        return None
    out.sort(key=lambda x: x[2], reverse=True)
    lines = [f"{n:<12}{d:>+6.1f}% 1d {w:>+6.1f}% 5d" for n, d, w in out]
    body = "```\n" + "\n".join(lines) + "\n```\nSorted by 5-day strength. Leaders up top, laggards at the bottom."
    return {"title": "🔄 Sector rotation", "description": body, "color": 0x3498DB}


def earnings_section(today):
    if not FINNHUB_KEY:
        return None
    tomorrow = today + dt.timedelta(days=1)
    r = requests.get(
        "https://finnhub.io/api/v1/calendar/earnings",
        params={"from": today.isoformat(), "to": tomorrow.isoformat(), "token": FINNHUB_KEY},
        timeout=20,
    )
    r.raise_for_status()
    want = set(BIG_EARNINGS + WATCHLIST)
    hours = {"bmo": "before open", "amc": "after close", "dmh": "during mkt"}
    rows = [e for e in r.json().get("earningsCalendar", []) if e.get("symbol") in want]
    rows.sort(key=lambda e: (e["date"], e["symbol"]))
    if not rows:
        return {"title": "💰 Earnings (today & tomorrow)", "description": "None from the tracked list.", "color": 0xF1C40F}
    lines = [f"**{e['symbol']}** {e['date'][5:]} {hours.get(e.get('hour'), '')}".strip() for e in rows]
    return {"title": "💰 Earnings (today & tomorrow)", "description": "\n".join(lines), "color": 0xF1C40F}


def events_section(today):
    events = {d: "FOMC decision" for d in FOMC}
    events.update(EXTRA_EVENTS)
    upcoming = []
    for d, label in events.items():
        days = (dt.date.fromisoformat(d) - today).days
        if 0 <= days <= 14:
            upcoming.append((days, d, label))
    upcoming.sort()
    if not upcoming:
        nxt = [(dt.date.fromisoformat(d) - today).days for d in FOMC if dt.date.fromisoformat(d) >= today]
        txt = f"Nothing in the next 14 days. Next FOMC decision in {min(nxt)} days." if nxt else "Nothing in the next 14 days."
    else:
        txt = "\n".join(
            f"{'**TODAY**' if n == 0 else f'in {n}d'}: {label} ({d[5:]})" for n, d, label in upcoming
        )
    return {"title": "🏛️ FOMC & macro", "description": txt, "color": 0xE74C3C}


NEWS_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://finance.yahoo.com/news/rssindex",
]


def news_section():
    last_err = None
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            items = ET.fromstring(r.content).findall("./channel/item")[:6]
            lines = []
            for it in items:
                title = (it.findtext("title") or "").replace("[", "(").replace("]", ")")
                link = it.findtext("link") or ""
                if title and link:
                    lines.append(f"\u2022 [{title[:110]}]({link})")
            if lines:
                return {"title": "\U0001F4F0 News to watch", "description": "\n".join(lines), "color": 0x9B59B6}
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return None


def main():
    now = dt.datetime.now(ET_TZ)
    today = now.date()
    embeds = []
    for name, fn in [
        ("news", news_section),
        ("events", lambda: events_section(today)),
        ("earnings", lambda: earnings_section(today)),
        ("sectors", sectors_section),
        ("levels", levels_section),
    ]:
        try:
            e = fn()
            if e:
                embeds.append(e)
        except Exception as ex:
            log_error(f"{name}: {ex}")

    if not embeds:
        log_error("no sections built, nothing posted")
        sys.exit(1)

    payload = {
        "username": "Daily Levels",
        "content": f"**GM. Daily levels for {now.strftime('%a %b %d')}**",
        "embeds": embeds,
    }
    if not DRY and not WEBHOOK.startswith("https://"):
        log_error("DISCORD_WEBHOOK_URL is empty or not a full webhook URL. Check the DAILY_LEVELS_WEBHOOK secret name and value.")
        sys.exit(1)
    if DRY:
        for e in embeds:
            print(f"\n=== {e['title']} ===\n{e['description']}")
        return
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    r.raise_for_status()


if __name__ == "__main__":
    main()
