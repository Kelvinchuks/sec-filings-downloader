#!/usr/bin/env python3
"""
scripts/extract_sec_sections.py

Parse downloaded SEC HTML filings and extract specific sections:

 - For 10-K: extract Item 1A (Risk Factors) and Item 7 (Management's Discussion and Analysis)
 - For 10-Q: extract Part 1 Item 2 (Management's Discussion and Analysis) and Part 2 Item 1A (Risk Factors)

Saves extracted sections as plain text under:
  data/processed/filings/sec/text/<TICKER>/<FORM>/<ACCESSION>/<SECTION>.txt

Usage (from repo root):
  PYTHONPATH=backend python3 scripts/extract_sec_sections.py --in-root data/raw/filings/sec/html --out-root data/processed/filings/sec/text --tickers AAPL MSFT

Prerequisites:
 - Python 3.8+
 - beautifulsoup4
 - tqdm

Notes:
 - The script tries to be robust to heading variants (e.g., "Item 1A.", "ITEM 1A - RISK FACTORS", "ITEM 7.").
 - For 10-Q, extraction is constrained inside PART I and PART II respectively.
 - Use --force to overwrite existing outputs.

"""

import re
import os
import sys
import argparse
import logging
from typing import Optional, Tuple, Dict, List
from pathlib import Path

from bs4 import BeautifulSoup
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sec_extractor")

# Patterns (uppercase will be used for searching)
ITEM_HEADING_RE = re.compile(r"\bITEM\s+\d+[A-Z]?\b", flags=re.IGNORECASE)

# Target patterns for start detection (use uppercase when searching)
TENK_PATTERNS = {
    "item_1a_risk_factors": re.compile(r"\bITEM\s+1A\b", flags=re.IGNORECASE),
    "item_7_mda": re.compile(r"\bITEM\s+7\b", flags=re.IGNORECASE),
}

TENQ_PART_PATTERNS = {
    "part_i": re.compile(r"\bPART\s+I\b|\bPART\s+1\b", flags=re.IGNORECASE),
    "part_ii": re.compile(r"\bPART\s+II\b|\bPART\s+2\b", flags=re.IGNORECASE),
}

TENQ_ITEM_PATTERNS = {
    "part1_item2_mda": re.compile(r"\bITEM\s+2\b", flags=re.IGNORECASE),
    "part2_item1a_risk_factors": re.compile(r"\bITEM\s+1A\b", flags=re.IGNORECASE),
}

SECTION_FILENAMES = {
    'item_1a_risk_factors': 'item_1a_risk_factors.txt',
    'item_7_mda': 'item_7_mda.txt',
    'part1_item2_mda': 'part1_item2_mda.txt',
    'part2_item1a_risk_factors': 'part2_item1a_risk_factors.txt',
}


def find_all_filing_paths(in_root: str, tickers: Optional[List[str]] = None, forms: Optional[List[str]] = None) -> List[Tuple[str,str,str,str]]:
    """Return list of tuples (ticker, form, accession, full_path_to_html)

    Walks directory layout: <in_root>/<TICKER>/<FORM>/<ACCESSION>/full-submission.htm
    """
    results = []
    root = Path(in_root)
    if not root.exists():
        logger.error("Input root %s does not exist", in_root)
        return results
    for ticker_dir in root.iterdir():
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name.upper()
        if tickers and ticker not in [t.upper() for t in tickers]:
            continue
        for form_dir in ticker_dir.iterdir():
            if not form_dir.is_dir():
                continue
            form = form_dir.name.upper()
            if forms and form not in [f.upper() for f in forms]:
                continue
            for accession_dir in form_dir.iterdir():
                if not accession_dir.is_dir():
                    continue
                html_path = accession_dir / 'full-submission.htm'
                if html_path.exists():
                    results.append((ticker, form, accession_dir.name, str(html_path)))
                else:
                    # try alternate file names sometimes used
                    for alt in ['index.html', 'filing.htm', 'primary_doc.html']:
                        altp = accession_dir / alt
                        if altp.exists():
                            results.append((ticker, form, accession_dir.name, str(altp)))
                            break
    return sorted(results)


