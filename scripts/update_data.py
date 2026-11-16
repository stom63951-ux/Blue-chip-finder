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
DUPLICATE_CLASSES = {"GOOG", "FOX", "NWS"}  # keep one share class per company
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


def first_trade_year(ticker, info):
    """Year the share first traded, from Yahoo; falls back to the full price history."""
    ms = info.get("firstTradeDateMilliseconds")
    if ms:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).year
    secs = info.get("firstTradeDateEpochUtc")
    if secs:
        return dt.datetime.fromtimestamp(secs, dt.timezone.utc).year
    try:
        h = yf.Ticker(ticker).history(period="max", interval="3mo", auto_adjust=False)
        if len(h):
            return int(h.index[0].year)
    except Exception as e:
        print(f"  {ticker}: history lookup failed ({e})")
    return None


def weekly_5y(series, end):
    """Weekly (Friday) closes for the last five years, for the in-page 5Y chart."""
    w = series[series.index > end - pd.DateOffset(years=5)].resample("W-FRI").last().dropna()
    return {"start": w.index[0].strftime("%Y-%m-%d"), "end": w.index[-1].strftime("%Y-%m-%d"),
            "c": [round(float(v), 2) for v in w.values]}


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
    members = [m for m in members if m["t"] not in DUPLICATE_CLASSES]
    tickers = [m["t"] for m in members]
    print(f"S&P 500 members: {len(tickers)}")

    px = yf.download(tickers, period="63mo", interval="1d", auto_adjust=False,
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
            "series": s,
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
        listed = first_trade_year(m["t"], info)
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
            "h5": weekly_5y(p["series"], p["asof"]),
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
        # latest close for every member, so portfolio holdings keep pricing
        # after a share recovers and leaves the dip list
        "prices": {k: round(v["curr"], 2) for k, v in sorted(prices.items())},
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"Wrote {len(out)} candidates to {OUT}")


if __name__ == "__main__":
    main()
