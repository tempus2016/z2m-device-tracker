"""Community device ratings: check new rating issues, and rebuild community_ratings.json from them.

Usage:
    python scripts/community_ratings.py check EVENT.json   check the issue in a GitHub `issues` event
    python scripts/community_ratings.py build              rebuild the ratings file from GitHub
    python scripts/community_ratings.py build issues.json  rebuild from a saved list of issues

`check` prints a JSON object, {"valid": bool, "message": markdown for the issue comment}.
`build` reads every issue labelled `rating` (except `invalid-rating` and `rating-rejected`),
keeps the newest report per person per model, writes z2m_tracker/app/community_ratings.json
and prints a one-line summary. It needs GITHUB_TOKEN (read access) unless given a file.

The report format lives in z2m_tracker/app/community.py, so the app and this script always agree.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "z2m_tracker" / "app"))

import community  # noqa: E402 - needs the path above

TARGET = ROOT / "z2m_tracker" / "app" / "community_ratings.json"
API = f"https://api.github.com/repos/{community.REPO}/issues"
EXCLUDED = {"invalid-rating", "rating-rejected"}
MAX_PAGES = 100


def read_issue(issue: dict) -> tuple[dict | None, list[str]]:
    """The clean report in an issue, with its comment and date, or None and the problems."""
    sections = community.parse_issue(issue.get("body") or "")
    report, errors = community.validate(community.parse_report(sections.get("report", "")))
    if report is None:
        return None, errors
    report["comment"] = community.clean_comment(sections.get("comment", ""))
    report["date"] = str(issue.get("created_at") or "")[:10] or None
    return report, []


def check(event_path: str) -> dict:
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    report, errors = read_issue(event.get("issue") or {})
    if report is None:
        problems = "\n".join(f"- {e}" for e in errors)
        return {
            "valid": False,
            "message": "Thanks for the rating. The report couldn't be read, so it isn't counted yet:\n\n"
            f"{problems}\n\nThe easiest fix is to open **Share your experience** from the device's **Community** tab "
            "in the Zigbee Tracker sidebar again, which fills the report in for you. Editing this issue so the "
            "report is right also works: it's checked again after every edit.",
        }
    # In a code span, so a vendor or model can't add links or @mentions to the bot's comment.
    name = f"{report['vendor']} {report['model']}".replace("`", "'")
    return {
        "valid": True,
        "message": f"Thanks for rating the `{name}`. It's counted in the community "
        "ratings from the next weekly rebuild, which reaches the Zigbee Tracker within a day after that. "
        "To change your rating later, share it again from the sidebar: your newest one replaces this one.",
    }


def fetch_issues() -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "z2m-tracker-ratings"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    issues: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{API}?labels={community.LABEL}&state=all&per_page=100&page={page}&sort=created&direction=desc"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
            batch = json.loads(response.read().decode("utf-8"))
        issues.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
    return issues


def collect(issues: list[dict]) -> tuple[list[dict], int]:
    """Valid reports, newest first, one per GitHub account per model and one per install per model."""
    reports, skipped = [], 0
    seen_user: set[tuple[str, str]] = set()
    seen_reporter: set[tuple[str, str]] = set()
    for issue in sorted(issues, key=lambda i: str(i.get("created_at") or ""), reverse=True):
        labels = {str(label.get("name")) for label in issue.get("labels") or [] if isinstance(label, dict)}
        if community.LABEL not in labels or labels & EXCLUDED:
            skipped += 1
            continue
        report, _ = read_issue(issue)
        if report is None:
            skipped += 1
            continue
        key = community.model_key(report["vendor"], report["model"])
        user = str((issue.get("user") or {}).get("login") or "").lower()
        if (user, key) in seen_user or (report["reporter"], key) in seen_reporter:
            skipped += 1
            continue
        seen_user.add((user, key))
        seen_reporter.add((report["reporter"], key))
        reports.append(report)
    return reports, skipped


def build(argv: list[str]) -> int:
    issues = json.loads(Path(argv[0]).read_text(encoding="utf-8")) if argv else fetch_issues()
    reports, skipped = collect(issues)
    models = community.aggregate(reports)
    old = json.loads(TARGET.read_text(encoding="utf-8")) if TARGET.exists() else {}
    if old.get("models") == models:
        print(f"community_ratings.json: no change ({len(models)} models)")
        return 0
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = community.ratings_file(models, updated)
    TARGET.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    rated = sum(1 for m in models.values() if m["verdict"])
    print(
        f"community_ratings.json: {len(old.get('models') or {})} -> {len(models)} models, "
        f"{len(reports)} reports, {rated} with a verdict, {skipped} issues skipped"
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "check":
        print(json.dumps(check(argv[2])))
        return 0
    if len(argv) >= 2 and argv[1] == "build":
        return build(argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
