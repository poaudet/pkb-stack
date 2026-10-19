"""Name normalization shared by candidate resolution, validation and merge."""
from __future__ import annotations

import re

from ..db import fold

HONORIFICS = {"mr", "mrs", "ms", "miss", "mme", "mlle", "mmes", "dr", "dre", "prof", "m"}
ID_NAME_RE = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
NAME_CHECKED_TYPES = {"person", "place", "habit", "list"}


def norm_name(value: object) -> str:
    """Lowercase, accents stripped, honorifics dropped, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    text = fold(value) or ""
    text = re.sub(r"[^\w\s'-]|_", " ", text)
    words = [word for word in text.split() if word not in HONORIFICS]
    return " ".join(words)


def is_id_name(value: object) -> bool:
    return isinstance(value, str) and bool(ID_NAME_RE.match(value.strip()))


def is_named_type(type_name: object) -> bool:
    return isinstance(type_name, str) and (
        type_name in NAME_CHECKED_TYPES or type_name.endswith("_list")
    )
