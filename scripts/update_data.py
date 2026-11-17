"""Daily refresh for the Blue-Chip Dip Finder.

Screens every member of the S&P 500, Nasdaq 100, Dow Jones, FTSE 100,
Euro Stoxx 50, Nifty 50 and ASX 200, keeps those that fell 10%+ over the last
3 months, pulls fundamentals for them, and writes data.json for the page.
Prices stay in each share's own currency (London prices are turned from pence
into pounds); company money figures are converted to US dollars so the
US$20 billion size test means the same thing everywhere.
Runs in GitHub Actions (see .github/workflows/update-data.yml).
"""
import datetime as dt
import io
import json
import math
import re
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
MIN_COVERAGE = 0.9   # stop if fewer than 90% of members have prices (keeps yesterday's file)
MAX_INFO_MISSES = 0.25  # stop if more than a quarter of the fallers have no fundamentals
OTHER_COVERAGE = 0.8  # an index other than the S&P 500 is skipped for the day below this

# The S&P 500 comes first and is required. The others are skipped for the day (yesterday's
# figures are kept) if their member list or prices cannot be read.
INDEXES = [
    {"id": "sp500", "name": "S&P 500", "yahoo": "^GSPC", "ccy": "USD"},
    {"id": "ndx", "name": "Nasdaq 100", "yahoo": "^NDX", "ccy": "USD", "page": ["Nasdaq-100", "List_of_Nasdaq-100_companies", "Nasdaq-100_Index"], "suffix": "", "rows": (90, 115)},
    {"id": "dow", "name": "Dow Jones", "yahoo": "^DJI", "ccy": "USD", "page": ["Dow_Jones_Industrial_Average", "List_of_Dow_Jones_Industrial_Average_companies", "Components_of_the_Dow_Jones_Industrial_Average"], "suffix": "", "rows": (28, 32)},
    {"id": "ftse", "name": "FTSE 100", "yahoo": "^FTSE", "ccy": "GBP", "page": "FTSE_100_Index", "suffix": ".L", "rows": (95, 105)},
    {"id": "sx5e", "name": "Euro Stoxx 50", "yahoo": "^STOXX50E", "ccy": "EUR", "page": "EURO_STOXX_50", "suffix": None, "rows": (48, 52)},
    {"id": "nifty", "name": "Nifty 50", "yahoo": "^NSEI", "ccy": "INR", "page": "NIFTY_50", "suffix": ".NS", "rows": (48, 52)},
    {"id": "asx", "name": "ASX 200", "yahoo": "^AXJO", "ccy": "AUD", "page": "S%26P/ASX_200", "suffix": ".AX", "rows": (180, 210)},
]
SUFFIX_CCY = {"L": "GBP", "NS": "INR", "BO": "INR", "AX": "AUD", "DE": "EUR", "PA": "EUR", "AS": "EUR", "MI": "EUR",
              "MC": "EUR", "BR": "EUR", "HE": "EUR", "IR": "EUR", "VI": "EUR", "LS": "EUR", "F": "EUR"}
FX_FALLBACK = {"GBP": 1.3, "EUR": 1.1, "INR": 0.0115, "AUD": 0.65}  # only if Yahoo and yesterday's file both fail
# Yahoo's sector names, mapped to the GICS names the S&P 500 list uses
SECTORS = {"Technology": "Information Technology", "Financial Services": "Financials", "Consumer Cyclical": "Consumer Discretionary",
           "Consumer Defensive": "Consumer Staples", "Healthcare": "Health Care", "Basic Materials": "Materials"}
TICKER_COLS = {"ticker", "symbol", "code", "ticker symbol", "epic", "asx code"}
NAME_COLS = {"company", "security", "name", "company name", "constituent", "company name[a]"}


def ccy_of(ticker):
    m = re.search(r"\.([A-Z]+)$", ticker)
    return SUFFIX_CCY.get(m.group(1), "USD") if m else "USD"


