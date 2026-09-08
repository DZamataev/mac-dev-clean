from __future__ import annotations

import math
import re

#: Docker and Homebrew report SI units, not binary ones: `15.03GB` means
#: 15.03 * 10^9 bytes, not 15.03 GiB. Converting with 1024 would overstate
#: every figure by 7% and make our number disagree with the tool's own.
_MULTIPLIERS = (
    ("TB", 1000 ** 4),
    ("GB", 1000 ** 3),
    ("MB", 1000 ** 2),
    ("KB", 1000),
    ("B", 1),
)

_VALUE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)")


def parse_tool_size(text: str) -> int:
    """Convert a tool-reported size such as `15.03GB (68%)` into bytes.

    Returns 0 for anything unparseable. A tool that reports a figure we cannot
    read must degrade to "no estimate", never to a guess: the estimate drives
    what the user sees before confirming a destructive command.
    """
    if not text:
        return 0
    match = _VALUE.match(text)
    if match is None:
        return 0

    try:
        value = float(match.group(1))
    except ValueError:
        return 0
    if not math.isfinite(value) or value <= 0:
        return 0

    unit = match.group(2).upper()
    if not unit:
        return int(value)
    for suffix, multiplier in _MULTIPLIERS:
        if unit == suffix:
            result = value * multiplier
            if not math.isfinite(result):
                return 0
            return int(result)
    return 0
