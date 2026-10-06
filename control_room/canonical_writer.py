"""Explicitly disabled boundary for future canonical entity persistence."""


class CanonicalEntityWriter:
    """Future bridge to database_v2; Commit 5 never writes canonical data."""

    def create_from_verified_identity(self, proposed_identity):
        raise RuntimeError(
            'Canonical entity creation is disabled; verified identity remains PENDING_CREATE')
