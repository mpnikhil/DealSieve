"""SQLite persistence. Owned by W2.

Entry point: Repo(db_path). Events and underwriting runs are append-only.
"""

from dealsieve.persistence.repo import (  # noqa: F401
    DuplicateNotification,
    MessageClaimResult,
    MessageProcessingStatus,
    Repo,
)
