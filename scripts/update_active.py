"""Regenerate the "projects I am working on" table in README.md.

Counts commits authored by USERNAME over a rolling window (default 7 days),
ranks repositories by commit count, and rewrites the region between the
ACTIVE markers in README.md.

Runs on GitHub Actions (weekly) and can also be run locally:
    python scripts/update_active.py
    python scripts/update_active.py --days 14

Exit codes:
    0  README updated, or already up to date
    1  GitHub API failure, or malformed README
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USERNAME = os.environ.get("GH_USERNAME", "iamtyroon")
WINDOW_DAYS = 7
MAX_ROWS = 5

MARKER_START = "<!-- ACTIVE:START -->"
MARKER_END = "<!-- ACTIVE:END -->"

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10


def request(path, params=None):
    """GET a GitHub API path and return decoded JSON."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "update-active-script",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"GitHub API {exc.code} on {path}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network failure on {path}: {exc.reason}") from exc


def tally_commits(since):
    """Return {full_name: commit_count} for commits authored since `since`."""
    counts = {}
    query = f"author:{USERNAME} author-date:>={since}"

    for page in range(1, MAX_PAGES + 1):
        payload = request(
            "/search/commits",
            {"q": query, "per_page": PER_PAGE, "page": page},
        )
        items = payload.get("items", [])
        for item in items:
            repo = (item.get("repository") or {}).get("full_name")
            if repo:
                counts[repo] = counts.get(repo, 0) + 1

        # Stop once a short page proves we are past the last result.
        if len(items) < PER_PAGE:
            break

    return counts


def render_table(counts, since):
    """Build the markdown table, highest commit count first."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    total = sum(counts.values())

    lines = [
        f"_Commits in the {WINDOW_DAYS} days since {since} - {total} total._",
        "",
        "| Repo | Commits |",
        "|---|---:|",
    ]
    for repo, count in ranked[:MAX_ROWS]:
        lines.append(f"| [{repo}](https://github.com/{repo}) | {count} |")

    if len(ranked) > MAX_ROWS:
        lines.append(f"| _+{len(ranked) - MAX_ROWS} more_ | |")

    return "\n".join(lines)


def splice_readme(table):
    """Replace the region between the markers in README.md with table."""
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )

    try:
        with open(readme_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SystemExit(f"Cannot read README.md: {exc}") from exc

    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )
    if not pattern.search(text):
        raise SystemExit(
            f"Markers missing from README.md. Expected {MARKER_START} and {MARKER_END}."
        )

    updated = pattern.sub(f"{MARKER_START}\n{table}\n{MARKER_END}", text)

    if updated == text:
        print("README already up to date.")
        return False

    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(updated)
    return True


def main():
    global WINDOW_DAYS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=WINDOW_DAYS,
        help="size of the rolling window in days (default 7)",
    )
    args = parser.parse_args()

    WINDOW_DAYS = args.days

    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime(
        "%Y-%m-%d"
    )

    counts = tally_commits(since)
    if not counts:
        # A quiet week is normal. Keep the last known table rather than
        # replacing a real snapshot with an empty one.
        raise SystemExit(
            f"No commits since {since}; leaving README untouched."
        )

    print(f"{USERNAME} since {since}: {len(counts)} repositories")
    for repo, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:3d}  {repo}")

    changed = splice_readme(render_table(counts, since))
    print("README updated." if changed else "No change.")
    sys.exit(0)


if __name__ == "__main__":
    main()
