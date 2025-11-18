# SEC Filings Bulk Downloader

A Python script to download all **10-K** and **10-Q** filings for a list of companies (tickers) from **2020-01-01** to **2025-12-31** using the official **SEC EDGAR API**.

---

## Features
- Fetches CIKs automatically for any stock ticker.
- Downloads full 10-K and 10-Q filings (HTML/iXBRL format).
- Organizes output neatly.

---
Steps
---
functions to map tickers → CIKs (via SEC's company_tickers.json),

fetching the company's submissions/CIKxxxxx.json and extracting 10-K / 10-Q filings in the requested date window,

building likely SEC archive URLs and downloading the full-submission.htm (with fallbacks),

simple rate limiting (default ~0.12s between requests → stays under the 10 req/sec guidance), retry/backoff logic, logging, and CLI integration (--tickers / --tickers-file, --start, --end, --out, --force).

---
Usage reminder:
---

Run from repository root so imports / PYTHONPATH are consistent:
PYTHONPATH=backend python3 scripts/download_sec_bulk.py --tickers AAPL MSFT AMZN ...

Set SEC_USER_AGENT environment variable to identify yourself (required by SEC), e.g.:
export SEC_USER_AGENT="Your Name youremail@example.com"

---
Extraction
---
From the downloaded htm 10K/Q files, I extract item “1A - Risk Factors" and “7 Management’s Discussion and Analysis of Financial Condition and Results of Operations” from 10K. I extracted Part1  “2 - Management’s Discussion and Analysis of Financial Condition and Results of Operations" and Part2 "1A - Risk Factors"  from 10 Q.
