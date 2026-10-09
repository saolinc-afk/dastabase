"""Canonical requested-output contract for Import & Enrich jobs."""
import json


REQUESTED_OUTPUTS = ('WEBSITE', 'EMAIL', 'PHONE', 'FINANCIALS')
CONTACT_OUTPUTS = ('WEBSITE', 'EMAIL', 'PHONE')
LEGACY_CONTACT_OUTPUTS = ('WEBSITE', 'EMAIL')


def canonicalize_requested_outputs(values, *, allow_none=False):
    """Validate, deduplicate, and return outputs in canonical product order."""
    if values is None:
        if allow_none:
            return None
        raise ValueError('Requested outputs are required')
    if isinstance(values, (str, bytes)):
        raise ValueError('Requested outputs must be a collection')
    try:
        supplied = tuple(values)
    except TypeError as exc:
        raise ValueError('Requested outputs must be a collection') from exc
    if not supplied:
        raise ValueError('At least one requested output is required')
    unknown = [value for value in supplied
               if not isinstance(value, str) or value not in REQUESTED_OUTPUTS]
    if unknown:
        raise ValueError(f'Unknown requested output: {unknown[0]}')
    selected = set(supplied)
    return tuple(value for value in REQUESTED_OUTPUTS if value in selected)


def encode_requested_outputs(values, *, allow_none=False):
    outputs = canonicalize_requested_outputs(values, allow_none=allow_none)
    if outputs is None:
        return None
    return json.dumps(outputs, separators=(',', ':'))


def decode_requested_outputs(value):
    if value is None:
        return None
    try:
        stored = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid persisted requested outputs') from exc
    outputs = canonicalize_requested_outputs(stored)
    if list(outputs) != stored:
        raise ValueError('Persisted requested outputs are not canonical')
    return outputs


def missing_discovery_outputs(snapshot, requested_outputs):
    """Return requested contact outputs absent from accepted knowledge.

    FINANCIALS is intentionally absent: Discovery cannot satisfy canonical
    financial fields. ``None`` retains legacy Control Room website+email work.
    """
    outputs = LEGACY_CONTACT_OUTPUTS if requested_outputs is None else requested_outputs
    predicates = {
        'WEBSITE': snapshot.has_website,
        'EMAIL': snapshot.has_reusable_email,
        'PHONE': snapshot.has_reusable_phone,
    }
    return tuple(output for output in CONTACT_OUTPUTS
                 if output in outputs and not predicates[output]())
