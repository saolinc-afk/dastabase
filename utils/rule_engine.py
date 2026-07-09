from bs4 import BeautifulSoup


def evaluate_rules(html: str, rules: dict) -> dict:
    """
    Generic keyword rule engine.

    Parameters
    ----------
    html : str
        HTML page

    rules : dict
        Loaded YAML configuration

    Returns
    -------
    {
        "score": 170,
        "matches": [
            {
                "category": "explicit",
                "keyword": "družinsko podjetje",
                "score": 100
            }
        ]
    }
    """

    soup = BeautifulSoup(html, "html.parser")

    text = soup.get_text(" ", strip=True).lower()

    total_score = 0

    matches = []

    for category, config in rules["rules"].items():

        score = config["score"]

        for keyword in config["keywords"]:

            if keyword.lower() in text:

                total_score += score

                matches.append({

                    "category": category,

                    "keyword": keyword,

                    "score": score

                })

    return {

        "score": total_score,

        "matches": matches

    }