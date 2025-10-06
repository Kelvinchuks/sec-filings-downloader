#!/usr/bin/env python3
"""
scripts/download_sec_bulk.py

Downloads 10-K and 10-Q filings from EDGAR for a list of tickers over a date window.

Usage examples (run from repo root):

  PYTHONPATH=backend python3 scripts/download_sec_bulk.py --tickers AAPL MSFT AMZN --out data/raw/filings/sec/html

  PYTHONPATH=backend python3 scripts/download_sec_bulk.py --tickers-file tickers.txt --start 2020-01-01 --end 2025-12-31

Prerequisites:
 - Python 3.8+
 - requests
 - tqdm
 - An identifying User-Agent header is REQUIRED by the SEC. Set environment variable SEC_USER_AGENT or export SEC_USER_AGENT="Your Name your_email@example.com".

Notes:
 - The script uses the SEC "company submissions" JSON (data.sec.gov/submissions/CIK###.json) to find filings, which is efficient and reliable.
 - The script respects SEC rate guidance: it will not exceed ~10 requests/second and implements simple backoff for 429/5xx responses.
 - Destination layout matches: <OUT_ROOT>/<TICKER>/<FORM>/<ACCESSION>/full-submission.htm (accession with dashes preserved as folder but nodash used in SEC archive URL.)
 - To rerun: the script will skip downloads that already exist; use --force to re-download.

"""

import os
import sys
import time
import argparse
import logging
import json
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from tqdm import tqdm

# Constants
SEC_COMPANY_TICKERS_JSON = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik_padded}.json"
SEC_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
VALID_FORMS = {"10-K", "10-Q"}
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2025-12-31"

# Logging config
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sec_downloader")


def get_user_agent() -> str:
    ua = os.getenv("SEC_USER_AGENT")
    if not ua:
        raise EnvironmentError(
            "SEC_USER_AGENT environment variable not set. Set it to something like: 'Your Name youremail@example.com'"
        )
    return ua


