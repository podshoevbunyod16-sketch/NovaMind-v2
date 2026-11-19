#!/usr/bin/env python3
"""Fail when a change introduces new Ruff diagnostic fingerprints."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath


def run_ruff(ruff: str, root: Path, repo: Path) -> list[dict]:
    result = subprocess.run(
        [ruff, "check", str(root), "--output-format", "json"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Ruff failed ({result.returncode}): {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def fingerprints(diagnostics: list[dict], root: Path) -> Counter[tuple[str, str, str]]:
    found: Counter[tuple[str, str, str]] = Counter()
    for diagnostic in diagnostics:
        filename = Path(diagnostic["filename"])
        relative = os.path.relpath(filename, root) if filename.is_absolute() else filename.as_posix()
        found[(Path(relative).as_posix(), diagnostic["code"], diagnostic["message"])] += 1
    return found


def archive_revision(repo: Path, revision: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination.parent / "baseline.tar"
    with archive_path.open("wb") as output:
        result = subprocess.run(
            ["git", "archive", "--format=tar", revision],
            cwd=repo,
            check=False,
            stdout=output,
            stderr=subprocess.PIPE,
        )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())

    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Base revision archive contains an unsafe path")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Cannot read archived file: {member.name}")
                with source, target.open("wb") as output:
                    output.write(source.read())
            else:
                raise RuntimeError(f"Unsupported base archive entry: {member.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git commit to use as the lint baseline")
    parser.add_argument("--ruff", default="ruff", help="Ruff executable (default: ruff on PATH)")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ruff-baseline-") as temporary:
        base_root = Path(temporary) / "tree"
        archive_revision(repo, args.base, base_root)
        baseline = fingerprints(run_ruff(args.ruff, base_root, repo), base_root)
        current = fingerprints(run_ruff(args.ruff, repo, repo), repo)

    introduced = current - baseline
    resolved = baseline - current
    print(
        "Ruff baseline: "
        f"{sum(baseline.values())} findings; current: {sum(current.values())}; "
        f"resolved: {sum(resolved.values())}; introduced: {sum(introduced.values())}"
    )
    for (filename, code, message), count in sorted(introduced.items()):
        print(f"NEW {count}x {filename} [{code}] {message}")
    if introduced:
        print("Failing because this change introduces Ruff diagnostics.", file=sys.stderr)
        return 1
    print("No new Ruff diagnostics; existing lint debt remains non-blocking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
