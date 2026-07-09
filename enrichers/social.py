from bs4 import BeautifulSoup

SOCIAL_DOMAINS = {
    "linkedin": "linkedin.com",
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "youtube": "youtube.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "tiktok": "tiktok.com"
}


def detect_social(html: str) -> dict:

    soup = BeautifulSoup(html, "html.parser")

    links = []

    for tag in soup.find_all("a", href=True):

        links.append(tag["href"].lower())

    result = {

        "linkedin": False,
        "facebook": False,
        "instagram": False,
        "youtube": False,
        "twitter": False,
        "tiktok": False

    }

    for url in links:

        for network, domain in SOCIAL_DOMAINS.items():

            if domain in url:

                if network == "x":
                    result["twitter"] = True
                else:
                    result[network] = True

    return result