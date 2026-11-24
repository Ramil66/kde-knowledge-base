from datetime import UTC, datetime

from .database import CatalogError

# Canonical local states and spellings used by Invent and Bugzilla.
STATES = {
    "tasks": {
        "open": ("new", "unconfirmed", "confirmed", "reported"),
        "in_progress": ("assigned", "in progress"),
        "completed": ("resolved", "closed", "verified", "done", "fixed"),
        "reopened": ("reopened",),
    },
    "branches": {"active": ("open",), "merged": (), "abandoned": ("closed", "deleted")},
    "merge_requests": {"open": ("opened", "reopened"), "closed": (), "merged": ("accepted",)},
}


def state(table: str, value: str, *, external: bool) -> str:
    spelling = value.strip().lower()
    for canonical, aliases in STATES[table].items():
        if spelling == canonical or spelling in aliases:
            return canonical
    if external:
        return "unknown"
    raise CatalogError(
        f"Unknown {table} status {value!r}. Choose one of {', '.join(STATES[table])}."
    )


def transition(table: str, old: str | None, new: str | None) -> None:
    if old == new or old in (None, "unknown") or new == "unknown":
        return
    allowed = {
        "tasks": {
            "open": ("in_progress", "completed"),
            "in_progress": ("open", "completed"),
            "completed": ("reopened", "in_progress", "open"),
            "reopened": ("open", "in_progress", "completed"),
        },
        "merge_requests": {"open": ("closed", "merged"), "closed": ("open",), "merged": ()},
        "branches": {"active": ("merged", "abandoned"), "abandoned": ("active",), "merged": ()},
    }
    if new not in allowed[table].get(old, ()):
        raise CatalogError(f"Invalid {table} transition: {old} -> {new}")


def timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Timestamp requires a UTC offset")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def now() -> str:
    return timestamp(datetime.now(UTC))