def wiki_members(ix):
    """Members of an index from Wikipedia, trying each page it might be listed on."""
    pages = ix["page"] if isinstance(ix["page"], list) else [ix["page"]]
    errors = []
    for page in pages:
        try:
            return wiki_table(ix, page)
        except Exception as e:
            errors.append(f"{page}: {e}")
            time.sleep(1)
    raise ValueError(" | ".join(errors))


def wiki_table(ix, page):
    """The first table on a Wikipedia page with a ticker column and the right number of rows."""
    html = ""
    for attempt in range(3):   # Wikipedia sometimes answers "too many requests"
        r = requests.get("https://en.wikipedia.org/wiki/" + page, headers=UA, timeout=60)
        html = r.text
        if r.status_code == 200:
            break
        time.sleep(4 * (attempt + 1))
    else:
        raise ValueError(f"Wikipedia answered {r.status_code}")
    lo, hi = ix["rows"]
    tables = []
    try:  # several index pages mark the member table with this id
        tables += pd.read_html(io.StringIO(html), attrs={"id": "constituents"}, flavor="lxml")
    except Exception:
        pass
    tables += pd.read_html(io.StringIO(html), flavor="lxml")
    clean = lambda k: re.sub(r"\[.*?\]", "", k).strip()
    seen = []
    for t in tables:
        # a table with a two-row heading comes back with tuple column names; use the last part
        cols = {str(c[-1] if isinstance(c, tuple) else c).strip().lower(): c for c in t.columns}
        tcol = next((cols[k] for k in cols if clean(k) in TICKER_COLS), None) \
            or next((cols[k] for k in cols if "ticker" in k or "symbol" in k), None)
        if len(t) >= 20:
            seen.append(f"{len(t)} rows {list(cols)[:6]}")
        if tcol is None or not lo <= len(t) <= hi:
            continue
        ncol = next((cols[k] for k in cols if re.sub(r"\[.*?\]", "", k).strip() in NAME_COLS), None)
        rows = []
        for _, r in t.iterrows():
            sym = re.sub(r"\[.*?\]", "", str(r[tcol])).split(":")[-1].strip().upper()
            if not sym or sym == "NAN":
                continue
            if ix["suffix"] is None:      # already written the way Yahoo writes it, e.g. SAP.DE
                tick = sym
            elif ix["suffix"] == "":      # US: BRK.B -> BRK-B
                tick = sym.replace(".", "-")
            else:                         # BT.A -> BT-A.L, RR. -> RR.L
                tick = sym.rstrip(".").replace(".", "-") + ix["suffix"]
            rows.append({"t": tick, "name": str(r[ncol]).strip() if ncol is not None else tick,
                         "sector": "", "industry": "", "hq": "", "founded": ""})
        if rows:
            return rows
    raise ValueError(f"no member table of {lo} to {hi} rows; tables seen: {seen[:8]}")


def download_closes(tickers):
    """Daily closes for every ticker, fetched in batches so one bad batch does not lose the rest."""
    out = {}
    for i in range(0, len(tickers), 250):
        batch = tickers[i:i + 250]
        try:
            px = yf.download(batch, period="63mo", interval="1d", auto_adjust=False,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"Price batch {i // 250 + 1} failed ({e})")
            continue
        for t in batch:
            try:
                s = px[t]["Close"].dropna()
            except KeyError:
                continue
            if len(s) >= 60:
                out[t] = s * 0.01 if t.endswith(".L") else s   # London quotes are in pence
        time.sleep(2)
    return out


_fx = {"USD": 1.0}


def fx(code, old=None):
    """US dollars per one unit of a currency (latest close)."""
    code = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(code, code) or "USD"
    if code not in _fx:
        rate = None
        try:
            h = yf.Ticker(f"{code}USD=X").history(period="10d", auto_adjust=False)["Close"].dropna()
            rate = float(h.iloc[-1]) if len(h) else None
        except Exception as e:
            print(f"  FX {code}: lookup failed ({e})")
        _fx[code] = rate or (old or {}).get(code) or FX_FALLBACK.get(code)
        print(f"FX {code}USD = {_fx[code]}")
    return _fx[code]


