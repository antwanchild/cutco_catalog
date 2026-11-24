"""Format GitHub release Markdown for Discord embed fields."""

import re
import sys


def format_notes(text: str) -> str:
    """Use bold section labels and join soft-wrapped prose and list entries."""
    result: list[str] = []
    in_code = False
    join_next = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            result.append(line)
            join_next = False
        elif in_code:
            result.append(line)
        elif not stripped:
            if result and result[-1]:
                result.append("")
            join_next = False
        elif heading := re.fullmatch(r"#{1,6}\s+(.+?)(?:\s+#+)?", stripped):
            result.append(f"**{heading.group(1)}**")
            join_next = False
        elif re.match(r"(?:[-*+]\s|\d+[.)]\s|>\s)", stripped):
            result.append(line)
            join_next = not stripped.startswith(">")
        elif join_next:
            result[-1] += " " + stripped
            join_next = not line.endswith("  ")
        else:
            result.append(stripped)
            join_next = not line.endswith("  ")
    return "\n".join(result).strip()


if __name__ == "__main__":
    print(format_notes(sys.stdin.read()))
