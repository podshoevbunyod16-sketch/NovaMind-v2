#!/usr/bin/env python3
"""Secret scanner for NovaMind CI.  Never prints a detected value.

Usage:
    python scripts/scan_secrets.py            # working tree (tracked files)
    python scripts/scan_secrets.py --history  # also scan diffs in git history

Exit code 1 means at least one high-confidence finding.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# High confidence only: random-looking provider keys, private keys, assignments
# with long literal secrets.  Placeholder values in .env.example are excluded.
PATTERNS = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "google_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "github_pat": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----"),
    "assigned_secret": re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|client[_-]?secret|access[_-]?token)"
        r"\s*[:=]\s*[\"']([^\"']{16,})[\"']"),
}
PLACEHOLDER = re.compile(r"(?i)(change|example|placeholder|your[_-]|dummy|test|fake|sentinel|<|\$\{|xxx|\*\*\*)")


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    return [item.decode("utf-8", "replace") for item in result.stdout.split(b"\0") if item]


def scan_text(text: str) -> list[str]:
    findings = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group(0)
            if value and PLACEHOLDER.search(value):
                continue
            findings.append(name)
    return sorted(set(findings))


def scan_tree(include_tests: bool = False) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for path in tracked_files():
        # Test fixtures contain deliberately fake provider-shaped strings to
        # exercise redaction.  Use --include-tests to scan them too.
        if not include_tests and (path.startswith("tests/") or path.startswith("evals/")):
            continue
        try:
            with open(path, "rb") as handle:
                blob = handle.read(2_000_000)
        except OSError:
            continue
        if b"\x00" in blob[:1024]:
            continue
        names = scan_text(blob.decode("utf-8", "replace"))
        for name in names:
            findings.append((path, name))
    return findings


def scan_history(include_tests: bool = False) -> list[tuple[str, str]]:
    """Scan patch lines only; output commit id + rule name, never the secret."""
    result = subprocess.run(
        ["git", "log", "--all", "-p", "--no-color", "--format=COMMIT %H", "--"],
        capture_output=True, check=False,
    )
    commit = "unknown"
    current_path = ""
    findings: list[tuple[str, str]] = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("COMMIT "):
            commit = line.split()[1][:12]
            continue
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if not include_tests and (current_path.startswith("tests/")
                                  or current_path.startswith("evals/")):
            continue
        for name in scan_text(line[1:]):
            findings.append((f"history:{commit}:{current_path}", name))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--include-tests", action="store_true")
    args = parser.parse_args()
    findings = scan_tree(include_tests=args.include_tests)
    if args.history:
        findings += scan_history(include_tests=args.include_tests)
    for path, name in findings:
        print(f"potential secret: file={path} rule={name} (value redacted)")
    print(f"secret scan: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
