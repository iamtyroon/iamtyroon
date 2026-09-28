"""Regenerate the contributed-repos table in README.md from the GitHub API.

Queries issues and pull requests authored by USERNAME, ranks repositories by
how many contributions landed in each, and rewrites the region between the
CONTRIBUTED markers in README.md.

Runs on GitHub Actions (monthly) and can also be run locally:
    python scripts/update_contributed.py

Exit codes:
    0  README updated, or already up to date
    1  GitHub API failure or malformed README
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

USERNAME = os.environ.get("GH_USERNAME", "iamtyroon")
MAX_ROWS = 6
READ_REPO = "iamtyroon/iamtyroon"

MARKER_START = "<!-- CONTRIBUTED:START -->"
MARKER_END = "<!-- CONTRIBUTED:END -->"

API = "https://api.github.com"


def request(path, params=None):
    """GET a GitHub API path and return decoded JSON."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "update-contributed-script",
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


def tally_contributions():
    """Return {full_name: contribution_count} for issues + PRs authored by USERNAME."""
    counts = {}

    for kind in ("issue", "pr"):
        payload = request(
            "/search/issues",
            {
                "q": f"author:{USERNAME} type:{kind}",
                "per_page": 100,
                "sort": "updated",
            },
        )
        for item in payload.get("items", []):
            full_name = item.get("repository_url", "").rsplit("/", 2)[-2:]
            if len(full_name) != 2:
                continue
            repo = "/".join(full_name)
            # The profile repo itself is metadata, not a contribution.
            if repo.lower() == READ_REPO.lower():
                continue
            counts[repo] = counts.get(repo, 0) + 1

    return counts


def render_table(counts):
    """Build the markdown table, highest contribution count first."""
    if not counts:
        return "_No issues or pull requests found._"

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    lines = [
        "| Repo | Contributions |",
        "|---|---:|",
    ]
    for repo, count in ranked[:MAX_ROWS]:
        lines.append(
            f"| [{repo}](https://github.com/{repo}) | {count} |"
        )

    if len(ranked) > MAX_ROWS:
        remaining = len(ranked) - MAX_ROWS
        lines.append(f"| _+{remaining} more_ | |")

    return "\n".join(lines)


def splice_readme(table, total):
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
    print(f"README updated with {min(MAX_ROWS, total)} of {total} repos.")
    return True


def main():
    counts = tally_contributions()
    if not counts:
        # Never overwrite a populated table with an empty result on a
        # transient API problem.
        raise SystemExit("No contributions returned; leaving README untouched.")

    print(f"{USERNAME}: {len(counts)} repositories with contributions")
    for repo, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:3d}  {repo}")

    if splice_readme(render_table(counts), len(counts)):
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
