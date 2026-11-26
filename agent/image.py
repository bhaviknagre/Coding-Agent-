"""The project's own image, built from its Dockerfile after the developer has read it and said yes."""
import hashlib
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from agent import box
from agent.approve import Answer, ask_human
from agent.receipt import visible

NAME = f"{box.LABEL}/project"
MOST = 64 * 1024  # a longer Dockerfile is not shown in full, so it is not built


def dockerfile(repo: Path, commit: str) -> bytes | None:
    """The Dockerfile of the commit: the one in Git, never the working folder's."""
    entries, _ = box.list_commit(repo, commit)
    if not any(e.name == "Dockerfile" and e.mode in ("100644", "100755") for e in entries):
        return None
    return box.git_bytes(repo, "cat-file", "blob", f"{commit}:Dockerfile")


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


def choose(repo: Path, commit: str, given: str, ask: Callable[[str], Answer] = ask_human) -> str:
    """The image to use: the one named with --image, else the project's own after a yes, else plain Python."""
    if given:
        return given
    text = dockerfile(repo, commit)
    if text is None:
        return box.HELPER_IMAGE
    name = tag(repo, commit)
    if box.docker("images", "-q", name):
        print(f"Image      {name}: built earlier from this commit's Dockerfile, after your yes")
        return name
    if len(text) > MOST:
        print("Note       the Dockerfile is too long to show in full, so it is not built: plain Python is used")
        return box.HELPER_IMAGE
    if not ask(question(text, commit)).yes:
        print("Note       not built: plain Python is used. Tests that need packages will fail.")
        return box.HELPER_IMAGE
    build(repo, commit, name)
    return name


def build(repo: Path, commit: str, name: str) -> None:
    entries, left_out = box.list_commit(repo, commit)
    box.refuse_conflicts(entries)
    print(f"Building {name} (the first time can take minutes)…", flush=True)
    with tempfile.TemporaryFile() as context:
        box._write_tar(repo, entries, context)
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
        raise box.BoxError(f"the Dockerfile did not build:\n{said}\nNext: fix it, commit, and start again")
    if left_out:
        print(f"Note       left out of the build: {', '.join(visible(x) for x in left_out[:5])}")
