"""Publish DQ-Stack to a Hugging Face Space (Docker), with PostgreSQL on Neon.

    pip install huggingface_hub
    HF_TOKEN=hf_... python scripts/deploy_hf.py --space your-name/dqstack

Creates the Space if it does not exist, then uploads only what the image needs: the
code, the front ends, the site, the requirements, and deploy/spaces/ (its Dockerfile,
start.sh and the Space's README) at the Space's root. Tests, docs, data and the landing
page's source photographs stay behind. The GitHub workflow hf-space.yml runs this after
every green CI run on main. Steps: deploy/HUGGINGFACE.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ["dqstack", "ui", "webapp", "site"]
FILES = ["alembic.ini", "requirements.txt", "requirements-app.txt", "constraints.txt"]
TEXT = {".sh", ".py", ".txt", ".ini", ".md", ".js", ".css", ".html", ".json", ".svg", ""}


def space_host(space: str) -> str:
    """The address Hugging Face gives a Space: owner-name.hf.space, lower case."""
    owner, name = space.split("/", 1)
    return f"{owner}-{name}".lower().replace("_", "-").replace(".", "-") + ".hf.space"


def _skip(directory: str, names: list[str]) -> set[str]:
    rel = Path(directory).resolve().relative_to(ROOT).as_posix()
    out = {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}
    if rel == "ui":
        out.add("data")                  # the planner's generated bundle, not source
    return out


def _lf(path: Path) -> None:
    """Linux needs LF line endings; a Windows checkout may have CRLF."""
    data = path.read_bytes()
    if b"\r\n" in data:
        path.write_bytes(data.replace(b"\r\n", b"\n"))


def build(bundle: Path, space: str) -> None:
    for d in CODE:
        shutil.copytree(ROOT / d, bundle / d, ignore=_skip)
    for f in FILES:
        shutil.copy2(ROOT / f, bundle / f)
    spaces = ROOT / "deploy" / "spaces"
    shutil.copy2(spaces / "Dockerfile", bundle / "Dockerfile")
    shutil.copy2(spaces / "start.sh", bundle / "start.sh")
    readme = (spaces / "README.md").read_text(encoding="utf-8")
    (bundle / "README.md").write_text(
        readme.replace("{{DIRECT_URL}}", "https://" + space_host(space)), encoding="utf-8")
    for p in bundle.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT and p.name != "README.md":
            _lf(p)
    _lf(bundle / "README.md")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--space", default=os.environ.get("HF_SPACE"),
                    help="owner/name of the Space, e.g. your-name/dqstack (or set HF_SPACE)")
    ap.add_argument("--dry-run", action="store_true", help="build the bundle and list it only")
    args = ap.parse_args(argv)
    if not args.space or "/" not in args.space:
        ap.error("give --space owner/name (or set HF_SPACE)")

    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "local"

    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "space"
        bundle.mkdir()
        build(bundle, args.space)
        files = [p for p in bundle.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        print(f"bundle: {len(files)} files, {size / 1e6:.1f} MB")
        if args.dry_run:
            for p in sorted(files)[:40]:
                print("  ", p.relative_to(bundle).as_posix())
            return 0

        from huggingface_hub import HfApi
        token = os.environ.get("HF_TOKEN")      # or a saved `huggingface-cli login`
        api = HfApi(token=token)
        api.create_repo(args.space, repo_type="space", space_sdk="docker", exist_ok=True)
        api.upload_folder(
            folder_path=str(bundle), repo_id=args.space, repo_type="space",
            commit_message=f"Deploy {sha}",
            # Replace the code wholesale, so a file deleted here is deleted there too.
            delete_patterns=[f"{d}/*" for d in CODE])
    print(f"published {sha} to https://huggingface.co/spaces/{args.space}")
    print(f"the app will be at https://{space_host(args.space)} once the build finishes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
