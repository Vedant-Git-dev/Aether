"""Pull JSON out of LLM responses.

Models are asked for strict JSON, but they wrap it in prose or code fences
often enough that every structured consumer (salience judge, entity
confirmation, screen-vision extraction) funnels through one tolerant
extractor instead of each rolling its own.
"""

from __future__ import annotations

import json
from typing import Any


class JsonParseError(ValueError):
    """The response did not contain a usable JSON object."""


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from `text`.

    Tolerates ```json fences, leading prose, and trailing commentary.
    Raises JsonParseError when nothing object-shaped is there.
    """
    if not text or not text.strip():
        raise JsonParseError("empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # drop the fence line ("```json") and any closing fence
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        cleaned = cleaned.removesuffix("```").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise JsonParseError("no JSON object found")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise JsonParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise JsonParseError("response was not a JSON object")
    return parsed
