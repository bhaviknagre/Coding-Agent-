"""Hide secrets in text before it is stored, shown or sent.

Two kinds: exact secrets we were told about, and text that looks like a secret.
Changed forms (base64, reversed, spaced) are NOT found. The main protection is
that keys never enter the agent at all.
"""
import re

from agent import scan

MASK = "[MASKED]"
# Emails and home folders are blocked in commits, but are normal text in a project's own files.
PATTERNS = [rx for name, rx in scan.RULES if name not in ("personal-email", "home-path")]
HOLD = 4096  # a pattern secret longer than this, split across pieces, can be missed
LIMIT = 1_000_000  # never hold more than this


def mask(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern in PATTERNS:
        text = pattern.sub(MASK, text)
    return text


class StreamMask:
    """Mask text that arrives in pieces. The end is held back, because a secret may be cut in two."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = tuple(s for s in secrets if s)
        self.hold = max([HOLD, *(len(s) for s in self.secrets)])
        self.patterns = PATTERNS + [re.compile(re.escape(s)) for s in self.secrets]
        self.waiting = ""

    def feed(self, piece: str) -> str:
        self.waiting += piece
        cut = len(self.waiting) - self.hold
        if cut < self.hold:
            return ""
        if len(self.waiting) < LIMIT:
            # Never cut inside a secret: move back until no match lies across the cut.
            matches = [m.span() for rx in self.patterns for m in rx.finditer(self.waiting)]
            while across := [start for start, stop in matches if start < cut < stop]:
                cut = min(across)
        ready, self.waiting = self.waiting[:cut], self.waiting[cut:]
        return mask(ready, self.secrets)

    def end(self) -> str:
        last, self.waiting = mask(self.waiting, self.secrets), ""
        return last
