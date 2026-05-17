import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def launch_browser():
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120 Safari/537.36"
        )
    )
    page = context.new_page()
    return p, browser, context, page


def search_jobs(page, country, job_position, job_location, date_posted):
    url = (
        f"{country}/jobs?"
        f"q={job_position.replace(' ', '+')}&"
        f"l={job_location}&"
        f"fromage={date_posted}"
    )

    page.goto(url, timeout=60000)
    page.wait_for_timeout(5000)

    # basic block detection
    if "verify" in page.url.lower() or "captcha" in page.content().lower():
        raise RuntimeError("Blocked by Indeed")

    return url


def scrape_job_data(page, country, max_pages=3):
    rows = []

    for _ in range(max_pages):
        # scroll to trigger lazy loading
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(random.randint(3000, 5000))

        cards = page.locator("div.job_seen_beacon")
        count = cards.count()

        for i in range(count):
            card = cards.nth(i)

            def safe_text(locator):
                return locator.inner_text().strip() if locator.count() else None

            link = None
            if card.locator("a[data-jk]").count():
                href = card.locator("a[data-jk]").first.get_attribute("href")
                link = country + href if href else None

            row = {
                "Job Title": safe_text(card.locator("h2.jobTitle span")),
                "Company": safe_text(card.locator("[data-testid='company-name']")),
                "Location": safe_text(card.locator("[data-testid='text-location']")),
                "Link": link,
            }
            rows.append(row)

        # pagination
        try:
            next_btn = page.locator("a[aria-label='Next Page']")
            if next_btn.count() == 0:
                break
            next_btn.first.click()
            page.wait_for_timeout(random.randint(4000, 6000))
        except PlaywrightTimeoutError:
            break

    return pd.DataFrame(rows)
