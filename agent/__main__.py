"""Command line: agent doctor."""
import sys


def main() -> int:
    if sys.version_info < (3, 11):
        print(f"Python 3.11 or newer is needed; this is {sys.version.split()[0]}. Next: install it, or use uv.")
        return 2
    from agent import doctor

    if sys.argv[1:] == ["doctor"]:
        return doctor.main()
    print("usage: agent doctor")
    return 2


if __name__ == "__main__":
    sys.exit(main())
