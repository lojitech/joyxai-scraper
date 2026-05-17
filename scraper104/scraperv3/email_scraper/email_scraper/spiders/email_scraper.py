import scrapy
import re
from scrapy.crawler import CrawlerProcess
from scrapy.linkextractors import LinkExtractor

class EmailSpider(scrapy.Spider):
    name = 'email_spider'
    allowed_domains = ['website.com']
    start_urls = ['https://website.com/']
    emails_found = set()

    # Initialize a LinkExtractor which will be used to extract links
    link_extractor = LinkExtractor()

    def parse(self, response):
        # Extract emails and add them to the set, automatically avoiding duplicates
        self.emails_found.update(re.findall(r'[\w\.-]+@[\w\.-]+', response.text))
        
        # Extract all links on the page and follow them
        links = self.link_extractor.extract_links(response)
        for link in links:
            yield scrapy.Request(url=link.url, callback=self.parse)

    def close(spider, reason):
        output_data = {'emails': list(spider.emails_found)} 
        with open('emails.json', 'w') as f:
            import json
            json.dump(output_data, f, indent=4)

if __name__ == "__main__":
    process = CrawlerProcess({
        'USER_AGENT': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'
    })
    process.crawl(EmailSpider)
    process.start()
