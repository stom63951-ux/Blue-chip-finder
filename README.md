# Blue-Chip Dip Finder

A single-page site that lists China-free S&P 500 blue-chip shares that have fallen 10% or more over the last 3 months, with a rule-based Yes/No buy check for each, a detail card, a 5-year chart, a Buy window and a Portfolio tab.

Live: https://stom63951-ux.github.io/Blue-chip-finder/ (GitHub Pages from `main`).

- `index.html` is the whole page. It loads `data.json` and works out the blue-chip test and the agent's score in the browser.
- `scripts/update_data.py` rebuilds `data.json` from Yahoo Finance (via yfinance) and Wikipedia's S&P 500 list. `.github/workflows/update-data.yml` runs it every weekday at 22:30 UTC and commits the result. If Yahoo returns too little data, the script stops and yesterday's file stays in place.
- `scripts/china_exclusions.json` is the hand-kept list of China-exposed companies. Revenue shares are approximate.
- Edits, purchases and the portfolio are stored in the viewer's browser (`localStorage`), and can be backed up and restored from the Portfolio tab.

Not financial advice.
