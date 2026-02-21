import argparse
import json
import re
import time
import threading
from typing import Optional, List, Set, Tuple, Dict
from urllib.parse import urljoin, urlparse

import requests
import gspread
import tldextract
from bs4 import BeautifulSoup

import phonenumbers
from phonenumbers import PhoneNumberMatcher


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CONTACT_HINTS = (
    "contact", "contact-us", "contactus", "contacts",
    "about", "about-us", "support", "help",
    "customer-service", "customerservice",
    "locations", "location", "office", "reach", "reach-us", "reachus",
    "impressum", "legal", "privacy", "terms",
)

BAD_WEBSITE_HOST_CONTAINS = (
    "linkedin.", "facebook.", "instagram.", "x.com", "twitter.",
    "tiktok.", "youtube.",
    "indeed.", "jobstreet.", "glassdoor.", "mycareersfuture.",
    "recordowl.com", "sgpbusiness.com", "sgpgrid.com", "opencorporates.com",
    "crunchbase.com", "zoominfo.", "dnb.", "yelp.", "yellowpages.",
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
OBFUSCATED_EMAIL_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*(?:\(|\[)?\s*(?:at|@)\s*(?:\)|\])?\s*"
    r"([a-zA-Z0-9.\-]+)\s*(?:\(|\[)?\s*(?:dot|\.)\s*(?:\)|\])?\s*"
    r"([a-zA-Z]{2,})",
    re.IGNORECASE,
)

SG_POSTAL_RE = re.compile(r"\b\d{6}\b")
SG_WORD_RE = re.compile(r"\bsingapore\b", re.IGNORECASE)

_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=False)

_thread_local = threading.local()


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


def registrable_domain(url: str) -> str:
    try:
        host = urlparse(normalize_url(url)).netloc
        ext = _TLD(host)
        return (ext.top_domain_under_public_suffix or host).lower()
    except Exception:
        return ""


def same_site(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)


def is_bad_website(url: str) -> bool:
    try:
        u = normalize_url(url)
        host = (urlparse(u).netloc or "").lower()
        if not host:
            return True
        return any(x in host for x in BAD_WEBSITE_HOST_CONTAINS)
    except Exception:
        return True


def get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        _thread_local.session = s
    return s


def fetch_html(url: str, timeout: float) -> Tuple[Optional[str], Optional[str], int]:
    s = get_session()
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True)
        ct = (r.headers.get("Content-Type") or "").lower()
        if r.status_code >= 400:
            return r.url, None, r.status_code
        if ct and ("text/html" not in ct and "application/xhtml" not in ct):
            return r.url, None, r.status_code
        return r.url, r.text, r.status_code
    except Exception:
        return None, None, 0


def extract_emails_from_text(text: str) -> Set[str]:
    found = set(m.group(0).lower() for m in EMAIL_RE.finditer(text or ""))
    for m in OBFUSCATED_EMAIL_RE.finditer(text or ""):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower())
    return {e for e in found if len(e) <= 254 and ".." not in e}


def extract_phones_from_text(text: str, region: str) -> Set[str]:
    phones: Set[str] = set()
    if not text:
        return phones

    try:
        for match in PhoneNumberMatcher(text, region):
            num = match.number
            if phonenumbers.is_possible_number(num) and phonenumbers.is_valid_number(num):
                phones.add(phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164))
    except Exception:
        pass

    return phones


def extract_address_from_text(text: str) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    for ln in lines:
        if SG_WORD_RE.search(ln) and SG_POSTAL_RE.search(ln):
            return ln
    for ln in lines:
        if SG_WORD_RE.search(ln) and len(ln) <= 160:
            return ln
    return ""


