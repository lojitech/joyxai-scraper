import argparse
import re
import time
from typing import Optional, List, Tuple, Dict
from urllib.parse import urlparse

import requests
import gspread
import tldextract

try:
    from cse_credentials import GOOGLE_API_KEY, GOOGLE_CX_ID
except Exception:
    GOOGLE_API_KEY = ""
    GOOGLE_CX_ID = ""

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BAD_WEBSITE_HOST_CONTAINS = (
    "linkedin.", "facebook.", "instagram.", "x.com", "twitter.",
    "tiktok.", "youtube.",
    "indeed.", "jobstreet.", "glassdoor.", "mycareersfuture.",
    "recordowl.com", "sgpbusiness.com", "sgpgrid.com", "opencorporates.com",
    "crunchbase.com", "zoominfo.", "dnb.", "yelp.", "yellowpages.",
)

_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=False)


def normalize_header(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\s_\-]+", " ", s)
    return s


def find_header_index(headers: List[str], candidates: List[str]) -> Optional[int]:
    norm_headers = [normalize_header(h) for h in headers]
    cand = {normalize_header(c) for c in candidates}
    for i, h in enumerate(norm_headers):
        if h in cand:
            return i
    return None


def ensure_columns(ws, headers: List[str], required: List[str]) -> List[str]:
    out = list(headers)
    changed = False
    for col in required:
        if col not in out:
            out.append(col)
            changed = True
    if changed:
        ws.resize(rows=ws.row_count, cols=len(out))
        ws.update(values=[out], range_name="1:1")
    return out


def open_worksheet_by_gid(sh, gid: int):
    try:
        ws = sh.get_worksheet_by_id(gid)
        if ws is not None:
            return ws
    except Exception:
        pass
    for ws in sh.worksheets():
        if getattr(ws, "id", None) == gid:
            return ws
    raise RuntimeError(f"Worksheet gid {gid} not found.")


