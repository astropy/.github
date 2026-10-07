"""
Add contributors who have had enough pull requests merged in the organization
to the vetting allowlist, and print a Markdown summary of the additions (for
use as a pull request description).

Only users who authored a pull request merged since the "last-updated" time in
the allowlist header are considered, so that the check stays cheap. A user is
added if they have had at least MIN_MERGED pull requests merged across the
organization, are not already listed (with or without a leading "-", which
explicitly excludes them), and are not a public member of the organization.
When users are added, the "last-updated" time is set to now.

Uses the GitHub GraphQL API, which allows the merged pull request counts of
many users to be fetched in one request, with the token from the GITHUB_TOKEN
environment variable, and only the standard library.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta

API = "https://api.github.com"
LAST_UPDATED = re.compile(r"^# last-updated: (\d{4}-\d{2}-\d{2})")

ORG = "astropy"
MIN_MERGED = 2  # merged pull requests in the organization needed to be added to the allowlist

# Printed to stdout, to be used as the description of the pull request adding
# the contributors to the allowlist, with one PR_BODY_ENTRY line per addition.
PR_BODY = """\
The following contributors have had at least {min_merged} pull requests merged in the {org} organization \
and are not organization members, so this adds them to the vetting allowlist:

{entries}

If you believe any of them should not be added to the allowlist, for example if
they have violated the AI policy, keep them in the allowlist but prefix their
username with a -
"""
PR_BODY_ENTRY = "* @{user} ({count} merged pull requests, https://github.com/pulls?q=org%3A{org}+type%3Apr+is%3Amerged+author%3A{user})"
PR_BODY_NOTHING = "No contributors to add."
SEARCH_LIMIT = 1000  # results per search query, imposed by GitHub
COUNT_BATCH_SIZE = 50  # users per GraphQL request when counting merged pull requests


def github_request(url, data=None):
    """
    GET a URL, or POST JSON data to it, and return the decoded JSON response
    and the headers, waiting and retrying when rate limited.
    """
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json"}
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=120) as response:
                return json.load(response), response.headers
        except urllib.error.HTTPError as error:
            if error.code not in (403, 429):
                raise
            wait = max(int(error.headers.get("X-RateLimit-Reset", 0)) - time.time(), 0) + 5
            print(f"Rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)


def graphql(query):
    result, _ = github_request(f"{API}/graphql", json.dumps({"query": query}).encode())
    if result.get("errors"):
        raise RuntimeError(f"GraphQL query failed: {result['errors']}")
    return result["data"]


def public_members():
    members = set()
    for page in range(1, 100):
        batch, _ = github_request(f"{API}/orgs/{ORG}/public_members?per_page=100&page={page}")
        members |= {member["login"].lower() for member in batch}
        if len(batch) < 100:
            return members


def merged_pull_request_authors(start, end):
    """
    The authors of the pull requests merged in the organization between two
    dates (inclusive), as a dict mapping lower-case login to login, skipping
    bots and deleted users.

    A search returns at most SEARCH_LIMIT results, so the date range is split
    in two until each part fits.
    """
    search = f'search(type: ISSUE, first: 100, after: %s, query: "org:{ORG} type:pr is:merged merged:{start}..{end}")'
    page = graphql(f"{{ {search % 'null'} {{ issueCount pageInfo {{ hasNextPage endCursor }} nodes {{ ... on PullRequest {{ author {{ __typename login }} }} }} }} }}")["search"]

    if page["issueCount"] > SEARCH_LIMIT and start < end:
        middle = start + (end - start) // 2
        return merged_pull_request_authors(start, middle) | merged_pull_request_authors(middle + timedelta(days=1), end)

    authors = {}
    while True:
        for node in page["nodes"]:
            author = node["author"]
            if author and author["__typename"] != "Bot":
                authors.setdefault(author["login"].lower(), author["login"])
        if not page["pageInfo"]["hasNextPage"]:
            return authors
        page = graphql(f"{{ {search % json.dumps(page['pageInfo']['endCursor'])} {{ pageInfo {{ hasNextPage endCursor }} nodes {{ ... on PullRequest {{ author {{ __typename login }} }} }} }} }}")["search"]


def merged_pull_request_counts(users):
    """
    How many pull requests each user has had merged in the organization, as
    a dict, fetched in batches of aliased searches.
    """
    counts = {}
    for start in range(0, len(users), COUNT_BATCH_SIZE):
        batch = users[start : start + COUNT_BATCH_SIZE]
        fields = " ".join(
            f'u{i}: search(type: ISSUE, first: 1, query: "org:{ORG} type:pr is:merged author:{user}") {{ issueCount }}'
            for i, user in enumerate(batch)
        )
        data = graphql(f"{{ {fields} }}")
        counts.update({user: data[f"u{i}"]["issueCount"] for i, user in enumerate(batch)})
    return counts


def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} ALLOWLIST_FILE")
    allowlist = sys.argv[1]

    with open(allowlist) as f:
        lines = f.read().splitlines()
    header = [line for line in lines if line.startswith("#")]
    names = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    listed = {name.lstrip("-").lower() for name in names}

    last_updated = next((LAST_UPDATED.match(line) for line in header if LAST_UPDATED.match(line)), None)
    if last_updated is None:
        sys.exit(f"No '# last-updated: YYYY-MM-DD' line found in {allowlist}")
    # Start a day earlier than the last update, in case it ran part way through a day
    since = date.fromisoformat(last_updated.group(1)) - timedelta(days=1)

    print(f"Looking for pull requests merged in {ORG} since {since}", file=sys.stderr)
    candidates = merged_pull_request_authors(since, datetime.now(UTC).date())
    members = public_members()
    to_count = [login for key, login in sorted(candidates.items()) if key not in listed and key not in members]
    print(f"{len(candidates)} contributors, {len(to_count)} not already listed or public members", file=sys.stderr)

    counts = merged_pull_request_counts(to_count)
    additions = sorted((user for user, count in counts.items() if count >= MIN_MERGED), key=str.lower)
    for user in to_count:
        print(f"  {user}: {counts[user]} merged pull requests{', adding' if user in additions else ''}", file=sys.stderr)

    if not additions:
        print(PR_BODY_NOTHING)
        return
    entries = "\n".join(PR_BODY_ENTRY.format(user=user, count=counts[user], org=ORG) for user in additions)
    print(PR_BODY.format(min_merged=MIN_MERGED, org=ORG, entries=entries), end="")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    header = [f"# last-updated: {now}" if LAST_UPDATED.match(line) else line for line in header]
    with open(allowlist, "w") as f:
        f.write("\n".join(header + sorted(names + additions, key=lambda name: name.lstrip("-").lower())) + "\n")


if __name__ == "__main__":
    main()
