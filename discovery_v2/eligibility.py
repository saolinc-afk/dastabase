"""General company eligibility rules applied before Discovery work starts."""
import re
import unicodedata


def normalize_legal_name(value):
    """Normalize case, punctuation and whitespace while retaining Unicode letters."""
    value = unicodedata.normalize('NFKC', value or '').casefold()
    return ' '.join(re.sub(r'[\W_]+', ' ', value, flags=re.UNICODE).split())


def company_eligibility(company):
    """Return the persisted eligibility status and reason for a source company."""
    normalized = normalize_legal_name(company.get('company_name'))
    if re.search(r'(?<!\w)v stečaju(?!\w)', normalized):
        return 'INELIGIBLE', 'BANKRUPTCY'
    return 'ELIGIBLE', None