def previous():
    """Yesterday's file: records by ticker (used when Yahoo returns no fundamentals), its date, and the whole payload."""
    try:
        old = json.loads(OUT.read_text())
        return {r["t"]: r for r in old.get("stocks", [])}, old.get("asof"), old
    except Exception:
        return {}, None, {}


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


def series_block(s, digits=2):
    return {"start": s.index[0].strftime("%Y-%m-%d"), "end": s.index[-1].strftime("%Y-%m-%d"),
            "c": [round(float(v), digits) for v in s.values]}


def daily_6m(series, end):
    """Daily closes for the last six months, for the 1M, 3M and 6M graph ranges."""
    d = series[series.index > end - pd.DateOffset(months=6)].dropna()
    return series_block(d) if len(d) >= 5 else None


def monthly_max(tickers):
    """Month-end closes over each share's whole listed life, for the Max graph range."""
    out = {}
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        try:
            px = yf.download(batch, period="max", interval="1mo", auto_adjust=False,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"Monthly history batch failed ({e})")
            continue
        for t in batch:
            try:
                m = (px[t]["Close"] if len(batch) > 1 or isinstance(px.columns, pd.MultiIndex) else px["Close"]).dropna()
            except KeyError:
                continue
            if len(m) >= 12:
                out[t] = series_block(m * 0.01 if t.endswith(".L") else m)
        time.sleep(2)
    return out


def index_history(symbol):
    """An index's own daily, weekly and monthly closes, so the graph can overlay it on a share."""
    c = yf.download(symbol, period="max", interval="1d", auto_adjust=False, progress=False)["Close"]
    if isinstance(c, pd.DataFrame):
        c = c.iloc[:, 0]
    c = c.dropna()
    end = c.index[-1]
    w = c[c.index > end - pd.DateOffset(years=5)].resample("W-FRI").last().dropna()
    m = c.resample("ME").last().dropna()
    return c, {"d": daily_6m(c, end), "w": series_block(w), "m": series_block(m, 1)}