def requests_session_with_retries(user_agent: str, max_retries: int = 5) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept": "application/json, text/plain, */*"})
    retries = Retry(total=max_retries, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def pad_cik(cik: str) -> str:
    """Pad CIK to 10 digits used by data.sec.gov submissions endpoint"""
    cik_digits = ''.join(ch for ch in cik if ch.isdigit())
    return cik_digits.zfill(10)


def load_ticker_to_cik_map(session: requests.Session) -> Dict[str, str]:
    """Download SEC's company_tickers.json and return map ticker -> cik (as string without padding).

    Falls back to an internal minimal mapping if the download fails.
    """
    try:
        r = session.get(SEC_COMPANY_TICKERS_JSON, timeout=10)
        r.raise_for_status()
        data = r.json()
        mapping = {v["ticker"]: str(v["cik_str"]) for _, v in data.items()}
        return mapping
    except Exception as e:
        logger.warning("Failed to load official ticker->CIK mapping: %s", e)
        # Minimal fallback (user may pass CIKs directly instead of tickers)
        return {}


def get_cik_for_ticker(ticker: str, mapping: Dict[str, str]) -> Optional[str]:
    t = ticker.upper()
    if t in mapping:
        return mapping[t]
    # allow the user to pass a raw CIK
    if ticker.isdigit():
        return ticker
    return None


def fetch_submissions_json(session: requests.Session, cik: str) -> Optional[dict]:
    padded = pad_cik(cik)
    url = SEC_SUBMISSIONS_URL.format(cik_padded=padded)
    logger.debug("Fetching submissions JSON for CIK %s -> %s", cik, url)
    r = session.get(url, timeout=20)
    if r.status_code == 404:
        logger.error("No submissions found for CIK %s (404)", cik)
        return None
    r.raise_for_status()
    return r.json()


def list_filings_from_submissions(submissions_json: dict, start_date: date, end_date: date) -> List[dict]:
    """Extract filings list from company submissions JSON and filter by form and date range.

    Returns list of dicts with keys: accessionNumber, form, filingDate, primaryDocument
    """
    filings = []
    # Modern structure: "filings" -> "recent" and "files" ->... But the top-level 'filings' contains 'recent' and 'files'
    # We'll read either 'filings'->'recent' or the older 'filings'-> 'files' or the top-level 'filings' field 'recent' and 'filing' arrays
    # Simpler: the submissions_json often has 'filings'->'recent' with arrays for 'accessionNumber', 'form', 'filingDate', 'primaryDocument'
    try:
        recent = submissions_json.get('filings', {}).get('recent', {})
        accession_list = recent.get('accessionNumber', [])
        forms = recent.get('form', [])
        dates = recent.get('filingDate', [])
        primary_docs = recent.get('primaryDocument', [])
        for acc, form, fdate, pdoc in zip(accession_list, forms, dates, primary_docs):
            try:
                fd = datetime.strptime(fdate, "%Y-%m-%d").date()
            except Exception:
                continue
            if form in VALID_FORMS and start_date <= fd <= end_date:
                filings.append({
                    'accessionNumber': acc,
                    'form': form,
                    'filingDate': fdate,
                    'primaryDocument': pdoc,
                })
    except Exception as e:
        logger.exception("Failed to parse submissions JSON: %s", e)
    return filings


def accession_nodash(accession: str) -> str:
    return accession.replace('-', '')


def build_archive_urls(cik: str, accession: str, primary_document: Optional[str]) -> List[str]:
    """Return list of candidate URLs to try for the full submission HTML.

    We try:
     - /Archives/edgar/data/{CIK}/{ACCESSION_NODASH}/full-submission.htm
     - /Archives/edgar/data/{CIK}/{ACCESSION_NODASH}/{primaryDocument}
     - /Archives/edgar/data/{CIK}/{ACCESSION_NODASH}/{ACCESSION}-index.htm
    """
    cik_int = ''.join(ch for ch in cik if ch.isdigit())
    acc_nodash = accession_nodash(accession)
    candidates = []
    candidates.append(f"{SEC_ARCHIVE_BASE}/{int(cik_int)}/{acc_nodash}/full-submission.htm")
    if primary_document:
        candidates.append(f"{SEC_ARCHIVE_BASE}/{int(cik_int)}/{acc_nodash}/{primary_document}")
        # sometimes primaryDocument is not .htm; try adding .htm
        if not primary_document.lower().endswith('.htm'):
            candidates.append(f"{SEC_ARCHIVE_BASE}/{int(cik_int)}/{acc_nodash}/{primary_document}.htm")
    # accession-index variant
    candidates.append(f"{SEC_ARCHIVE_BASE}/{int(cik_int)}/{acc_nodash}/{accession}-index.htm")
    return candidates


def save_content_to_path(content: bytes, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)


def download_filing(session: requests.Session, cik: str, ticker: str, form: str, accession: str, primary_document: Optional[str], out_root: str, rate_limiter: float = 0.12, force: bool = False) -> Tuple[bool, Optional[str]]:
    """Download filing and save to: out_root/<TICKER>/<FORM>/<ACCESSION>/full-submission.htm

    Returns (downloaded_bool, filepath_or_none)
    """
    dest_dir = os.path.join(out_root, ticker.upper(), form, accession)
    dest_file = os.path.join(dest_dir, 'full-submission.htm')
    if os.path.exists(dest_file) and not force:
        logger.info("Skipping existing %s", dest_file)
        return False, dest_file

    candidates = build_archive_urls(cik, accession, primary_document)
    for url in candidates:
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200 and resp.content:
                save_content_to_path(resp.content, dest_file)
                logger.info("Saved filing %s %s -> %s", ticker, accession, dest_file)
                # Respect a simple rate limiter
                time.sleep(rate_limiter)
                return True, dest_file
            else:
                logger.debug("Tried %s got status %s", url, resp.status_code)
        except requests.HTTPError as e:
            logger.warning("HTTP error fetching %s: %s", url, e)
        except Exception as e:
            logger.warning("Error fetching %s: %s", url, e)
        # small pause between retries/candidates
        time.sleep(rate_limiter)
    logger.error("Failed to download filing for %s accession %s (tried %d urls)", ticker, accession, len(candidates))
    return False, None


def parse_args():
    p = argparse.ArgumentParser(description="Download 10-K and 10-Q filings from SEC EDGAR for given tickers.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--tickers', nargs='+', help='List of tickers, e.g. AAPL MSFT AMZN')
    group.add_argument('--tickers-file', type=str, help='File with one ticker per line')
    p.add_argument('--start', type=str, default=DEFAULT_START, help='Start date YYYY-MM-DD')
    p.add_argument('--end', type=str, default=DEFAULT_END, help='End date YYYY-MM-DD')
    p.add_argument('--out', type=str, default='data/raw/filings/sec/html', help='Output root directory')
    p.add_argument('--force', action='store_true', help='Re-download existing files')
    p.add_argument('--rate', type=float, default=0.12, help='Minimum seconds between requests (default 0.12 -> ~8 req/sec). SEC recommends <=10 req/sec')
    return p.parse_args()


def main():
    args = parse_args()
    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    except Exception:
        logger.error("Invalid start or end date format. Use YYYY-MM-DD")
        sys.exit(1)

    tickers = []
    if args.tickers:
        tickers = args.tickers
    else:
        with open(args.tickers_file, 'r') as f:
            tickers = [line.strip() for line in f if line.strip()]

    # Setup session
    user_agent = get_user_agent()
    session = requests_session_with_retries(user_agent)

    # Load mapping
    mapping = load_ticker_to_cik_map(session)

    # For progress
    overall_todo = []

    for t in tickers:
        cik = get_cik_for_ticker(t, mapping)
        if cik is None:
            logger.error("Could not find CIK for ticker %s. Skipping.", t)
            continue
        submissions = fetch_submissions_json(session, cik)
        if not submissions:
            continue
        filings = list_filings_from_submissions(submissions, start_date, end_date)
        if not filings:
            logger.info("No 10-K/10-Q filings in range for %s", t)
            continue
        for f in filings:
            overall_todo.append((t, cik, f['form'], f['accessionNumber'], f.get('primaryDocument')))

    logger.info("Found %d filings to download", len(overall_todo))

    # Download sequentially with rate-limiting and simple backoff
    for (t, cik, form, accession, pdoc) in tqdm(overall_todo, desc="Downloading filings"):
        success = False
        attempt = 0
        max_attempts = 4
        while attempt < max_attempts and not success:
            try:
                downloaded, path = download_filing(session, cik, t, form, accession, pdoc, args.out, rate_limiter=args.rate, force=args.force)
                success = downloaded or (path is not None and os.path.exists(path))
                if not success:
                    attempt += 1
                    backoff = 2 ** attempt
                    logger.info("Retrying %s after backoff %s seconds (attempt %d)", accession, backoff, attempt)
                    time.sleep(backoff)
            except Exception as e:
                attempt += 1
                logger.exception("Error downloading %s attempt %d: %s", accession, attempt, e)
                time.sleep(2 ** attempt)

    logger.info("Done")


if __name__ == '__main__':
    main()