def cell(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://" + url


def is_bad_website(url: str) -> bool:
    try:
        u = normalize_url(url)
        host = (urlparse(u).netloc or "").lower()
        if not host:
            return True
        return any(x in host for x in BAD_WEBSITE_HOST_CONTAINS)
    except Exception:
        return True


def fetch_ok_html(session: requests.Session, url: str, timeout: float) -> bool:
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return False
        ct = (r.headers.get("Content-Type") or "").lower()
        if ct and ("text/html" not in ct and "application/xhtml" not in ct):
            return False
        return True
    except Exception:
        return False


def company_to_tokens(company: str) -> List[str]:
    name = (company or "").lower()
    name = re.sub(r"[^\w\s]", " ", name)
    for x in (
        "pte ltd", "pvt ltd", "private limited", "limited", "ltd",
        "sdn bhd", "bhd", "inc", "llc", "plc", "corp",
        "company", "group", "holdings", "the"
    ):
        name = name.replace(x, " ")
    name = re.sub(r"\s+", " ", name).strip()
    tokens = [t for t in name.split() if len(t) > 1]
    return tokens


def domain_guess_find_website(company: str, session: requests.Session, timeout: float, region: str) -> str:
    tokens = company_to_tokens(company)
    if not tokens:
        return ""

    bases: List[str] = []
    bases.append(tokens[0])
    if len(tokens) >= 2:
        bases.append(tokens[0] + tokens[1])
        bases.append(tokens[0] + "-" + tokens[1])
    if len(tokens) >= 3:
        bases.append(tokens[0] + tokens[1] + tokens[2])

    region_u = (region or "").upper().strip()
    if region_u == "SG":
        tlds = [".com.sg", ".sg", ".com"]
    elif region_u == "MY":
        tlds = [".com.my", ".my", ".com"]
    else:
        tlds = [".com"]

    for b in bases:
        for tld in tlds:
            for pref in ("https://www.", "https://", "http://www.", "http://"):
                u = f"{pref}{b}{tld}"
                if fetch_ok_html(session, u, timeout):
                    return u
    return ""


def google_cse_find_website(company: str, api_key: str, cx: str, timeout: float, retries: int) -> str:
    endpoint = "https://www.googleapis.com/customsearch/v1"
    queries = [f"{company} official website", company]
    for q in queries:
        for _ in range(max(1, retries + 1)):
            try:
                r = requests.get(
                    endpoint,
                    params={"key": api_key, "cx": cx, "q": q, "num": 5},
                    timeout=timeout,
                )
                data = r.json()
                if isinstance(data, dict) and "error" in data:
                    return ""

                items = (data.get("items") or []) if isinstance(data, dict) else []
                for item in items[:5]:
                    link = (item.get("link") or "").strip()
                    if not link.startswith("http"):
                        continue
                    if link.lower().endswith(".pdf"):
                        continue
                    if is_bad_website(link):
                        continue
                    return link
            except Exception:
                continue
    return ""


def find_official_website(company: str, session: requests.Session, timeout: float, retries: int, region: str) -> str:
    guess = domain_guess_find_website(company, session, timeout, region)
    if guess and not is_bad_website(guess):
        return guess

    api_key = (GOOGLE_API_KEY or "").strip()
    cx = (GOOGLE_CX_ID or "").strip()
    if api_key and cx:
        return google_cse_find_website(company, api_key, cx, timeout, retries)

    return ""


def update_cells_with_backoff(ws, cells: List[gspread.Cell], max_tries: int = 6):
    if not cells:
        return
    delay = 1.0
    for i in range(max_tries):
        try:
            ws.update_cells(cells, value_input_option="RAW")
            return
        except gspread.exceptions.APIError:
            if i == max_tries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--gid", type=int, required=True)
    ap.add_argument("--service-account", required=True)

    ap.add_argument("--region", default="SG")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--start-row", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--batch-cells", type=int, default=50)  # 1 cell per row
    args = ap.parse_args()

    gc = gspread.service_account(filename=args.service_account)
    sh = gc.open_by_key(args.sheet_id)
    ws = open_worksheet_by_gid(sh, args.gid)

    values = ws.get_all_values()
    if not values:
        raise RuntimeError("Worksheet is empty.")

    headers = values[0]
    idx_company = find_header_index(headers, ["Company", "COMPANY", "Company Name", "Name"])
    if idx_company is None:
        raise RuntimeError("Company column not found (Company/Company Name/Name).")

    headers = ensure_columns(ws, headers, ["Website"])
    idx_website = find_header_index(headers, ["Website"])
    if idx_website is None:
        raise RuntimeError("Website column not found and could not be created.")

    start_row = max(2, args.start_row)
    total_rows = len(values)
    last_row = total_rows if args.limit <= 0 else min(total_rows, start_row + args.limit - 1)
    if last_row < start_row:
        return

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    website_cache: Dict[str, str] = {}
    pending: List[gspread.Cell] = []

    try:
        for sheet_row in range(start_row, last_row + 1):
            row = values[sheet_row - 1]
            row = row + [""] * (len(headers) - len(row))

            company = cell(row, idx_company)
            if not company:
                continue

            cur_w = cell(row, idx_website)
            needs_w = args.overwrite or (not cur_w) or is_bad_website(cur_w)
            if not needs_w:
                continue

            key = company.strip().lower()
            website = website_cache.get(key)
            if website is None:
                website = find_official_website(company, session, args.timeout, args.retries, args.region)
                website_cache[key] = website

            pending.append(gspread.Cell(sheet_row, idx_website + 1, website))

            if len(pending) >= args.batch_cells:
                update_cells_with_backoff(ws, pending)
                pending = []

            print(f"[row {sheet_row}] website={'Y' if website else 'N'}", flush=True)
    finally:
        if pending:
            update_cells_with_backoff(ws, pending)


if __name__ == "__main__":
    main()