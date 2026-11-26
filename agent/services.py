"""Model services in the OpenAI format: the known ones, and any https service the developer adds.

Each has its own key slot in the Keychain (named like the service), its own address, default model, speed limit
and notice about where the code goes. Only Gemini is tested; the others are configured from their public pages.
"""
import json
import re
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from agent.verify import data_folder


class Service(NamedTuple):
    name: str
    address: str
    model: str
    per_minute: int  # calls a minute until the service's own answer says otherwise
    company: str  # who receives the code
    free_tier_note: str = ""
    busy: tuple[str, ...] = ()  # lighter models for when the default is busy or out of quota
    tested: bool = False


KNOWN = {s.name: s for s in (
    Service("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-3.8-flash",
            5, "Google", "On the free tier Google may use it to improve its products, and people may read it",
            ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"), tested=True),
    Service("groq", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", 30, "Groq"),
    Service("openrouter", "https://openrouter.ai/api/v1/chat/completions", "meta-llama/llama-3.3-70b-instruct:free",
            20, "OpenRouter and the company that runs the chosen model",
            "Free models on OpenRouter may keep and use what is sent"),
    Service("github", "https://models.github.ai/inference/chat/completions", "openai/gpt-4.1-mini", 10,
            "GitHub (Microsoft) and the company of the chosen model"),
    Service("openai", "https://api.openai.com/v1/chat/completions", "gpt-4.1-mini", 30, "OpenAI",
            "OpenAI is paid: each call costs money"),
    Service("mistral", "https://api.mistral.ai/v1/chat/completions", "mistral-small-latest", 30, "Mistral AI"),
)}
NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
RESERVED = {"script", "recorded", "fake"}


def _saved() -> Path:
    return data_folder() / "services.json"


def added() -> dict[str, Service]:
    try:
        rows = json.loads(_saved().read_text())
        return {n: Service(n, r["address"], r["model"], int(r.get("per_minute", 10)), r["company"])
                for n, r in rows.items() if NAME.fullmatch(n) and n not in KNOWN}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def all_services() -> dict[str, Service]:
    return {**KNOWN, **added()}


def find(name: str) -> Service | None:
    return all_services().get(name)


def check_address(address: str) -> str:
    """Only https, a real host name, no name or password in it: the key goes there."""
    u = urlsplit(address)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment or u.query:
        raise ValueError("the address must be https://HOST/PATH, without a name, password, ? or #")
    return address


def add(name: str, address: str, model: str, company: str, per_minute: int = 10) -> Service:
    """A service of the developer's own, saved without any key."""
    if not NAME.fullmatch(name) or name in RESERVED:
        raise ValueError("a service name is 2-31 small letters, digits or -, starting with a letter")
    if name in KNOWN:
        raise ValueError(f"'{name}' is already known; use another name")
    if not model.strip() or not company.strip() or not 1 <= per_minute <= 600:
        raise ValueError("give a model, who runs the service (--company), and calls a minute from 1 to 600")
    check_address(address)
    path = _saved()
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        rows = {}
    rows[name] = {"address": address, "model": model.strip(), "per_minute": per_minute, "company": company.strip()}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1))
    return Service(name, address, model.strip(), per_minute, company.strip())
