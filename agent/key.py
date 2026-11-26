"""The API key: typed without being shown, kept in the macOS Keychain, read only by the launcher.

It is never written to a file, a log, a receipt, a recording, the screen, a command line or a container. Elsewhere
than macOS it is asked for again in each session and only kept in memory.
"""
import getpass
import os
import re
import subprocess
import sys

SERVICE = "coding-agent-v2"
SHAPE = re.compile(r"[A-Za-z0-9_.\-]{20,300}")


class KeyError_(Exception):
    """A message for the human. Never holds the key."""


def _keychain(given: str) -> str:
    """Tests set CODING_AGENT_KEYCHAIN, so that they can never reach the developer's real key."""
    return given or os.environ.get("CODING_AGENT_KEYCHAIN", "")


def _security(*args: str, keychain: str = "") -> subprocess.CompletedProcess[str]:
    chain = _keychain(keychain)
    return subprocess.run(["security", *args, *([chain] if chain else [])], capture_output=True, text=True, timeout=30)


def valid(key: str) -> bool:
    return bool(SHAPE.fullmatch(key))


def store(provider: str, key: str, keychain: str = "") -> None:
    if sys.platform != "darwin":
        raise KeyError_("Storing a key is only built for macOS. Next: the key is asked for in each session.")
    if not valid(key):
        raise KeyError_("That does not look like an API key (letters, digits, - _ . only). Nothing was stored.")
    # Through standard input, so the key is never in a command line that other programs can see.
    chain = _keychain(keychain)
    line = f"add-generic-password -U -s {SERVICE} -a {provider} -w {key}" + (f" {chain}" if chain else "") + "\n"
    p = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=30)
    if p.returncode or p.stderr.strip():
        raise KeyError_("The Keychain did not accept the key. Next: check in Keychain Access that it is unlocked.")


def load(provider: str, keychain: str = "") -> str:
    if sys.platform != "darwin":
        return ""
    p = _security("find-generic-password", "-s", SERVICE, "-a", provider, "-w", keychain=keychain)
    key = p.stdout.strip() if p.returncode == 0 else ""
    return key if valid(key) else ""


def exists(provider: str, keychain: str = "") -> bool:
    """Without -w the Keychain answers whether there is one, not what it is."""
    return sys.platform == "darwin" and _security("find-generic-password", "-s", SERVICE, "-a", provider,
                                                  keychain=keychain).returncode == 0


def remove(provider: str, keychain: str = "") -> bool:
    return sys.platform == "darwin" and _security("delete-generic-password", "-s", SERVICE, "-a", provider,
                                                  keychain=keychain).returncode == 0


def ask(provider: str) -> str:
    """Typed by the person, not shown. Only a person at a terminal can do this."""
    if not sys.stdin.isatty():
        raise KeyError_("A key can only be typed at a terminal. Next: run this in Terminal.")
    key = getpass.getpass(f"Paste your {provider} key (it is not shown), then Enter: ").strip()
    if not valid(key):
        raise KeyError_("That does not look like an API key. Nothing was stored.")
    return key


USAGE = """usage: agent key set|status|remove SERVICE      (SERVICE: {known}, or one you added)
       agent key list
       agent key add NAME --url https://HOST/PATH --model MODEL --company WHO [--per-minute N]
                     (any service in the OpenAI format; then: agent key set NAME)"""


def main(argv: list[str]) -> int:
    """agent key set|status|remove SERVICE, agent key list, agent key add NAME ..."""
    from agent import services

    known = services.all_services()
    usage = USAGE.format(known=", ".join(services.KNOWN))
    if argv[:1] == ["list"] and len(argv) == 1:
        for s in known.values():
            stored = "key stored" if exists(s.name) else "no key"
            print(f"{s.name:12} {stored:11} {s.model}  ({'tested' if s.tested else 'not tested yet'})")
        return 0
    if argv[:1] == ["add"]:
        import argparse

        p = argparse.ArgumentParser(prog="agent key add")
        p.add_argument("name")
        p.add_argument("--url", required=True)
        p.add_argument("--model", required=True)
        p.add_argument("--company", required=True, help="who runs the service: it receives your code")
        p.add_argument("--per-minute", type=int, default=10)
        a = p.parse_args(argv[1:])
        try:
            s = services.add(a.name, a.url, a.model, a.company, a.per_minute)
        except ValueError as e:
            print(f"Stopped: {e}")
            return 2
        print(f"Added {s.name} ({s.address}). No key is saved with it. Next: agent key set {s.name}")
        return 0
    if len(argv) != 2 or argv[0] not in ("set", "status", "remove") or argv[1] not in known:
        print(usage)
        return 2
    action, provider = argv
    try:
        if action == "set":
            store(provider, ask(provider))
            print("Stored in the macOS Keychain. It is never shown again; 'agent key remove' deletes it.")
        elif action == "status":
            print("A key is stored." if exists(provider) else "No key is stored.")
        else:
            print("Removed." if remove(provider) else "There was no key to remove.")
    except KeyError_ as e:
        print(f"Stopped: {e}")
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nNothing was stored.")
        return 2
    return 0

