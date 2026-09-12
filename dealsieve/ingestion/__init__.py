"""Turn raw channel payloads into InboundMessage. Owned by W2 (email, text) and W4 (telegram)."""

from dealsieve.ingestion.email import parse_eml  # noqa: F401
from dealsieve.ingestion.text import from_text  # noqa: F401
