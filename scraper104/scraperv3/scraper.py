import time
import re
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright


class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

SHEET_URL = "https://docs.google.com/spreadsheets/d/1wuRJT4-RgUxJfpuDyM52PgDzEDJcL8s9Rn7OxM9bo_g/edit"


# GOOGLE SHEETS CONNECTION


def connect_gsheets():
    print(f"{Colors.OKBLUE}Connecting to Google Sheets...{Colors.ENDC}")

    try:
        SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
        gc = gspread.authorize(creds)
        wb = gc.open_by_url(SHEET_URL)
        main_sheet = wb.sheet1

        try:
            output_sheet = wb.worksheet("formatted_output")
        except:
            output_sheet = wb.add_worksheet(title="formatted_output", rows=5000, cols=12)
            output_sheet.append_row([
                "company name", "serial number", "website", "email",
                "phone number", "phone label", "address"
            ])

        print(f"{Colors.OKGREEN}✔ Connected successfully!{Colors.ENDC}")
        return wb, main_sheet, output_sheet

    except Exception as e:
        print(f"{Colors.FAIL}✘ Connection Failed: {e}{Colors.ENDC}")
        exit()


def choose_start_row(sheet):
    print(f"\n{Colors.HEADER}--- SCAN SETTINGS ---{Colors.ENDC}")
    print("1. Continue from last unfinished/failed row")
    print("2. Start from Row 11")

    choice = input(f"{Colors.BOLD}Choose (1/2): {Colors.ENDC}").strip()

    rows = sheet.get_all_values()

    if choice == "1":
        for i in range(10, len(rows)):  # Start scanning after row 10
            if len(rows[i]) < 13:
                return i + 1
            status = rows[i][12].strip() if len(rows[i]) > 12 else ""
            if status in ["", "Not Found", "Error", "Error: Failed to load"]:
                return i + 1
        return 11

    return 11




# EMAIL 


def extract_email(text):
    # Basic email pattern
    matches = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,10}", text)
    if not matches:
        return None

    clean = []
    seen = set()

    junk_domains = {
        "example.com", "test.com", "testing.com",
        "localhost", "invalid", "domain.com",
        "xxx.xxx", "xxx.xx", "xxx.com", "placeholder.com"
    }

    bad_tlds = {
        ".xx", ".xxx", ".local", ".invalid",
        ".test", ".example", ".placeholder"
    }

    junk_extensions = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".webp", ".css", ".js", ".woff", ".bmp"
    }

    banned_keywords = {
        "dummy", "fake", "placeholder", "noreply",
        "no-reply", "donotreply", "do-not-reply"
    }

    for email in matches:
        e = email.strip().lower()

        if ">" in e or "<" in e or "mailto" in e:
            continue

        try:
            user, domain = e.split("@")
        except:
            continue

        if len(user) > 30 and re.fullmatch(r"[0-9a-f]+", user):
            continue

        if re.fullmatch(r"[0-9.\-]+", domain):
            continue

        if domain in junk_domains:
            continue

        if any(domain.endswith(tld) for tld in bad_tlds):
            continue

        if any(e.endswith(ext) for ext in junk_extensions):
            continue

        if any(word in e for word in banned_keywords):
            continue

        tld = domain.split(".")[-1]
        if len(tld) < 2 or len(tld) > 10:
            continue

        if e not in seen:
            seen.add(e)
            clean.append(email)

    if not clean:
        return None

    priority = ["sales", "service", "info", "contact", "support", "admin"]
    clean.sort(key=lambda x: 0 if any(p in x.lower() for p in priority) else 1)

    return clean[0]




# PHONE 

def extract_phones_with_labels(text):
    """
    Returns list of (phone_number, label_before_number)
    Example:
    "Kaohsiung Office: 07-1234567" → ("07-1234567", "Kaohsiung Office")
    """

    raw = re.finditer(r'(\+?\d[\d\-\s\(\)]{6,20}\d)', text)
    results = []
    seen = set()

    for m in raw:
        phone = m.group(1).strip()
        idx = m.start()

        digits = re.sub(r"[^\d+]", "", phone)
        if re.fullmatch(r"\d{8}", digits):
            continue
        if re.search(r"20[0-4][0-9]", digits):
            continue
        if not (8 <= len(digits) <= 15):
            continue
        if not (digits.startswith("+") or digits.startswith("0") or digits.startswith("8")):
            continue

        label_window = text[max(0, idx - 50): idx].strip()
        label = None

        m2 = re.search(r'([A-Za-z\u4e00-\u9fa5 ]{2,80}):?\s*$', label_window)
        if m2:
            label = m2.group(1).strip()

        if not label:
            label = "Not Found"

        if phone not in seen:
            seen.add(phone)
            results.append((phone, label))

    return results



