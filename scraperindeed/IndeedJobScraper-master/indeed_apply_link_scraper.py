import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError

START_URL = "https://sg.indeed.com/jobs?q=intern&l=Singapore"
MAX_PAGES = 20          # hard safety limit
MAX_JOBS_PER_PAGE = 50  # safety limit


def main():
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        page.goto(START_URL, timeout=60000)
        page.wait_for_timeout(8000)

        # accept cookies if shown
        try:
            btn = page.locator("button:has-text('Accept')")
            if btn.count():
                btn.first.click()
                page.wait_for_timeout(3000)
        except:
            pass

        for page_no in range(MAX_PAGES):
            print(f"\n=== Page {page_no + 1} ===")

            # ensure job cards load
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(4000)

            cards = page.locator("div.job_seen_beacon")
            card_count = min(cards.count(), MAX_JOBS_PER_PAGE)

            for i in range(card_count):
                card = cards.nth(i)

                try:
                    card.click()
                    page.wait_for_timeout(3000)

                    # job title
                    title = page.locator("h1").inner_text()

                    # company name
                    company = page.locator('[data-testid="company-name"]').inner_text()

                    # apply button (YOUR TARGET)
                    apply_btn = page.locator(
                        'button[aria-label^="Apply on company site"]'
                    )

                    apply_url = (
                        apply_btn.first.get_attribute("href")
                        if apply_btn.count() > 0
                        else None
                    )

                    results.append({
                        "Job Title": title,
                        "Company": company,
                        "Apply URL": apply_url
                    })

                    print(f"✔ {company} | {apply_url}")

                    time.sleep(random.uniform(1.5, 3.0))

                except Exception as e:
                    print(f"Skipped job {i}: {e}")

            # go to next page
            try:
                next_btn = page.locator('a[aria-label="Next Page"]')
                if next_btn.count() == 0:
                    break

                next_btn.first.click()
                page.wait_for_timeout(random.randint(5000, 7000))

            except TimeoutError:
                break

        browser.close()

    # save output
    df = pd.DataFrame(results)
    df.to_csv("indeed_apply_links.csv", index=False)
    print("\nSaved to indeed_apply_links.csv")


if __name__ == "__main__":
    main()
