import time
import random
import hashlib
import os
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError

# =====================
# CONFIGURATION
# =====================
BASE_URL = "https://www.mycareersfuture.gov.sg/search"
MAX_PAGES = 5
JOBS_PER_SESSION_CAP = 35

# Google Sheets Config
GSHEET_ID = "1wuRJT4-RgUxJfpuDyM52PgDzEDJcL8s9Rn7OxM9bo_g"
GSHEET_TAB = "MCF scraper"
CREDS_FILE = "service_account.json"

# =====================
# LOCKED SELECTORS
# =====================
SEL_JOB_CARD = "a[data-testid='job-card-link']"
SEL_TITLE    = "span[data-testid='job-card__job-title']"
SEL_COMPANY  = "p[data-testid='company-hire-info']"
SEL_LOCATION = "p[data-testid='job-card__location']"
SEL_SALARY   = "span[data-testid='salary-range']"
SEL_DATE     = "span[data-testid='job-card-date-info']"
SEL_DETAIL   = "div[id='job_description']"


# =====================
# GOOGLE SHEETS MODULE
# =====================
def init_google_sheet():
    """Connects to Sheet and returns the specific worksheet."""
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        
        sheet = client.open_by_key(GSHEET_ID)
        
        try:
            ws = sheet.worksheet(GSHEET_TAB)
            print(f"[*] Connected to worksheet: {GSHEET_TAB}")
        except gspread.WorksheetNotFound:
            print(f"[*] Creating worksheet: {GSHEET_TAB}")
            ws = sheet.add_worksheet(title=GSHEET_TAB, rows="2000", cols="10")
            # Create Header
            ws.append_row(["Job Hash", "Source", "Title", "Company", "Location", "URL", "Description"])
            
        return ws
    except Exception as e:
        print(f"[!] Google Sheets Error: {e}")
        return None

def load_history_from_gsheet(ws):
    """Downloads existing hashes from Cloud so we don't duplicate."""
    if not ws: return set()
    print("[*] Syncing history from Cloud...")
    try:
        # Assumes Column A (index 1) is 'Job Hash'
        hashes = ws.col_values(1)
        if len(hashes) > 1:
            clean = set(hashes[1:]) # Skip header row
            print(f"[*] Cloud Memory: {len(clean)} jobs known.")
            return clean
        return set()
    except:
        return set()

# =====================
# UTILS
# =====================
def random_sleep(min_s=1.5, max_s=4.5):
    time.sleep(random.uniform(min_s, max_s))

def human_scroll(page, deep=False):
    page.mouse.wheel(0, random.randint(300, 700))
    time.sleep(0.4)
    if deep:
        page.mouse.wheel(0, random.randint(600, 1000))
        time.sleep(0.6)

def generate_job_hash(title, company, location):
    raw = f"{title}|{company}|{location}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# =====================
# MAIN
# =====================
def run_scraper():
    # 1. Init Cloud
    gsheet = init_google_sheet()
    if not gsheet: return

    # 2. Load Memory (No local CSV needed)
    seen_hashes = load_history_from_gsheet(gsheet)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=250)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()

        print(f"[*] Opening {BASE_URL}")
        page.goto(BASE_URL)

        # Initial Load
        try:
            page.wait_for_selector(SEL_JOB_CARD, timeout=20000)
        except TimeoutError:
            print("[!] Initial load failed. Reloading...")
            page.reload()
            page.wait_for_selector(SEL_JOB_CARD)

        # Cookie Consent
        try:
            btn = page.locator("button:has-text('Accept')")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                random_sleep(1, 1.5)
        except: pass

        jobs_session = 0

        # Pagination Loop
        for page_num in range(MAX_PAGES):
            print(f"\n--- Page {page_num + 1} ---")
            try:
                page.wait_for_selector(SEL_JOB_CARD, timeout=15000)
            except:
                print("[!] No cards found or timeout.")
                break

            count = page.locator(SEL_JOB_CARD).count()
            print(f"[*] {count} cards found")

            for i in range(count):
                if jobs_session >= JOBS_PER_SESSION_CAP:
                    print("[!] Session cap reached.")
                    browser.close()
                    return

                card = page.locator(SEL_JOB_CARD).nth(i)

                try:
                    human_scroll(page)

                    # --- PRECISE EXTRACTION (Corrected) ---
                    try: 
                        title = card.locator(SEL_TITLE).first.inner_text().strip()
                    except: 
                        title = "Unknown Title"

                    try: 
                        company = card.locator(SEL_COMPANY).first.inner_text().strip()
                    except: 
                        company = "Unknown Company"

                    location = "Unknown"
                    if card.locator(SEL_LOCATION).count() > 0:
                        location = card.locator(SEL_LOCATION).first.inner_text().strip()

                    # Dedup Check
                    job_hash = generate_job_hash(title, company, location)
                    if job_hash in seen_hashes:
                        print(f"    [Skip] {title[:30]}...")
                        continue

                    # --- Action ---
                    print(f"    [New] {title[:30]}...")
                    card.click()

                    # Detail Page
                    page.wait_for_selector(SEL_DETAIL, timeout=8000)
                    human_scroll(page, deep=True)
                    random_sleep(2.0, 4.0)

                    desc = page.locator(SEL_DETAIL).inner_text()
                    url = page.url
                    clean_desc = desc[:800].replace("\n", " ")

                    # Write to Cloud
                    gsheet.append_row([
                        job_hash,
                        "MCF",
                        title,
                        company,
                        location,
                        url,
                        clean_desc
                    ])

                    seen_hashes.add(job_hash)
                    jobs_session += 1

                    # Back
                    page.go_back()
                    page.wait_for_selector(SEL_JOB_CARD, timeout=15000)
                    random_sleep(1.0, 2.0)

                    if jobs_session % 7 == 0:
                        random_sleep(5, 8)

                except Exception as e:
                    print(f"    [!] Error: {e}")
                    try:
                        if page.locator(SEL_DETAIL).count() > 0:
                            page.go_back()
                            page.wait_for_selector(SEL_JOB_CARD)
                    except: pass

            # Next Page
            next_btn = page.locator("button[aria-label='Next']")
            if next_btn.is_visible() and not next_btn.is_disabled():
                print("[*] Next page...")
                next_btn.click()
                random_sleep(3, 5)
            else:
                print("[*] No more pages.")
                break

        browser.close()

if __name__ == "__main__":
    run_scraper()