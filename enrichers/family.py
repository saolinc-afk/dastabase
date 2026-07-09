"""
Dastabase Family Business Detection

This module evaluates cached website HTML against
business rules defined in config/family_rules.yaml.
"""

from utils.config_loader import load_yaml
from utils.rule_engine import evaluate_rules


def detect_family(html: str) -> dict:
    """
    Detect whether a company is likely to be family owned.

    Returns
    -------
    {
        "family_status": "EXPLICIT",
        "score": 170,
        "matches": [...]
    }
    """

    rules = load_yaml("family_rules")

    result = evaluate_rules(html, rules)

    score = result["score"]

    explicit_threshold = rules["classification"]["explicit_threshold"]
    likely_threshold = rules["classification"]["likely_threshold"]

    if score >= explicit_threshold:
        status = "EXPLICIT"

    elif score >= likely_threshold:
        status = "LIKELY"

    else:
        status = "UNKNOWN"

    return {

        "family_status": status,

        "score": score,

        "matches": result["matches"]

    }