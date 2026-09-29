"""Validated Control Room job lifecycle."""

TRANSITIONS = {
    'DRAFT': {'REVIEW_REQUIRED', 'QUEUED'},
    'REVIEW_REQUIRED': {'QUEUED', 'FAILED'},
    'QUEUED': {'STARTING'},
    'STARTING': {'RUNNING', 'FAILED'},
    'RUNNING': {'EXPORTING', 'COMPLETED', 'PARTIAL', 'FAILED'},
    'EXPORTING': {'COMPLETED', 'PARTIAL', 'FAILED'},
    'PARTIAL': {'QUEUED'},
    'COMPLETED': set(),
    'FAILED': set(),
}


def validate_transition(current, target):
    if current not in TRANSITIONS or target not in TRANSITIONS[current]:
        raise ValueError(f'Invalid job transition: {current} -> {target}')