def extract_jsonld_contacts_and_address(soup: BeautifulSoup, region: str) -> Tuple[Set[str], Set[str], str]:
    emails: Set[str] = set()
    phones: Set[str] = set()
    addr = ""

    def walk(obj):
        nonlocal addr
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if lk in ("email", "e-mail"):
                    if isinstance(v, str):
                        emails.update(extract_emails_from_text(v))
                    elif isinstance(v, list):
                        for x in v:
                            if isinstance(x, str):
                                emails.update(extract_emails_from_text(x))
                if lk in ("telephone", "tel", "phone", "contactnumber", "contact_number"):
                    if isinstance(v, str):
                        phones.update(extract_phones_from_text(v, region))
                    elif isinstance(v, list):
                        for x in v:
                            if isinstance(x, str):
                                phones.update(extract_phones_from_text(x, region))
                if lk in ("address", "streetaddress", "postaladdress"):
                    if isinstance(v, str) and not addr:
                        a = extract_address_from_text(v)
                        if a:
                            addr = a
                walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    for s in soup.find_all("script"):
        t = (s.get("type") or "").lower().strip()
        if t != "application/ld+json":
            continue
        raw = (s.string or s.get_text() or "").strip()
        if not raw:
            continue

        emails.update(extract_emails_from_text(raw))
        phones.update(extract_phones_from_text(raw, region))
        if not addr:
            a = extract_address_from_text(raw)
            if a:
                addr = a

        try:
            data = json.loads(raw)
            walk(data)
        except Exception:
            continue

    return emails, phones, addr


def parse_contacts_and_links(html: str, base_url: str, region: str) -> Tuple[Set[str], Set[str], str, List[str]]:
    raw = html or ""

    emails: Set[str] = set()
    phones: Set[str] = set()
    addr = ""

    emails.update(extract_emails_from_text(raw))
    phones.update(extract_phones_from_text(raw, region))
    addr = extract_address_from_text(raw) or ""

    soup = BeautifulSoup(raw, "html.parser")

    for a in soup.select("a[href^='mailto:']"):
        href = a.get("href", "")
        mail = href.split("mailto:", 1)[-1].split("?", 1)[0].strip().lower()
        if mail and EMAIL_RE.fullmatch(mail):
            emails.add(mail)

    for a in soup.select("a[href^='tel:']"):
        href = a.get("href", "")
        tel = href.split("tel:", 1)[-1].split("?", 1)[0].strip()
        if tel:
            phones.add(tel)

    em_ld, ph_ld, addr_ld = extract_jsonld_contacts_and_address(soup, region)
    emails |= em_ld
    phones |= ph_ld
    if not addr and addr_ld:
        addr = addr_ld

    for tag in soup(["style", "noscript"]):
        tag.decompose()
    for s in soup.find_all("script"):
        t = (s.get("type") or "").lower().strip()
        if t != "application/ld+json":
            s.decompose()

    text = soup.get_text(separator="\n", strip=True)
    emails |= extract_emails_from_text(text)
    phones |= extract_phones_from_text(text, region)
    if not addr:
        addr = extract_address_from_text(text) or ""

    contact_links: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        abs_url = urljoin(base_url, href)
        if not abs_url.startswith("http"):
            continue
        if not same_site(abs_url, base_url):
            continue

        path_lc = (urlparse(abs_url).path or "").lower()
        anchor_lc = (a.get_text(" ", strip=True) or "").lower()
        contactish = any(k in path_lc for k in CONTACT_HINTS) or any(k in anchor_lc for k in CONTACT_HINTS)

        if contactish and abs_url not in seen:
            seen.add(abs_url)
            contact_links.append(abs_url)

    return emails, phones, addr, contact_links