def extract_text_from_html_file(path: str) -> str:
    with open(path, 'rb') as f:
        raw = f.read()
    # Parse with lxml or html.parser
    soup = BeautifulSoup(raw, 'lxml')
    # Remove scripts/styles
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    # Get text with newline separators to retain heading boundaries
    text = soup.get_text(separator='\n')
    # Normalize whitespace: collapse multiple newlines to double-newline, strip
    text = re.sub(r"\r", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Trim trailing/leading spaces on each line
    text = '\n'.join(line.strip() for line in text.splitlines())
    # Collapse multiple spaces
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def find_next_item_index(upper_text: str, start_pos: int) -> Optional[int]:
    """Find index of the next ITEM heading after start_pos in the uppercase text. Return index or None if not found."""
    m = ITEM_HEADING_RE.search(upper_text, pos=start_pos + 1)
    if m:
        return m.start()
    return None


def extract_between(upper_text: str, full_text: str, start_idx: int, end_idx: Optional[int]) -> str:
    if end_idx is None:
        snippet = full_text[start_idx:].strip()
    else:
        snippet = full_text[start_idx:end_idx].strip()
    return snippet


def extract_for_10k(full_text: str) -> Dict[str, Optional[str]]:
    """Extract item_1a_risk_factors and item_7_mda from a 10-K filing text.

    Returns dict with keys and values (or None if not found)
    """
    upper = full_text.upper()
    out = {k: None for k in TENK_PATTERNS.keys()}
    # Find starts
    starts = {}
    for key, pat in TENK_PATTERNS.items():
        m = pat.search(upper)
        if m:
            starts[key] = m.start()
    # Sort starts by position
    for key, start_pos in starts.items():
        # find next ITEM heading after this start
        next_idx = find_next_item_index(upper, start_pos)
        # If next item index is before this (rare), find the next instance after start+1
        if next_idx is not None and next_idx <= start_pos:
            next_idx = find_next_item_index(upper, start_pos + 1)
        snippet = extract_between(upper, full_text, start_pos, next_idx)
        out[key] = snippet
    return out


def find_part_bounds(upper_text: str) -> Dict[str, Optional[Tuple[int,int]]]:
    """Return start/end indices for PART I and PART II regions in the uppercase text.

    If PART II not found, the end is None.
    """
    res = {'part_i': None, 'part_ii': None}
    m1 = TENQ_PART_PATTERNS['part_i'].search(upper_text)
    m2 = TENQ_PART_PATTERNS['part_ii'].search(upper_text)
    if m1:
        start1 = m1.start()
        if m2:
            start2 = m2.start()
            res['part_i'] = (start1, start2)
            res['part_ii'] = (start2, None)
        else:
            # Only PART I found -> from start1 to end
            res['part_i'] = (start1, None)
    elif m2:
        # Only PART II found
        start2 = m2.start()
        res['part_ii'] = (start2, None)
    return res


def extract_for_10q(full_text: str) -> Dict[str, Optional[str]]:
    """Extract part1_item2_mda and part2_item1a_risk_factors from a 10-Q filing text.
    Uses PART boundaries to constrain search.
    """
    upper = full_text.upper()
    out = {k: None for k in TENQ_ITEM_PATTERNS.keys()}
    part_bounds = find_part_bounds(upper)

    # Part I -> Item 2
    if part_bounds.get('part_i'):
        pstart, pend = part_bounds['part_i']
        # search for ITEM 2 in that slice
        search_slice = upper[pstart: pend] if pend else upper[pstart:]
        m = TENQ_ITEM_PATTERNS['part1_item2_mda'].search(search_slice)
        if m:
            start_idx = pstart + m.start()
            # find next item after this start
            next_idx = find_next_item_index(upper, start_idx)
            snippet = extract_between(upper, full_text, start_idx, next_idx)
            out['part1_item2_mda'] = snippet
    else:
        logger.debug("PART I not found for 10-Q; attempting global search for ITEM 2")
        # fallback to global search
        m = TENQ_ITEM_PATTERNS['part1_item2_mda'].search(upper)
        if m:
            start_idx = m.start()
            next_idx = find_next_item_index(upper, start_idx)
            out['part1_item2_mda'] = extract_between(upper, full_text, start_idx, next_idx)

    # Part II -> Item 1A
    if part_bounds.get('part_ii'):
        pstart, pend = part_bounds['part_ii']
        search_slice = upper[pstart: pend] if pend else upper[pstart:]
        m = TENQ_ITEM_PATTERNS['part2_item1a_risk_factors'].search(search_slice)
        if m:
            start_idx = pstart + m.start()
            next_idx = find_next_item_index(upper, start_idx)
            out['part2_item1a_risk_factors'] = extract_between(upper, full_text, start_idx, next_idx)
    else:
        logger.debug("PART II not found for 10-Q; attempting global search for ITEM 1A")
        m = TENQ_ITEM_PATTERNS['part2_item1a_risk_factors'].search(upper)
        if m:
            start_idx = m.start()
            next_idx = find_next_item_index(upper, start_idx)
            out['part2_item1a_risk_factors'] = extract_between(upper, full_text, start_idx, next_idx)

    return out


def save_sections(out_root: str, ticker: str, form: str, accession: str, sections: Dict[str, Optional[str]], force: bool = False) -> None:
    base = Path(out_root) / ticker.upper() / form.upper() / accession
    base.mkdir(parents=True, exist_ok=True)
    for key, content in sections.items():
        filename = SECTION_FILENAMES.get(key, f"{key}.txt")
        dest = base / filename
        if dest.exists() and not force:
            logger.info("Skipping existing %s", dest)
            continue
        if content is None or len(content.strip()) == 0:
            logger.warning("No content extracted for %s %s %s -> %s", ticker, form, accession, key)
            continue
        dest.write_text(content, encoding='utf-8')
        logger.info("Wrote %s", dest)


def process_filing(ticker: str, form: str, accession: str, html_path: str, out_root: str, force: bool = False) -> None:
    logger.info("Processing %s %s %s", ticker, form, accession)
    try:
        text = extract_text_from_html_file(html_path)
        if form.upper() == '10-K' or form.upper() == '10K':
            sections = extract_for_10k(text)
        elif form.upper() == '10-Q' or form.upper() == '10Q':
            sections = extract_for_10q(text)
        else:
            logger.debug("Skipping form %s", form)
            return
        save_sections(out_root, ticker, form, accession, sections, force=force)
    except Exception as e:
        logger.exception("Failed to process %s %s %s: %s", ticker, form, accession, e)


def parse_args():
    p = argparse.ArgumentParser(description='Extract specific sections from SEC 10-K and 10-Q filings')
    p.add_argument('--in-root', type=str, default='data/raw/filings/sec/html', help='Root of downloaded filings')
    p.add_argument('--out-root', type=str, default='data/processed/filings/sec/text', help='Where to write extracted sections')
    p.add_argument('--tickers', nargs='*', help='Optional list of tickers to limit processing')
    p.add_argument('--forms', nargs='*', help='Optional list of forms to process (e.g., 10-K 10-Q)')
    p.add_argument('--force', action='store_true', help='Overwrite existing extracted files')
    return p.parse_args()


def main():
    args = parse_args()
    filings = find_all_filing_paths(args.in_root, tickers=args.tickers, forms=args.forms)
    logger.info("Found %d filings under %s", len(filings), args.in_root)
    for ticker, form, accession, path in tqdm(filings, desc='Extracting sections'):
        process_filing(ticker, form, accession, path, args.out_root, force=args.force)
    logger.info("Done")

if __name__ == '__main__':
    main()
