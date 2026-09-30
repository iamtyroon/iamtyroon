"""Regenerate the "projects I am working on" table in README.md.

Counts commits per repository over a rolling window (default 7 days) and
rewrites the region between the ACTIVE markers in README.md.

Two things this has to work around:

1. The commit search API cannot see private repositories, so repositories
   are enumerated via /user/repos and queried one by one instead.

2. The per-repo `author=` query parameter only matches commits linked to a
   GitHub account. Commits made through an agent identity (for example
   `Codex <codex@local>`) have a null `author` and are silently dropped by
   that filter, so commits are fetched unfiltered and matched on
   commit.author.name here.

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
MAX_ROWS = 6

MARKER_START = "<!-- ACTIVE:START -->"
MARKER_END = "<!-- ACTIVE:END -->"

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 5

# Commits land under the account owner and under agent identities. The
# server-side author filter only matches commits linked to a GitHub
# account, so unlinked agent commits are matched client-side by name.
#
# AUTHOR_LOGINS matches the linked GitHub account (robust: it does not
# depend on how the local git user.name happens to be spelled).
# AUTHOR_NAMES is the fallback for commits with no linked account.
AUTHOR_LOGINS = {
    login.strip().casefold()
    for login in os.environ.get("GH_AUTHOR_LOGINS", USERNAME).split(",")
    if login.strip()
}
AUTHOR_NAMES = {
    name.strip().casefold()
    for name in os.environ.get(
        "GH_AUTHOR_NAMES", "Tesfaalem Nahom,%s,Codex" % USERNAME
    ).split(",")
    if name.strip()
}


def _headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "update-active-script",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def request(path, params=None, tolerate=False):
    """GET a GitHub API path and return decoded JSON.

    With tolerate=True, empty/missing repositories return an empty list
    instead of raising, so one dead repo cannot abort the whole run.
    """
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if tolerate and exc.code in (404, 409, 422):
            return []
        body = exc.read().decode("utf-8", "replace")[:200]
        raise SystemExit("GitHub API %d on %s: %s" % (exc.code, path, body)) from exc
    except urllib.error.URLError as exc:
        raise SystemExit("Network failure on %s: %s" % (path, exc.reason)) from exc


def list_owned_repos():
    """Return [{full_name, private}] for every repo the token can read."""
    repos = []
    for page in range(1, MAX_PAGES + 1):
        batch = request(
            "/user/repos",
            {
                "affiliation": "owner",
                "per_page": PER_PAGE,
                "page": page,
                "sort": "pushed",
            },
        )
        if not batch:
            break
        for repo in batch:
            repos.append(
                {
                    "full_name": repo["full_name"],
                    "private": bool(repo.get("private")),
                }
            )
        if len(batch) < PER_PAGE:
            break
    return repos


def is_mine(commit):
    """True when the commit was authored by the account owner or an agent.

    Prefers the linked GitHub login, because the commit author name is
    whatever `git config user.name` happened to be set to and varies
    between machines. Falls back to the raw name for commits that carry
    no linked account, such as agent-authored ones.
    """
    login = (commit.get("author") or {}).get("login")
    if login:
        return login.casefold() in AUTHOR_LOGINS

    name = ((commit.get("commit") or {}).get("author") or {}).get("name") or ""
    return name.strip().casefold() in AUTHOR_NAMES


def commits_in_window(full_name, since):
    """Return this account's commits in the window, matched by author name."""
    commits = request(
        "/repos/%s/commits" % full_name,
        {"since": since, "per_page": PER_PAGE},
        tolerate=True,
    )
    return [c for c in commits if is_mine(c)]


def commit_line_stats(full_name, sha):
    """Return (additions, deletions) for one commit, or (0, 0) if unavailable."""
    commit = request(
        "/repos/%s/commits/%s" % (full_name, sha), tolerate=True
    )
    if not commit:
        return 0, 0
    stats = commit.get("stats") or {}
    return int(stats.get("additions", 0)), int(stats.get("deletions", 0))


def gather(days):
    """Return (results, since) for the window."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    results = []
    for repo in list_owned_repos():
        commits = commits_in_window(repo["full_name"], since)
        if not commits:
            continue

        additions = 0
        deletions = 0
        for commit in commits:
            add, dele = commit_line_stats(repo["full_name"], commit["sha"])
            additions += add
            deletions += dele

        results.append(
            {
                "repo": repo["full_name"],
                "private": repo["private"],
                "commits": len(commits),
                "additions": additions,
                "deletions": deletions,
            }
        )

    results.sort(key=lambda r: (-r["commits"], r["repo"].lower()))
    return results, since


def render(results, since, days):
    """Build the markdown table."""
    total_commits = sum(r["commits"] for r in results)
    total_add = sum(r["additions"] for r in results)
    total_del = sum(r["deletions"] for r in results)

    lines = [
        "**%d commits** - **+%s added** / **-%s removed** across public and "
        "private repos in the last %d days (since %s)."
        % (
            total_commits,
            "{:,}".format(total_add),
            "{:,}".format(total_del),
            days,
            since[:10],
        ),
        "",
        "| Project | Commits | Lines |",
        "|---|---:|---:|",
    ]

    for entry in results[:MAX_ROWS]:
        name = entry["repo"]
        label = name.split("/", 1)[1]
        if entry["private"]:
            label += " 🔒"
        lines.append(
            "| [%s](https://github.com/%s) | %d | +%s / -%s |"
            % (
                label,
                name,
                entry["commits"],
                "{:,}".format(entry["additions"]),
                "{:,}".format(entry["deletions"]),
            )
        )

    if len(results) > MAX_ROWS:
        lines.append("| _+%d more_ | | |" % (len(results) - MAX_ROWS))

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
        raise SystemExit("Cannot read README.md: %s" % exc) from exc

    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )
    if not pattern.search(text):
        raise SystemExit(
            "Markers missing from README.md. Expected %s and %s."
            % (MARKER_START, MARKER_END)
        )

    updated = pattern.sub("%s\n%s\n%s" % (MARKER_START, table, MARKER_END), text)

    if updated == text:
        print("README already up to date.")
        return False

    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(updated)
    return True


def main():
    parser = argparse.ArgumentParser(description="Update the active projects table.")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="size of the rolling window in days (default 7)",
    )
    args = parser.parse_args()

    results, since = gather(args.days)
    if not results:
        # A quiet week is normal. Keep the last known table rather than
        # replacing a real snapshot with an empty one.
        raise SystemExit("No commits since %s; leaving README untouched." % since)

    print("%s: %d active repositories since %s" % (USERNAME, len(results), since))
    for entry in results:
        lock = " (private)" if entry["private"] else ""
        print(
            "  %4d commits  +%-9s -%-9s %s%s"
            % (
                entry["commits"],
                "{:,}".format(entry["additions"]),
                "{:,}".format(entry["deletions"]),
                entry["repo"],
                lock,
            )
        )

    changed = splice_readme(render(results, since, args.days))
    print("README updated." if changed else "No change.")
    sys.exit(0)


if __name__ == "__main__":
    main()