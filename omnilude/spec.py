import hashlib
import json
from typing import Annotated

from pydantic import BaseModel, Field

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]


def canonical_json(obj: object) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()
