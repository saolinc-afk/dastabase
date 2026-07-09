from bs4 import BeautifulSoup
import re

SUPPORTED_LANGUAGES = {
    "sl": "sl",
    "en": "en",
    "de": "de",
    "it": "it",
    "hr": "hr",
    "fr": "fr",
    "es": "es",
    "cs": "cs",
    "pl": "pl",
    "hu": "hu",
}


def detect_languages(html: str) -> dict:
    """
    Detect languages from cached HTML.

    Returns:
    {
        "languages": "sl,en,de",
        "language_count": 3,
        "international": 1
    }
    """

    soup = BeautifulSoup(html, "html.parser")

    found = set()

    #
    # <html lang="en">
    #
    html_tag = soup.find("html")

    if html_tag:
        lang = html_tag.get("lang")

        if lang:
            lang = lang.lower()[:2]

            if lang in SUPPORTED_LANGUAGES:
                found.add(lang)

    #
    # hreflang="en"
    #
    for tag in soup.find_all(attrs={"hreflang": True}):

        lang = tag.get("hreflang", "").lower()[:2]

        if lang in SUPPORTED_LANGUAGES:
            found.add(lang)

    #
    # URLs (/en/, /de/, ...)
    #
    html_lower = html.lower()

    for lang in SUPPORTED_LANGUAGES:

        patterns = [
            f'/{lang}/',
            f'/{lang}"',
            f'/{lang}?',
            f'lang="{lang}"',
            f'lang={lang}',
        ]

        if any(pattern in html_lower for pattern in patterns):
            found.add(lang)

    #
    # Language switcher text
    #
    text = soup.get_text(" ", strip=True).lower()

    keywords = {
        "en": ["english"],
        "de": ["deutsch"],
        "it": ["italiano"],
        "hr": ["hrvatski"],
        "fr": ["français"],
        "es": ["español"],
    }

    for lang, words in keywords.items():

        for word in words:

            if word in text:

                found.add(lang)

    #
    # Default
    #
    if not found:

        found.add("sl")

    languages = sorted(found)

    return {
        "languages": ",".join(languages),
        "language_count": len(languages),
        "international": int(any(l != "sl" for l in languages))
    }