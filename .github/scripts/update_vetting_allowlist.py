"""
Add contributors who have had enough pull requests merged in the organization
to the vetting allowlist.

Only users who authored a pull request merged since the "last-updated" time in
the allowlist header are considered, so that the check stays cheap. A user is
added if they have had at least --min-merged pull requests merged across the
organization, are not already listed (with or without a leading "-", which
explicitly excludes them), and are not a public member of the organization.
When users are added, the "last-updated" time is set to now.

Uses the GitHub token from the GITHUB_TOKEN environment variable, and only the
standard library.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

API = "https://api.github.com"
LAST_UPDATED = re.compile(r"^# last-updated: (\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2})?\s*$")


def github_get(url, params=None):
    """
    GET a GitHub API URL as JSON, waiting and retrying when rate limited.
    """
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                return json.load(response), response.headers
        except urllib.error.HTTPError as error:
            if error.code in (403, 429) and error.headers.get("X-RateLimit-Remaining") == "0":
                wait = max(int(error.headers.get("X-RateLimit-Reset", 0)) - time.time(), 0) + 5
            elif error.code in (403, 429) and "rate limit" in error.read().decode(errors="replace").lower():
                wait = int(error.headers.get("Retry-After", 60))
            else:
                raise
            print(f"Rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)


def paginate(url, params=None):
    params = dict(params or {}, per_page=100)
    while True:
        items, headers = github_get(url, params)
        yield from items
        match = re.search(r'<([^>]+)>; rel="next"', headers.get("Link", ""))
        if not match:
            return
        url, params = match.group(1), None


def search_merged_pull_requests(query):
    """
    Yield the merged pull requests matching a search query (at most the 1000
    the search API allows).
    """
    url = f"{API}/search/issues"
    params = {"q": f"type:pr is:merged {query}", "per_page": 100, "sort": "created", "order": "desc"}
    page = 1
    while True:
        result, _ = github_get(url, dict(params, page=page))
        if page == 1 and result["total_count"] > 1000:
            print(f"Warning: {result['total_count']} merged pull requests match, only the first 1000 are considered", file=sys.stderr)
        yield from result["items"]
        if len(result["items"]) < 100 or page * 100 >= 1000:
            return
        page += 1


def count_merged_pull_requests(org, user):
    result, _ = github_get(f"{API}/search/issues", {"q": f"org:{org} type:pr is:merged author:{user}", "per_page": 1})
    return result["total_count"]


def public_members(org):
    return {member["login"].lower() for member in paginate(f"{API}/orgs/{org}/public_members")}


def listed_names(lines):
    """
    The user names in the allowlist, lower-cased, whether allowed or
    explicitly excluded with a leading "-".
    """
    names = set()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line.lstrip("-").lstrip("@").lower())
    return names


def last_updated(lines):
    for line in lines:
        match = LAST_UPDATED.match(line)
        if match:
            return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
    return None


def add_names(lines, names):
    """
    Add names to the allowlist lines. If the existing names are in
    case-insensitive alphabetical order, the new ones are inserted in order,
    otherwise they are appended at the end.
    """
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    existing = [line for line in lines if line.strip() and not line.startswith("#")]
    if existing == sorted(existing, key=str.lower):
        # Rebuild the file as the header comments followed by the sorted names
        header_end = next((i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")), len(lines))
        return lines[:header_end] + sorted(existing + list(names), key=str.lower)
    return lines + sorted(names, key=str.lower)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allowlist", default="contributor-allowlist.txt", help="path of the allowlist file")
    parser.add_argument("--org", default="astropy", help="GitHub organization")
    parser.add_argument("--min-merged", type=int, default=2, help="merged pull requests needed to be added (default: 2)")
    parser.add_argument("--since", help="consider pull requests merged on or after this date (default: the last-updated header, or 30 days ago)")
    parser.add_argument("--body", help="write a Markdown summary of the additions to this file, for the pull request body")
    parser.add_argument("--dry-run", action="store_true", help="report but do not modify the allowlist")
    args = parser.parse_args()

    with open(args.allowlist) as f:
        lines = f.read().splitlines()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
    else:
        since = last_updated(lines) or datetime.now(UTC) - timedelta(days=30)
    # A day of margin, in case the previous update ran part way through a day
    since -= timedelta(days=1)

    print(f"Looking for pull requests merged in {args.org} since {since:%Y-%m-%d}", file=sys.stderr)

    already = listed_names(lines)
    members = public_members(args.org)

    candidates = {}
    for pull in search_merged_pull_requests(f"org:{args.org} merged:>={since:%Y-%m-%d}"):
        user = pull["user"]
        if user["type"] == "Bot" or user["login"].endswith("[bot]"):
            continue
        candidates.setdefault(user["login"].lower(), user["login"])

    print(f"{len(candidates)} contributors with pull requests merged since then", file=sys.stderr)

    additions = {}
    for login_lower, login in sorted(candidates.items()):
        if login_lower in already:
            continue
        if login_lower in members:
            print(f"  {login}: public member of {args.org}, skipping", file=sys.stderr)
            continue
        count = count_merged_pull_requests(args.org, login)
        if count >= args.min_merged:
            print(f"  {login}: {count} merged pull requests, adding", file=sys.stderr)
            additions[login] = count
        else:
            print(f"  {login}: {count} merged pull request(s), below {args.min_merged}", file=sys.stderr)

    if args.body:
        with open(args.body, "w") as f:
            if additions:
                f.write(f"The following contributors have had at least {args.min_merged} pull requests merged in the "
                        f"{args.org} organization and are not organization members, so this adds them to the vetting "
                        f"allowlist:\n\n")
                for login, count in sorted(additions.items(), key=lambda item: item[0].lower()):
                    f.write(f"* @{login} ({count} merged pull requests, "
                            f"https://github.com/pulls?q=org%3A{args.org}+type%3Apr+is%3Amerged+author%3A{login})\n")
            else:
                f.write("No contributors to add.\n")

    if not additions:
        print("Nothing to add", file=sys.stderr)
        return

    if args.dry_run:
        print(f"Would add: {', '.join(sorted(additions))}", file=sys.stderr)
        return

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    lines = [f"# last-updated: {now}" if LAST_UPDATED.match(line) else line for line in lines]
    if last_updated(lines) is None:
        lines.insert(0, f"# last-updated: {now}")
    lines = add_names(lines, additions)

    with open(args.allowlist, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Added {len(additions)} contributor(s) to {args.allowlist}", file=sys.stderr)


if __name__ == "__main__":
    main()
