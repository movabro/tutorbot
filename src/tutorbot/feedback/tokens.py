"""Conservative token estimation for feedback inputs."""
from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    wide = sum(ord(char) >= 0x1100 for char in text)
    return math.ceil(wide + (len(text) - wide) / 3.5)
