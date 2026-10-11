#!/usr/bin/env python3
"""Check release metadata and report the Git identity used for attribution."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def first_line(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def inspect(root):
    issues = []
    version_path = root / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        issues.append("invalid_version")
    else:
        major, minor, _ = version.split(".")
        display_version = f"{major}.{minor}"
        if first_line(root / "README.md") not in {f"# Evolving Profile {display_version}", "# Evolving Profile"}:
            issues.append("readme_version_mismatch")
        if first_line(root / "NOTICE.md") != f"# Attribution — Evolving Profile {display_version}":
            issues.append("notice_version_mismatch")
        changelog = root / f"CHANGELOG-{display_version}.md"
        if not changelog.is_file():
            issues.append("missing_changelog")
        elif first_line(changelog) != f"# Evolving Profile {version}":
            issues.append("changelog_version_mismatch")
        flow_view = root / "console" / "src" / "components" / "flow-view.tsx"
        if flow_view.is_file():
            flow_text = flow_view.read_text(encoding="utf-8")
            if re.search(r"\bEP\s+\d+\.\d+\b", flow_text) and f"EP {display_version}" not in flow_text:
                issues.append("flow_badge_version_mismatch")

    branch = git(root, "branch", "--show-current")
    authors = git(root, "shortlog", "-sne", "HEAD")
    return {
        "status": "passed" if not issues else "failed",
        "version": version or None,
        "issues": issues,
        "git": {
            "branch": branch,
            "commit": git(root, "rev-parse", "HEAD"),
            "origin": git(root, "remote", "get-url", "origin"),
            "fork": git(root, "remote", "get-url", "fork"),
            "commit_authors": authors.splitlines() if authors else [],
        },
        "boundary": "git_authors_are_not_a_complete_contributor_or_license_audit",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = inspect(args.root.resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Release metadata: {report['status']} ({report['version'] or 'unknown'})")
        for issue in report["issues"]:
            print(f"- {issue}")
        print(f"Branch: {report['git']['branch'] or 'not a Git checkout'}")
        print(f"Origin: {report['git']['origin'] or 'unknown'}")
        print("Commit authors (verify attribution separately):")
        for author in report["git"]["commit_authors"]:
            print(f"- {author.strip()}")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