# ADDRESS 


def extract_addresses(text):
    text = text.replace("\n", " ").replace("\t", " ").replace("  ", " ")

    patterns = [
        r"\d{1,3}F[, ]*No\.\d+[, ]*Sec\.\s*\d+[, ]*[A-Za-z0-9 .\-]+Dist\.[, ]*[A-Za-z ]+City[, ]*\d{3,6}[, ]*Taiwan",

        r"[A-Za-z0-9 ,.\-]+(Road|Rd\.|Street|St\.|Lane|Ln\.|Boulevard|Blvd\.|Avenue|Ave\.)([, ]+[A-Za-z0-9 .,\-]+){1,5}",

        r"(台北市|新北市|桃園市|台中市|台南市|高雄市)[^，。 \n]{4,80}",

        r"No\.\s*\d+[A-Za-z0-9\-]*[, ]*Sec\.\s*\d+[, ]*[A-Za-z0-9 .\-]+",

        r"[A-Za-z ]+ Dist\."
    ]

    found = []

    for p in patterns:
        matches = re.findall(p, text, flags=re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple):
                m = m[0]
            m = m.strip()
            if len(m) > 10:
                found.append(m)

    return list(set(found))




def match_phone_to_address(phone, text, addresses):
    idx = text.find(phone)
    if idx == -1 or not addresses:
        return addresses[0] if addresses else "Not Found"

    window = text[max(0, idx - 250): idx + 250]

    candidates = [addr for addr in addresses if addr in window]

    if candidates:
        return max(candidates, key=len)

    return max(addresses, key=len)




# SCRAPER 


def run_scraper():
    wb, main_sheet, output_sheet = connect_gsheets()
    start_row = choose_start_row(main_sheet)

    with sync_playwright() as p:

        # Factory: create fresh browser/context/page
        def new_browser():
            b = p.chromium.launch(headless=True)
            c = b.new_context()
            return b, c, c.new_page()

        # Safe navigation with retries
        def safe_goto(page, url, max_retry=3):
            for _ in range(max_retry):
                try:
                    page.goto(url, timeout=60000, wait_until="networkidle")
                    return True
                except:
                    time.sleep(2)
            return False

        browser, ctx, page = new_browser()
        rows = main_sheet.get_all_values()

        print(f"{Colors.BOLD}--- Starting Scan from Row {start_row} ---{Colors.ENDC}")

        for i in range(start_row - 1, len(rows)):
            row = rows[i]

            # Restart browser every 20 URLs 
            if (i - (start_row - 1)) % 20 == 0 and i != (start_row - 1):
                try:
                    page.close()
                    ctx.close()
                    browser.close()
                except:
                    pass
                browser, ctx, page = new_browser()

            if len(row) < 4:
                continue

            company = row[1] if len(row) > 1 else "Not Found"
            url = row[3]

            if "http" not in url:
                continue

            print(f"[{i+1}] Scanning: {Colors.OKBLUE}{url}{Colors.ENDC}")

            if not safe_goto(page, url):
                print(f"{Colors.FAIL}✘ Navigation failed repeatedly{Colors.ENDC}")
                continue

            time.sleep(1.5)

            
            html = page.content()

            try:
                text = page.inner_text("body")
            except:
                text = html

            text = text.replace("\n", " ").replace("\t", " ")
            while "  " in text:
                text = text.replace("  ", " ")

            combined = (html + " " + text).replace("\n", " ")

            email = extract_email(combined) or "Not Found"
            phones_with_labels = extract_phones_with_labels(text)
            addresses = extract_addresses(combined)

            if not phones_with_labels:
                output_sheet.append_row([
                    company,
                    f"{company} - 1",
                    url,
                    email,
                    "Not Found",
                    "Not Found",
                    "Not Found"
                ])
                continue

            serial = 1
            for phone, label in phones_with_labels:
                addr = match_phone_to_address(phone, text, addresses) or "Not Found"

                output_sheet.append_row([
                    company,
                    f"{company} - {serial}",
                    url,
                    email,
                    phone,
                    label,
                    addr
                ])

                serial += 1

            print(f"{Colors.OKGREEN}✔ Stored {len(phones_with_labels)} numbers{Colors.ENDC}")

        browser.close()

    print(f"{Colors.OKGREEN}--- Scrape Complete! ---{Colors.ENDC}")




if __name__ == "__main__":
    run_scraper()


