"""Command line: agent doctor, verify, merge, discard."""
import sys

USAGE = """usage: agent [COMMAND]
  (nothing)         open the chat in this repository, like Claude Code
  doctor            check what is needed
  demo [--keep]     see it work on a made-up project, without a key (about 1 minute)
  key set gemini    store your Gemini key in the macOS Keychain (typed, not shown)
  model-test gemini try the real model on a made-up project; marks it TESTED or EXPERIMENTAL
  run "REQUEST"     let the agent change this repository on a new branch, then check it
  verify BRANCH     check a branch in fresh containers (no key needed)
  merge BRANCH      show how to merge; only you merge
  discard BRANCH    delete an agent branch, after asking"""


def main() -> int:  # this file must stay readable for old Python versions
    if sys.version_info < (3, 11):
        print(f"Python 3.11 or newer is needed; this is {sys.version.split()[0]}. Next: install it, or use uv.")
        return 2
    import sqlite3

    from agent import box, chat, demo, doctor, gitsafe, key, mask, screen, verify

    commands = {
        "": chat.main,
        "demo": demo.demo,
        "model-test": demo.model_test,
        "run": screen.run,
        "verify": verify.verify,
        "merge": verify.merge,
        "discard": verify.discard,
    }
    name, rest = (sys.argv[1:2] or [""])[0], sys.argv[2:]
    if name == "doctor" and not rest:
        return doctor.main()
    if name == "key":
        return key.main(rest)
    if name in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if name.startswith("-"):  # options of the chat, such as --model
        name, rest = "", sys.argv[1:]
    if name not in commands:
        print(USAGE)
        return 2
    box.signals_stop_cleanly()
    try:
        if name in ("", "run", "verify", "demo", "model-test"):
            box.clean_leftovers()
        return commands[name](rest)
    except (verify.Stop, box.BoxError, gitsafe.Refused, ValueError, OSError, sqlite3.Error) as e:
        print("Stopped: " + mask.mask(verify.visible(e)))
        return 2
    except KeyboardInterrupt:
        print("\nCancelled. Everything that was started has been removed.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
