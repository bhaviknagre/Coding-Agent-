"""The project's own image: built from its Dockerfile, or from its requirements.txt, after the developer said yes."""
import hashlib
import io
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import IO

from agent import box, junit
from agent.approve import Answer, ask_human
from agent.receipt import visible

NAME = f"{box.LABEL}/project"
MOST = 64 * 1024  # a longer Dockerfile or list is not shown in full, so it is not built
# A package by name, with extras, versions and markers. No options, addresses or paths: those can fetch from
# anywhere or run the project's own files, which this list does not show.
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._ -]*\])?[^@/\\:]*")
# Wheels only: no package's own build code runs while the image is made.
FROM_LIST = f"""FROM {box.HELPER_IMAGE}
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --root-user-action=ignore --disable-pip-version-check \\
    -r /requirements.txt
"""


def _file(repo: Path, commit: str, name: str) -> bytes | None:
    """A file of the commit: the one in Git, never the working folder's."""
    entries, _ = box.list_commit(repo, commit)
    if not any(e.name == name and e.mode in ("100644", "100755") for e in entries):
        return None
    return box.git_bytes(repo, "cat-file", "blob", f"{commit}:{name}")


def dockerfile(repo: Path, commit: str) -> bytes | None:
    return _file(repo, commit, "Dockerfile")


def packages(text: bytes) -> tuple[list[str], list[str]]:
    """The packages of a requirements.txt, and the lines that are not a plain package."""
    good: list[str] = []
    bad: list[str] = []
    for line in text.decode("utf-8", "replace").splitlines():
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            (good if PACKAGE.fullmatch(line) else bad).append(line)
    return good, bad


def tag(repo: Path, commit: str) -> str:
    """Same files, same image: the name comes from the commit's tree."""
    tree = box.git_bytes(repo, "rev-parse", f"{commit}^{{tree}}").strip()
    return f"{NAME}:{hashlib.sha256(tree).hexdigest()[:16]}"


def question(text: bytes, commit: str) -> str:
    lines = [visible(line) for line in text.decode("utf-8", "replace").splitlines()]
    return "\n".join([
        "", "This project has a Dockerfile. The whole file:", "",
        *(f"  | {line}" for line in lines), "",
        f"Building it runs these commands in Docker, with network access, on the tracked files of commit {commit[:12]}",
        "(files that look secret are left out; your folder is not used). Tests then run in this image without network.",
        "Build this image? (no: the plain Python image is used)",
    ])


def list_question(names: list[str], commit: str) -> str:
    return "\n".join([
        "", f"This project has no Dockerfile, but its requirements.txt (commit {commit[:12]}) lists these packages:",
        "",
        *(f"  | {visible(n)}" for n in names), "",
        f"Building installs them from PyPI into {box.HELPER_IMAGE}, with network access, as ready-made wheels only",
        "(no package's own build code runs; nothing else of the project is used). Tests then run without network.",
        "Build this image? (no: the plain Python image is used)",
    ])


def choose(repo: Path, commit: str, given: str, ask: Callable[[str], Answer] = ask_human) -> str:
    """The image to use: --image, else the project's own after a yes, else plain Python."""
    if given:
        return given
    text = dockerfile(repo, commit)
    listed = None if text is not None else _file(repo, commit, "requirements.txt")
    if text is None and listed is None:
        lang = junit.language(repo, commit)
        if lang is None:
            return box.HELPER_IMAGE
        if not box.docker("images", "-q", lang.image):
            raise box.BoxError(junit.missing(lang.image))  # nothing is downloaded without the developer
        print(f"Image      {lang.image}: the image for {lang.name}, found from {lang.marker}")
        return lang.image
    name = tag(repo, commit)
    if box.docker("images", "-q", name):
        print(f"Image      {name}: built earlier from this commit's {'Dockerfile' if text else 'requirements.txt'},"
              " after your yes")
        return name
    if len(text or listed or b"") > MOST:
        print("Note       the file is too long to show in full, so it is not built: plain Python is used")
        return box.HELPER_IMAGE
    if text is not None:
        if not ask(question(text, commit)).yes:
            print("Note       not built: plain Python is used. Tests that need packages will fail.")
            return box.HELPER_IMAGE
        build(repo, commit, name)
        return name
    assert listed is not None  # noqa: S101 - for the type checker
    names, bad = packages(listed)
    if bad:
        print(f"Note       requirements.txt has lines that are not plain packages ({visible(bad[0])}), so it is not"
              " built: plain Python is used. For those, give the project a Dockerfile.")
        return box.HELPER_IMAGE
    if not names:
        return box.HELPER_IMAGE
    if not ask(list_question(names, commit)).yes:
        print("Note       not built: plain Python is used. Tests that need packages will fail.")
        return box.HELPER_IMAGE
    _build(name, lambda out: _list_context(names, out), [])
    return name


def _list_context(names: list[str], out: IO[bytes]) -> None:
    """Only the generated Dockerfile and the packages that were shown: the same list, not the file."""
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for file, text in (("Dockerfile", FROM_LIST), ("requirements.txt", "\n".join(names) + "\n")):
            data = text.encode()
            info = tarfile.TarInfo(file)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def build(repo: Path, commit: str, name: str) -> None:
    entries, left_out = box.list_commit(repo, commit)
    box.refuse_conflicts(entries)
    _build(name, lambda out: box._write_tar(repo, entries, out), left_out)


def _build(name: str, write: Callable[[IO[bytes]], None], left_out: list[str]) -> None:
    print(f"Building {name} (the first time can take minutes)…", flush=True)
    with tempfile.TemporaryFile() as context:
        write(context)
        context.seek(0)
        with box.uninterrupted():
            try:
                p = subprocess.run(
                    ["docker", "build", "--quiet", "--label", f"{box.LABEL}=image", "--tag", name, "-"],
                    stdin=context, capture_output=True, timeout=1800, start_new_session=True,
                )
            except subprocess.TimeoutExpired as e:
                raise box.BoxError("the image took more than 30 minutes to build") from e
    if p.returncode:
        said = visible(p.stderr.decode("utf-8", "replace").strip()[-800:])
        raise box.BoxError(f"the image did not build:\n{said}\nNext: fix it, commit, and start again")
    if left_out:
        print(f"Note       left out of the build: {', '.join(visible(x) for x in left_out[:5])}")
