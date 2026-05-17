from job_scraper_utils import *

SINGAPORE = "https://sg.indeed.com"


def main():
    p, browser, context, page = launch_browser()

    try:
        job_position = "web developer"
        job_location = "Singapore"
        date_posted = 14

        search_jobs(
            page,
            SINGAPORE,
            job_position,
            job_location,
            date_posted
        )

        df = scrape_job_data(page, SINGAPORE, max_pages=3)

        if df.empty:
            print("No jobs scraped (likely blocked).")
        else:
            print(df.head())
            df.to_csv("indeed_sg_jobs.csv", index=False)

    finally:
        context.close()
        browser.close()
        p.stop()


if __name__ == "__main__":
    main()
