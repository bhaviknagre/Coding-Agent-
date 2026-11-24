"""Find secrets and private details in files before they are committed. Prints places, never values."""
import re
import subprocess
import sys
from pathlib import Path

RULES: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx))
    for name, rx in (
        ("google-key", r"AIza[0-9A-Za-z_\-]{35}|AQ\.[0-9A-Za-z_\-]{30,}"),
        ("openai-style-key", r"\bsk-[A-Za-z0-9_\-]{20,}"),
        ("groq-key", r"\bgsk_[A-Za-z0-9]{20,}"),
        ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
        ("aws-key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        ("slack-token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
        ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        (
            "assigned-secret",
            r"""(?i)(?:api_?key|secret|token|passw(?:or)?d)["']?\s*[:=]\s*["'][A-Za-z0-9+/_\-\.=]{16,}["']""",
        ),
        (
            "personal-email",
            r"[A-Za-z0-9._%+\-]+@(?!users\.noreply\.github\.com|example\.)[A-Za-z0-9\-]+\.[A-Za-z.]{2,}",
        ),
        ("home-path", r"/(?:Users|home)/[a-z][a-z0-9_\-]+/"),
    )
]
BLOCKED_NAMES = re.compile(r"(^|/)(\.env(\..*)?|id_[a-z0-9]+|.*\.(pem|key|p12|pfx))$")


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return (line number, rule) pairs."""
    return [(n, name) for n, line in enumerate(text.splitlines(), 1) for name, rx in RULES if rx.search(line)]


def scan_repo(root: Path) -> list[str]:
    """Scan working files and the staged copies, because they can differ."""

    def git(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

    found: set[str] = set()
    staged = git("ls-files", "-z", "--cached").decode().split("\0")
    names = {n for n in staged + git("ls-files", "-z", "-o", "--exclude-standard").decode().split("\0") if n}
    for name in sorted(names):
        if BLOCKED_NAMES.search(name):
            found.add(f"{name}: blocked-file-name")
        texts = [git("show", f":{name}")] if name in staged else []
        if (root / name).is_file():
            texts.append((root / name).read_bytes())
        for data in texts:
            found |= {f"{name}:{n}: {rule}" for n, rule in scan_text(data.decode("utf-8", "replace"))}
    return sorted(found)


if __name__ == "__main__":
    problems = scan_repo(Path.cwd())
    print("\n".join(problems) or "secret scan: nothing found")
    sys.exit(1 if problems else 0)