def forecast_accuracy(ticker):
    """How close reported earnings per share came to the forecast, over up to 20 quarters.

    Yahoo has no record of companies' own guidance, so this compares each quarter's reported
    EPS with the analyst forecast that stood before the results."""
    df = yf.Ticker(ticker).get_earnings_dates(limit=28)
    if df is None or not len(df):
        return None
    est = next((c for c in df.columns if "estimate" in c.lower()), None)
    act = next((c for c in df.columns if "reported" in c.lower() or "actual" in c.lower()), None)
    if not est or not act:
        return None
    rows = df[[est, act]].dropna().head(20)
    miss, hit = [], 0
    for e, a in rows.itertuples(index=False):
        e, a = num(e), num(a)
        if e is None or a is None or abs(e) < 0.01:
            continue
        miss.append(abs(a - e) / abs(e) * 100)
        hit += a >= e
    if len(miss) < 4:
        return None
    miss.sort()
    return {"n": len(miss), "hit": int(hit), "med": round(miss[len(miss) // 2], 1)}


def stats(info, curr, ticker="", old_fx=None):
    """Company figures. Money amounts are converted to US dollars; curr is the price in the share's own currency."""
    B = 1e9
    g = lambda k: num(info.get(k))
    us = "." not in ticker
    pence = ticker.endswith(".L")
    fx_price = 1.0 if us else (fx(ccy_of(ticker), old_fx) or 1.0)             # market value is in the trading currency
    fx_fin = fx(info.get("financialCurrency") or ccy_of(ticker), old_fx) or fx_price  # accounts can be in another one
    div_rate = g("dividendRate")
    ebitda, debt = g("ebitda"), g("totalDebt")
    pct = lambda v: None if v is None else round(v * 100, 2)
    bn = lambda v, r=fx_fin: None if v is None else round(v * r / B, 3)
    pe, fpe, eps, pt = g("trailingPE"), g("forwardPE"), g("trailingEps"), g("targetMeanPrice")
    if pence:
        # Yahoo quotes London shares in pence. Bring the analyst target into pounds, and repair a
        # P/E that was worked out as pence divided by pounds (100 times too big).
        pt = pt / 100 if pt else pt
        feps = g("forwardEps")
        if pe and eps and eps > 0 and pe / (curr / eps) > 30:
            pe = round(curr / eps, 2)
        if fpe and feps and feps > 0 and fpe / (curr / feps) > 30:
            fpe = round(curr / feps, 2)
    if us:
        div = round(div_rate / curr * 100, 2) if div_rate and curr else 0
    else:
        dy = g("trailingAnnualDividendYield")
        if dy and pence:
            dy *= 100   # Yahoo divides a dividend in pounds by a price in pence
        if not dy and div_rate and curr and not pence:
            dy = div_rate / curr
        div = round(dy * 100, 2) if dy and 0 < dy < 0.5 else 0
    return {
        "mcap": bn(g("marketCap"), fx_price), "pe": pe, "fpe": fpe,
        "rev": bn(g("totalRevenue")), "ebitda": bn(ebitda), "ni": bn(g("netIncomeToCommon")),
        "pm": pct(g("profitMargins")), "om": pct(g("operatingMargins")), "eps": eps,
        "fcf": bn(g("freeCashflow")), "debt": bn(debt), "cash": bn(g("totalCash")),
        "debitda": round(debt / ebitda, 2) if debt is not None and ebitda and ebitda > 0 else None,
        "roe": pct(g("returnOnEquity")), "cr": g("currentRatio"),
        "div": div,
        "pt": pt, "cons": (info.get("recommendationKey") or "").replace("_", " ").title() or None,
        "analysts": g("numberOfAnalystOpinions"),
        "revg": pct(g("revenueGrowth")), "pb": g("priceToBook"),
    }


def main():
    prev, prev_asof, old = previous()
    old_fx = old.get("fx") or {}
    old_indexes = {i["id"]: i for i in old.get("indexes") or []}

    # Members of every index, merged by ticker. A share in several indexes is fetched once.
    members, failed = {}, {}
    for ix in INDEXES:
        try:
            rows = sp500() if ix["id"] == "sp500" else wiki_members(ix)
        except Exception as e:
            if ix["id"] == "sp500":
                raise
            failed[ix["id"]] = f"member list failed ({e})"
            print(f"{ix['name']}: {failed[ix['id']]}")
            continue
        time.sleep(1)
        rows = [m for m in rows if m["t"] not in DUPLICATE_CLASSES]
        ix["tickers"] = [m["t"] for m in rows]
        print(f"{ix['name']} members: {len(rows)}")
        for m in rows:
            members.setdefault(m["t"], {**m, "idx": []})["idx"].append(ix["id"])
    tickers = list(members)

    closes = download_closes(tickers)
    prices = {}
    for t, s in closes.items():
        end = s.index[-1]
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

    # Coverage, index by index. The S&P 500 must be there; another index is skipped for the day.
    for ix in INDEXES:
        if "tickers" not in ix:
            continue
        got = sum(1 for t in ix["tickers"] if t in prices)
        ix["universe"] = got
        if ix["id"] == "sp500":
            if got < MIN_COVERAGE * len(ix["tickers"]):
                sys.exit(f"Only {got} of {len(ix['tickers'])} S&P 500 members have prices; keeping the existing data.json")
        elif got < OTHER_COVERAGE * len(ix["tickers"]):
            failed[ix["id"]] = f"only {got} of {len(ix['tickers'])} members have prices"
            print(f"{ix['name']}: {failed[ix['id']]}; keeping yesterday's figures for it")
            del ix["tickers"]
    live = {ix["id"] for ix in INDEXES if "tickers" in ix}
    for t in list(members):
        members[t]["idx"] = [i for i in members[t]["idx"] if i in live]
        if not members[t]["idx"]:
            del members[t]

    sp = next(ix for ix in INDEXES if ix["id"] == "sp500")
    asof = max(prices[t]["asof"] for t in sp["tickers"] if t in prices)
    asof_str = asof.strftime("%Y-%m-%d")
    print(f"Prices for {len(prices)} tickers, S&P 500 as of {asof.date()}")

    # Each index's own 3-month change, shown above the % 3M column for comparison
    indexes = []
    for ix in INDEXES:
        if ix["id"] not in live:
            if ix["id"] in old_indexes:
                indexes.append({**old_indexes[ix["id"]], "stale": True})
            continue
        entry = {"id": ix["id"], "name": ix["name"], "ccy": ix["ccy"], "universe": ix["universe"],
                 "asof": max(prices[t]["asof"] for t in ix["tickers"] if t in prices).strftime("%Y-%m-%d")}
        try:
            c, entry["hist"] = index_history(ix["yahoo"])
            base = close_on_or_before(c, c.index[-1] - pd.DateOffset(months=3))
            if len(c) and base:
                entry.update(curr=round(float(c.iloc[-1]), 2), p3m=round(base, 2),
                             pct3m=round((float(c.iloc[-1]) / base - 1) * 100, 2))
                print(f"{ix['name']} 3-month change: {entry['pct3m']}%")
        except Exception as e:
            print(f"{ix['name']}: index lookup failed ({e}); the page will leave the figure out")
        indexes.append(entry)

    out, misses = [], 0
    for m in members.values():
        t = m["t"]
        p = prices.get(t)
        if not p or not p["p3m"]:
            continue
        pct3m = (p["curr"] / p["p3m"] - 1) * 100
        if pct3m > MIN_DROP:
            continue
        info = {}
        for attempt in range(3):
            try:
                info = yf.Ticker(t).info or {}
                break
            except Exception as e:  # rate limits etc.
                print(f"  {t}: info failed ({e}); retrying")
                time.sleep(5 * (attempt + 1))
        time.sleep(0.6)
        was = prev.get(t)
        p3 = lambda v: round(v, 2) if v else None
        if not num(info.get("marketCap")):
            misses += 1
            if was and (was.get("stats") or {}).get("mcap"):
                # No fundamentals today: carry yesterday's forward with fresh prices, and say so
                print(f"  {t}: no fundamentals from Yahoo; reusing figures from {was.get('statsAsof') or prev_asof}")
                out.append({**was, "idx": m["idx"], "ccy": ccy_of(t),
                            "sector": m["sector"] or was.get("sector"), "hq": m["hq"] or was.get("hq"),
                            "pct3m": round(pct3m, 2), "curr": round(p["curr"], 2),
                            "p1m": p3(p["p1m"]), "p3m": p3(p["p3m"]), "p1y": p3(p["p1y"]), "p2y": p3(p["p2y"]),
                            "china": CHINA.get(t) or (was.get("china") if t not in CHINA and str(was.get("china") or "").startswith("Headquartered") else None),
                            "h5": weekly_5y(p["series"], p["asof"]),
                            "statsAsof": was.get("statsAsof") or prev_asof})
                continue
        listed = first_trade_year(t, info)
        country = (info.get("country") or "").strip()
        china_reason = CHINA.get(t)
        if not china_reason and country.lower() in CHINA_COUNTRIES:
            china_reason = f"Headquartered in {country}"
        web = (info.get("website") or "").strip()
        ysector = (info.get("sector") or "").strip()
        rec = {
            "t": t, "name": info.get("longName") or m["name"], "idx": m["idx"], "ccy": ccy_of(t),
            "sector": m["sector"] or SECTORS.get(ysector, ysector) or "Other",
            "industry": info.get("industry") or m["industry"],
            "hq": m["hq"] or ", ".join(x for x in [(info.get("city") or "").strip(), country] if x),
            "country": country, "url": web, "listed": listed,
            "pct3m": round(pct3m, 2), "curr": round(p["curr"], 2),
            "p1m": p3(p["p1m"]), "p3m": p3(p["p3m"]), "p1y": p3(p["p1y"]), "p2y": p3(p["p2y"]),
            "china": china_reason,
            "h5": weekly_5y(p["series"], p["asof"]),
            "stats": stats(info, p["curr"], t, old_fx),
            "statsAsof": asof_str,
        }
        out.append(rec)
        print(f"  {t:12} {pct3m:7.1f}%  {'CHINA' if china_reason else ''}")

    # Graph data and forecast accuracy for the shares that made the list
    today = dt.date.today()
    monthly = monthly_max([r["t"] for r in out])
    fa_new = 0
    for r in out:
        p = prices.get(r["t"])
        if p:
            r["hd"] = daily_6m(p["series"], p["asof"])
        if r["t"] in monthly:
            r["hm"] = monthly[r["t"]]
        was = prev.get(r["t"]) or {}
        fresh = False
        try:
            fresh = was.get("fa") and (today - dt.date.fromisoformat(was.get("faAsof", "2000-01-01"))).days < 7
        except ValueError:
            pass
        if fresh:   # quarterly figures: once a week is plenty, and it spares Yahoo
            r["fa"], r["faAsof"] = was["fa"], was["faAsof"]
            continue
        try:
            fa = forecast_accuracy(r["t"])
            time.sleep(0.4)
        except Exception as e:
            print(f"  {r['t']}: forecast history failed ({str(e)[:80]})")
            fa = None
        if fa:
            r["fa"], r["faAsof"] = fa, today.isoformat()
            fa_new += 1
        elif was.get("fa"):
            r["fa"], r["faAsof"] = was["fa"], was.get("faAsof")
    print(f"Forecast accuracy: {sum(1 for r in out if r.get('fa'))} of {len(out)} shares ({fa_new} fetched today)")

    if len(out) >= 8 and misses > MAX_INFO_MISSES * len(out):
        sys.exit(f"Yahoo returned no fundamentals for {misses} of {len(out)} shares; keeping the existing data.json")

    # An index that could not be refreshed today keeps yesterday's shares and prices
    price_map = {k: round(v["curr"], 2) for k, v in prices.items() if k in members}
    have = {r["t"] for r in out}
    for r in old.get("stocks", []):
        keep = [i for i in r.get("idx", []) if i in failed]
        if keep and r["t"] not in have:
            out.append({**r, "idx": keep})
    for t, v in (old.get("prices") or {}).items():
        price_map.setdefault(t, v)

    rates = {c: round(fx(c, old_fx), 6) for c in sorted({i["ccy"] for i in indexes} - {"USD"}) if fx(c, old_fx)}
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "asof": asof_str,
        "universe": sp["universe"],
        "minDrop": abs(MIN_DROP),
        "index": next(({k: i.get(k) for k in ("name", "curr", "p3m", "pct3m")} for i in indexes if i["id"] == "sp500" and "pct3m" in i), None),
        "indexes": indexes,
        "fx": rates,
        "notes": failed,  # why an index was not refreshed today, if any
        "source": "Yahoo Finance via yfinance (prices, fundamentals); Wikipedia (index member lists)",
        "stocks": sorted(out, key=lambda r: r["pct3m"]),
        # latest close for every member, so portfolio holdings keep pricing
        # after a share recovers and leaves the dip list
        "prices": dict(sorted(price_map.items())),
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"Wrote {len(out)} candidates to {OUT}" + (f"; not refreshed today: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
