"""
Add contributors who have had enough pull requests merged in the organization
to the vetting allowlist.

Only users who authored a pull request merged since the "last-updated" time in
the allowlist header are considered, so that the check stays cheap. A user is
added if they have had at least --min-merged pull requests merged across the
organization, are not already listed (with or without a leading "-", which
explicitly excludes them), and are not a public member of the organization.
When users are added, the "last-updated" time is set to now.

Uses the GitHub GraphQL API, which allows the merged pull request counts of
many users to be fetched in one request, with the token from the GITHUB_TOKEN
environment variable (required), and only the standard library.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

API = "https://api.github.com"
LAST_UPDATED = re.compile(r"^# last-updated: (\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2})?\s*$")

# How many users to count merged pull requests for in a single GraphQL request
COUNT_BATCH_SIZE = 50


def github_headers():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN must be set (the GraphQL API requires authentication)")
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def wait_for_rate_limit(headers):
    wait = max(int(headers.get("X-RateLimit-Reset", 0)) - time.time(), 0) + 5
    print(f"Rate limited, waiting {wait:.0f}s", file=sys.stderr)
    time.sleep(wait)


def github_request(url, data=None):
    """
    Make a GitHub API request (GET, or POST with a JSON body) and return the
    decoded JSON response and the headers, waiting and retrying when rate
    limited.
    """
    while True:
        request = urllib.request.Request(url, headers=github_headers(), data=data, method="POST" if data else "GET")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response), response.headers
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                wait_for_rate_limit(error.headers)
                continue
            raise


def graphql(query):
    """
    Run a GraphQL query and return its ``data``, raising on errors (other
    than rate limiting, which is waited out).
    """
    while True:
        result, headers = github_request(f"{API}/graphql", json.dumps({"query": query}).encode())
        errors = result.get("errors") or []
        if any(error.get("type") == "RATE_LIMITED" for error in errors):
            wait_for_rate_limit(headers)
            continue
        if errors:
            raise RuntimeError("GraphQL query failed: " + "; ".join(error.get("message", str(error)) for error in errors))
        return result["data"]


def paginate(url):
    """
    Yield the items of a paginated REST list endpoint.
    """
    url += "?per_page=100"
    while True:
        items, headers = github_request(url)
        yield from items
        match = re.search(r'<([^>]+)>; rel="next"', headers.get("Link", ""))
        if not match:
            return
        url = match.group(1)


def search_merged_pull_requests(query):
    """
    Yield the ``author`` of each merged pull request matching a search query,
    and the total number of matches as the first item. At most the 1000
    results the search API allows are returned.
    """
    cursor = "null"
    while True:
        data = graphql(f"""
            {{
              search(type: ISSUE, first: 100, after: {cursor}, query: {json.dumps("type:pr is:merged " + query)}) {{
                issueCount
                pageInfo {{ hasNextPage endCursor }}
                nodes {{ ... on PullRequest {{ author {{ __typename login }} }} }}
              }}
            }}
        """)["search"]
        if cursor == "null":
            yield data["issueCount"]
        for node in data["nodes"]:
            yield node.get("author")
        if not data["pageInfo"]["hasNextPage"]:
            return
        cursor = json.dumps(data["pageInfo"]["endCursor"])


def authors_of_merged_pull_requests(org, start, end, authors=None):
    """
    The logins of the (non-bot) authors of pull requests merged in the
    organization between two dates (inclusive), in first-seen order, as a
    dict mapping lower-case login to login. Deleted users have no author and
    are skipped.

    The search API returns at most 1000 results per query, so the date range
    is split in two until each part is below that.
    """
    if authors is None:
        authors = {}
    results = search_merged_pull_requests(f"org:{org} merged:{start:%Y-%m-%d}..{end:%Y-%m-%d}")
    total = next(results)
    if total > 1000 and start < end:
        middle = start + (end - start) / 2
        authors_of_merged_pull_requests(org, start, middle, authors)
        authors_of_merged_pull_requests(org, middle + timedelta(days=1), end, authors)
        return authors
    if total > 1000:
        print(f"Warning: {total} merged pull requests on {start:%Y-%m-%d}, only the first 1000 are considered", file=sys.stderr)
    for author in results:
        if author and author["__typename"] != "Bot" and not author["login"].endswith("[bot]"):
            authors.setdefault(author["login"].lower(), author["login"])
    return authors


def count_merged_pull_requests(org, users):
    """
    The number of merged pull requests in the organization of each of the
    given users, as a dict, fetched in batches of aliased searches.
    """
    counts = {}
    users = list(users)
    for start in range(0, len(users), COUNT_BATCH_SIZE):
        batch = users[start : start + COUNT_BATCH_SIZE]
        fields = "\n".join(
            f"u{i}: search(type: ISSUE, first: 1, query: {json.dumps(f'org:{org} type:pr is:merged author:{user}')}) {{ issueCount }}"
            for i, user in enumerate(batch)
        )
        data = graphql(f"{{\n{fields}\n}}")
        for i, user in enumerate(batch):
            counts[user] = data[f"u{i}"]["issueCount"]
    return counts


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

    candidates = authors_of_merged_pull_requests(args.org, since, datetime.now(UTC))
    print(f"{len(candidates)} contributors with pull requests merged since then", file=sys.stderr)

    to_count = []
    for login_lower, login in sorted(candidates.items()):
        if login_lower in already:
            continue
        if login_lower in members:
            print(f"  {login}: public member of {args.org}, skipping", file=sys.stderr)
            continue
        to_count.append(login)

    additions = {}
    for login, count in count_merged_pull_requests(args.org, to_count).items():
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
