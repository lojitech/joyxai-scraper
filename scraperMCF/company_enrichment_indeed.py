import re
import time
import requests
import gspread
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote_plus
from google.oauth2.service_account import Credentials
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =====================
# CONFIG
# =====================
GSHEET_ID = "1wuRJT4-RgUxJfpuDyM52PgDzEDJcL8s9Rn7OxM9bo_g"
SOURCE_TAB = "indeed_data"
TARGET_TAB = "company_directory"
CREDS_FILE = "service_account.json"

GOOGLE_API_KEY = "AIzaSyAHwiVWu3J2pvBqoiGbcIRKThfvJLVb6tk"
GOOGLE_CX_ID = "37c3e127d40ea46c1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

BLOCKED_DOMAINS = {
    "linkedin.com", "facebook.com", "jobstreet", "indeed",
    "glassdoor", "mycareersfuture", "instagram", "tiktok.com",
    "youtube.com", "x.com", "twitter.com"
}

# Crawl limits
MAX_GOOGLE_RESULTS = 10
MAX_INTERNAL_PAGES = 6  # homepage + up to 5 candidate pages
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_COMPANIES = 1.2

# Patterns
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# SG-focused + general
PHONE_REGEX = re.compile(r"(\+65\s?\d{4}\s?\d{4}|\b[689]\d{7}\b|\+\d[\d\s\-()]{7,}\d)")
# SG postal code heuristic
POSTAL_REGEX = re.compile(r"\b\d{6}\b")

CONTACT_HINTS = ("contact", "contact-us", "contactus", "about", "locations", "location", "reach", "find-us", "findus", "imprint", "our-offices")

# =====================
# GOOGLE SHEETS
# =====================
def init_sheet():
    creds = Credentials.from_service_account_file(
        CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GSHEET_ID)

    src = sheet.worksheet(SOURCE_TAB)

    try:
        tgt = sheet.worksheet(TARGET_TAB)
    except gspread.WorksheetNotFound:
        tgt = sheet.add_worksheet(title=TARGET_TAB, rows="5000", cols="10")
        tgt.append_row(["Company", "Website", "Emails", "Phones", "Address", "Source", "Status"])

    return src, tgt

# =====================
# NORMALIZATION
# =====================
def normalize_company(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^\w\s]", " ", name)  # drop & . , etc
    for x in ["pte ltd", "pvt ltd", "private limited", "limited", "ltd", "singapore", "sg"]:
        name = name.replace(x, "")
    return re.sub(r"\s+", " ", name).strip()

# =====================
# GOOGLE SEARCH (OFFICIAL SITE)
# =====================
def is_blocked(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host.startswith(("www.linkedin.", "linkedin.")) or \
               host.startswith(("www.facebook.", "facebook.")) or \
               host.startswith(("www.instagram.", "instagram.")) or \
               host.startswith(("www.jobstreet.", "jobstreet.")) or \
               host.startswith(("www.indeed.", "indeed.")) or \
               host.startswith(("www.glassdoor.", "glassdoor.")) or \
               host.startswith(("www.mycareersfuture.", "mycareersfuture."))
    except:
        return True


def google_custom_search(query: str) -> list[str]:
    endpoint = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX_ID,
        "q": query,
        "num": MAX_GOOGLE_RESULTS
    }
    r = requests.get(endpoint, params=params, timeout=REQUEST_TIMEOUT)
    data = r.json()

    print("DEBUG Google response:", data)  # ← ADD THIS

    items = data.get("items", []) or []
    return [it.get("link", "") for it in items if it.get("link")]



def find_official_website(company: str) -> str | None:
    clean = normalize_company(company)
    queries = [
        f"{clean} official website",
        f"{clean} singapore",
        clean
    ]
    for q in queries:
        print(f"   🔍 Google query: {q}")
        try:
            links = google_custom_search(q)
        except Exception as e:
            print(f"   ❌ Google API error: {e}")
            continue

        for link in links:
            if not link.startswith("http"):
                continue
            if is_blocked(link):
                continue
            # Avoid PDFs as primary "website"
            if link.lower().endswith(".pdf"):
                continue
            return link

    return None

# =====================
# WEBSITE CRAWL + EXTRACTION
# =====================
def same_domain(base_url: str, candidate_url: str) -> bool:
    try:
        return urlparse(base_url).netloc.lower() == urlparse(candidate_url).netloc.lower()
    except:
        return False

def extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        # skip anchors and obvious junk
        if href.startswith("#"):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url.startswith("http") and same_domain(base_url, abs_url):
            urls.append(abs_url)
    # de-dup preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def candidate_pages(home_url: str, html: str) -> list[str]:
    links = extract_links(html, home_url)
    scored = []
    for u in links:
        path = (urlparse(u).path or "").lower()
        if any(h in path for h in CONTACT_HINTS):
            scored.append(u)
    # Put homepage first, then candidates
    # Limit to MAX_INTERNAL_PAGES total
    out = [home_url]
    for u in scored:
        if u not in out:
            out.append(u)
        if len(out) >= MAX_INTERNAL_PAGES:
            break
    return out

def parse_text_contacts(text: str) -> tuple[set[str], set[str], str]:
    emails = set(m.group(0) for m in EMAIL_REGEX.finditer(text))
    phones = set(m.group(0).strip() for m in PHONE_REGEX.finditer(text))

    # Address heuristic:
    # 1) any line containing "Singapore" and a 6-digit postal code
    address = ""
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    for ln in lines:
        if "singapore" in ln.lower() and POSTAL_REGEX.search(ln):
            address = ln
            break
    # 2) fallback: first line containing Singapore (short)
    if not address:
        for ln in lines:
            if "singapore" in ln.lower() and len(ln) <= 140:
                address = ln
                break

    return emails, phones, address

def parse_dom_contacts(soup: BeautifulSoup) -> tuple[set[str], set[str]]:
    # mailto: and tel:
    emails = set()
    phones = set()
    for a in soup.select("a[href^='mailto:']"):
        v = a.get("href", "").replace("mailto:", "").split("?")[0].strip()
        if v:
            emails.add(v)
    for a in soup.select("a[href^='tel:']"):
        v = a.get("href", "").replace("tel:", "").strip()
        if v:
            phones.add(v)
    return emails, phones

def fetch(url: str) -> tuple[str, str] | tuple[None, None]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False, allow_redirects=True)
        if r.status_code >= 400:
            return None, None
        return r.url, r.text  # final URL + HTML
    except:
        return None, None

def scrape_site_contacts(home_url: str) -> tuple[str, str, str, str]:
    final_home, html = fetch(home_url)
    if not final_home or not html:
        return "", "", "", "Homepage fetch failed"

    all_emails = set()
    all_phones = set()
    best_address = ""

    pages = candidate_pages(final_home, html)

    for idx, page_url in enumerate(pages):
        final_url, page_html = fetch(page_url)
        if not final_url or not page_html:
            continue

        soup = BeautifulSoup(page_html, "html.parser")
        text = soup.get_text("\n", strip=True)

        em_dom, ph_dom = parse_dom_contacts(soup)
        em_txt, ph_txt, addr = parse_text_contacts(text)

        all_emails |= em_dom | em_txt
        all_phones |= ph_dom | ph_txt
        if addr and not best_address:
            best_address = addr

        # small delay between pages
        if idx < len(pages) - 1:
            time.sleep(0.4)

    # basic cleanup
    all_emails = {e for e in all_emails if not any(e.lower().endswith(x) for x in (".png", ".jpg", ".jpeg", ".webp", ".gif"))}

    return ", ".join(sorted(all_emails)), ", ".join(sorted(all_phones)), best_address, "Success"

# =====================
# MAIN
# =====================
def run():
    src, tgt = init_sheet()

   
    
    # Company names are in column C (no usable header)
    companies = set(
        c.strip()
        for c in src.col_values(3)[1:]  # Column C, skip header row
        if c and c.strip()
        )

    existing = set(c.strip() for c in tgt.col_values(1)[1:] if c and c.strip())

    rows_to_append = []

    for company in sorted(companies):
        if company in existing:
            continue

        print(f"\n[+] Processing: {company}")

        website = find_official_website(company)
        if not website:
            rows_to_append.append([company, "", "", "", "", "Indeed", "No website found"])
            existing.add(company)
            continue

        print(f"   ✅ Found site: {website}")
        emails, phones, address, status = scrape_site_contacts(website)

        rows_to_append.append([
            company,
            website,
            emails,
            phones,
            address,
            "Indeed",
            status
        ])
        existing.add(company)

        # batch flush every 20 rows
        if len(rows_to_append) >= 20:
            tgt.append_rows(rows_to_append, value_input_option="RAW")
            rows_to_append = []

        time.sleep(SLEEP_BETWEEN_COMPANIES)

    # final flush
    if rows_to_append:
        tgt.append_rows(rows_to_append, value_input_option="RAW")

    print("\nDone.")

if __name__ == "__main__":
    run()