def crawl_for_contacts(
    website: str,
    max_pages: int,
    region: str,
    timeout: float,
    max_seconds: float,
    sleep_s: float,
) -> Tuple[Set[str], Set[str], str, str]:
    t0 = time.time()

    website = normalize_url(website)
    if not website:
        return set(), set(), "", "No website"
    if is_bad_website(website):
        return set(), set(), "", "Bad website domain"

    p = urlparse(website)
    base_url = f"{p.scheme}://{p.netloc}"

    final_home, html, sc = fetch_html(website, timeout)
    if not final_home or not html:
        return set(), set(), "", "Homepage fetch failed"

    emails_all: Set[str] = set()
    phones_all: Set[str] = set()
    best_addr = ""

    em, ph, addr, links = parse_contacts_and_links(html, base_url, region)
    emails_all |= em
    phones_all |= ph
    if addr:
        best_addr = addr

    queue: List[str] = [final_home] + links
    visited: Set[str] = set()

    while queue and len(visited) < max_pages:
        if time.time() - t0 > max_seconds:
            break

        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        if url == final_home:
            continue

        final_url, page_html, _ = fetch_html(url, timeout)
        if final_url and page_html:
            em2, ph2, addr2, links2 = parse_contacts_and_links(page_html, base_url, region)
            emails_all |= em2
            phones_all |= ph2
            if addr2 and not best_addr:
                best_addr = addr2
            for l in links2:
                if l not in visited and l not in queue:
                    queue.append(l)

        if sleep_s > 0 and queue:
            time.sleep(sleep_s)

    emails_all = {e for e in emails_all if not any(e.endswith(x) for x in (".png", ".jpg", ".jpeg", ".webp", ".gif"))}
    return emails_all, phones_all, best_addr, "OK" if (emails_all or phones_all or best_addr) else "No contacts found"


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
    ap.add_argument("--max-pages", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=18.0)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--max-seconds-per-company", type=float, default=25.0)
    ap.add_argument("--start-row", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--batch-cells", type=int, default=300)
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

    headers = ensure_columns(ws, headers, ["Website", "Email", "Phone", "Address", "Status"])

    idx_website = find_header_index(headers, ["Website"])
    idx_email = find_header_index(headers, ["Email", "Emails", "E-mail", "E-Mail"])
    idx_phone = find_header_index(headers, ["Phone", "Phones", "Telephone", "Tel"])
    idx_address = find_header_index(headers, ["Address"])
    idx_status = find_header_index(headers, ["Status"])

    start_row = max(2, args.start_row)
    total_rows = len(values)
    last_row = total_rows if args.limit <= 0 else min(total_rows, start_row + args.limit - 1)
    if last_row < start_row:
        return

    scrape_cache: Dict[str, Tuple[str, str, str, str]] = {}  # domain -> (emails_s, phones_s, addr, status)

    pending: List[gspread.Cell] = []

    for sheet_row in range(start_row, last_row + 1):
        row = values[sheet_row - 1]
        row = row + [""] * (len(headers) - len(row))

        company = cell(row, idx_company)
        if not company:
            continue

        website = cell(row, idx_website)
        if not website or is_bad_website(website):
            continue

        cur_e = cell(row, idx_email)
        cur_p = cell(row, idx_phone)
        cur_a = cell(row, idx_address)

        needs_e = args.overwrite or (not cur_e)
        needs_p = args.overwrite or (not cur_p)
        needs_a = args.overwrite or (not cur_a)

        if not (needs_e or needs_p or needs_a):
            continue

        dom = registrable_domain(website)
        if dom and dom in scrape_cache:
            emails_s, phones_s, addr_s, st = scrape_cache[dom]
        else:
            em_set, ph_set, addr_s, st = crawl_for_contacts(
                website=website,
                max_pages=args.max_pages,
                region=args.region.upper(),
                timeout=args.timeout,
                max_seconds=args.max_seconds_per_company,
                sleep_s=args.sleep,
            )
            emails_s = "; ".join(sorted(em_set)) if em_set else ""
            phones_s = "; ".join(sorted(ph_set)) if ph_set else ""
            if dom:
                scrape_cache[dom] = (emails_s, phones_s, addr_s, st)

        if needs_e:
            pending.append(gspread.Cell(sheet_row, idx_email + 1, emails_s))
        if needs_p:
            pending.append(gspread.Cell(sheet_row, idx_phone + 1, phones_s))
        if needs_a:
            pending.append(gspread.Cell(sheet_row, idx_address + 1, addr_s))

        pending.append(gspread.Cell(sheet_row, idx_status + 1, st))

        print(f"[row {sheet_row}] email={'Y' if emails_s else 'N'} phone={'Y' if phones_s else 'N'} addr={'Y' if addr_s else 'N'}", flush=True)

        if len(pending) >= args.batch_cells:
            update_cells_with_backoff(ws, pending)
            pending = []

    if pending:
        update_cells_with_backoff(ws, pending)


if __name__ == "__main__":
    main()