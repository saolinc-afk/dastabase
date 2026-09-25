import random
import time
import requests

from bs4 import BeautifulSoup
from settings import USER_AGENT, REQUEST_TIMEOUT

HEADERS = {
    "User-Agent": USER_AGENT
}

from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    """
    Ensure URL has a scheme.
    """

    url = url.strip()

    if not url:
        return url

    parsed = urlparse(url)

    if not parsed.scheme:
        url = "https://" + url

    return url

def download(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response

    except requests.RequestException as e:
        print(f"Download failed: {url}")
        print(e)
        return None

def html_to_text(html: str):

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "iframe"
        ]
    ):
        tag.decompose()

    text = soup.get_text(" ")

    text = " ".join(text.split())

    return soup, text

def page_title(soup):

    if soup.title:

        return soup.title.text.strip()

    return ""

def page_description(soup):

    tag = soup.find(
        "meta",
        attrs={"name": "description"}
    )

    if tag:

        return tag.get("content", "").strip()

    return ""

def crawl(url):

    url = normalize_url(url)
    print(f"Crawling {url}")
    response = download(url)

    if response is None:
        return None

    soup, text = html_to_text(response.text)

    result = {
        "status": response.status_code,
        "title": page_title(soup),
        "description": page_description(soup),
        "html": response.text,
        "text": text,
        "url": response.url,
        "text_length": len(text),
    }
    time.sleep(random.uniform(1.5, 4.0))
    return result

if __name__ == "__main__":
    data = crawl("https://www.elbi.si")

    if data is None:
        print("Crawler failed.")
    else:
        print(data["title"])
        print(data["description"])
        print(data["text"][:500])
