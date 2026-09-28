"""Sign-in and per-user scope (PRD FR-4). What a user may see is decided here and enforced by filters in
retrieval and SQL, never by the prompt."""
from .models import LEVELS, Scope, User  # noqa: F401
