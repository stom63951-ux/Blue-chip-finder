"""Daily refresh for the Blue-Chip Dip Finder.

Screens every S&P 500 company, keeps those that fell 10%+ over the last
3 months, pulls fundamentals for them, and writes data.json for the page.
Runs in GitHub Actions (see .github/workflows/update-data.yml).
"""
import datetime as dt
import io
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data.json"
CHINA = json.loads((ROOT / "scripts" / "china_exclusions.json").read_text())["tickers"]
CHINA_COUNTRIES = {"china", "hong kong", "macau", "macao"}
MIN_DROP = -10.0  # % over 3 months; the page slider starts at 10%
UA = {"User-Agent": "Mozilla/5.0 (blue-chip-finder data refresh)"}


def sp500():
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=60).text
    t = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    rows = []
    for _, r in t.iterrows():
        rows.append({
            "t": str(r["Symbol"]).strip().replace(".", "-"),
            "name": str(r["Security"]).strip(),
            "sector": str(r["GICS Sector"]).strip(),
            "industry": str(r.get("GICS Sub-Industry", "")).strip(),
            "hq": str(r.get("Headquarters Location", "")).strip(),
            "founded": str(r.get("Founded", "")).strip(),
        })
    return rows


def close_on_or_before(series, when):
    s = series.loc[:pd.Timestamp(when)]
    return float(s.iloc[-1]) if len(s) else None


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def stats(info, curr):
    B = 1e9
    g = lambda k: num(info.get(k))
    div_rate = g("dividendRate")
    ebitda, debt = g("ebitda"), g("totalDebt")
    pct = lambda v: None if v is None else round(v * 100, 2)
    bn = lambda v: None if v is None else round(v / B, 3)
    return {
        "mcap": bn(g("marketCap")), "pe": g("trailingPE"), "fpe": g("forwardPE"),
        "rev": bn(g("totalRevenue")), "ebitda": bn(ebitda), "ni": bn(g("netIncomeToCommon")),
        "pm": pct(g("profitMargins")), "om": pct(g("operatingMargins")), "eps": g("trailingEps"),
        "fcf": bn(g("freeCashflow")), "debt": bn(debt), "cash": bn(g("totalCash")),
        "debitda": round(debt / ebitda, 2) if debt is not None and ebitda and ebitda > 0 else None,
        "roe": pct(g("returnOnEquity")), "cr": g("currentRatio"),
        "div": round(div_rate / curr * 100, 2) if div_rate and curr else 0,
        "pt": g("targetMeanPrice"), "cons": (info.get("recommendationKey") or "").replace("_", " ").title() or None,
        "analysts": g("numberOfAnalystOpinions"),
    }


def main():
    members = sp500()
    tickers = [m["t"] for m in members]
    print(f"S&P 500 members: {len(tickers)}")

    px = yf.download(tickers, period="27mo", interval="1d", auto_adjust=False,
                     group_by="ticker", threads=True, progress=False)
    last_dates = []
    prices = {}
    for t in tickers:
        try:
            s = px[t]["Close"].dropna()
        except KeyError:
            continue
        if len(s) < 60:
            continue
        end = s.index[-1]
        last_dates.append(end)
        prices[t] = {
            "asof": end,
            "curr": float(s.iloc[-1]),
            "p1m": close_on_or_before(s, end - pd.DateOffset(months=1)),
            "p3m": close_on_or_before(s, end - pd.DateOffset(months=3)),
            "p1y": close_on_or_before(s, end - pd.DateOffset(years=1)),
            "p2y": close_on_or_before(s, end - pd.DateOffset(years=2)),
            "first": s.index[0],
        }
    if not prices:
        sys.exit("No price data downloaded")
    asof = max(last_dates)
    print(f"Prices for {len(prices)} tickers, as of {asof.date()}")

    out = []
    for m in members:
        p = prices.get(m["t"])
        if not p or not p["p3m"]:
            continue
        pct3m = (p["curr"] / p["p3m"] - 1) * 100
        if pct3m > MIN_DROP:
            continue
        info = {}
        for attempt in range(3):
            try:
                info = yf.Ticker(m["t"]).info or {}
                break
            except Exception as e:  # rate limits etc.
                print(f"  {m['t']}: info failed ({e}); retrying")
                time.sleep(5 * (attempt + 1))
        time.sleep(0.6)
        first_trade = info.get("firstTradeDateEpochUtc")
        listed = dt.datetime.utcfromtimestamp(first_trade).year if first_trade else None
        country = (info.get("country") or "").strip()
        china_reason = CHINA.get(m["t"])
        if not china_reason and country.lower() in CHINA_COUNTRIES:
            china_reason = f"Headquartered in {country}"
        web = (info.get("website") or "").strip()
        rec = {
            "t": m["t"], "name": info.get("longName") or m["name"], "sector": m["sector"],
            "industry": info.get("industry") or m["industry"], "hq": m["hq"], "country": country,
            "url": web, "listed": listed,
            "pct3m": round(pct3m, 2), "curr": round(p["curr"], 2),
            "p1m": round(p["p1m"], 2) if p["p1m"] else None,
            "p3m": round(p["p3m"], 2) if p["p3m"] else None,
            "p1y": round(p["p1y"], 2) if p["p1y"] else None,
            "p2y": round(p["p2y"], 2) if p["p2y"] else None,
            "china": china_reason,
            "stats": stats(info, p["curr"]),
        }
        out.append(rec)
        print(f"  {m['t']:6} {pct3m:7.1f}%  {'CHINA' if china_reason else ''}")

    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "asof": asof.strftime("%Y-%m-%d"),
        "universe": len(prices),
        "minDrop": abs(MIN_DROP),
        "source": "Yahoo Finance via yfinance (prices, fundamentals); Wikipedia (S&P 500 list)",
        "stocks": sorted(out, key=lambda r: r["pct3m"]),
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"Wrote {len(out)} candidates to {OUT}")


if __name__ == "__main__":
    main()
