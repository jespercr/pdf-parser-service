import urllib.robotparser
from urllib.parse import urlparse

def is_scraping_allowed(url):
    parsed_url = urlparse(url)
    robots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/robots.txt"

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)

    try:
        rp.read()
        return rp.can_fetch("*", url)
    except Exception as e:
        # If robots.txt is unreachable, you can either default to False or True
        print(f"⚠️ Failed to read robots.txt: {e}")
        return False