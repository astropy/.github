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
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta

API = "https://api.github.com"
LAST_UPDATED = re.compile(r"^# last-updated: (\d{4}-\d{2}-\d{2})")

ORG = "astropy"
ALLOWLIST = "contributor-allowlist.txt"
MIN_MERGED = 2  # merged pull requests in the organization needed to be added to the allowlist

# Printed to stdout, to be used as the description of the pull request adding
# the contributors to the allowlist, with one PR_BODY_ENTRY line per addition.
PR_BODY = """\
The following contributors have had at least {min_merged} pull requests merged in the {org} organization and are not organization members, so this adds them to the vetting allowlist:

{entries}

If you believe any of them should not be added to the allowlist, for example if they have violated the AI policy, keep them in the allowlist but prefix their username with a -
"""
PR_BODY_ENTRY = "* @{user} ({count} merged pull requests, [all pull requests to the {org} org](https://github.com/pulls?q=org%3A{org}+type%3Apr+author%3A{user_quoted}))"
PR_BODY_NOTHING = "No contributors to add."
SEARCH_LIMIT = 1000  # results per search query, imposed by GitHub
COUNT_BATCH_SIZE = 50  # users per GraphQL request when counting merged pull requests


AUTHORS_QUERY = """
{{
  search(type: ISSUE, first: 100, after: {after}, query: "org:{org} type:pr is:merged merged:{start}..{end}") {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{ author {{ __typename login }} }} }}
  }}
}}
"""
COUNT_QUERY = 'u{i}: search(type: ISSUE, first: 1, query: "org:{org} type:pr is:merged author:{user}") {{ issueCount }}'


def github_request(url, data=None):
    """
    GET a URL, or POST JSON data to it, and return the decoded JSON response.
    """
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=120) as response:
        return json.load(response)


def graphql(query):
    result = github_request(f"{API}/graphql", json.dumps({"query": query}).encode())
    if result.get("errors"):
        raise RuntimeError(f"GraphQL query failed: {result['errors']}")
    return result["data"]


def public_members():
    members = set()
    for page in range(1, 100):
        batch = github_request(f"{API}/orgs/{ORG}/public_members?per_page=100&page={page}")
        members |= {member["login"].lower() for member in batch}
        if len(batch) < 100:
            return members


def merged_pull_request_authors(start, end):
    """
    The authors of the pull requests merged in the organization between two
    dates (inclusive), as a dict mapping lower-case login to login, skipping
    deleted users. Bots are included, with the "[bot]" suffix that the REST
    API and webhooks use for their login.

    A search returns at most SEARCH_LIMIT results, so the date range is split
    in two until each part fits.
    """
    authors = {}
    after = "null"
    while True:
        page = graphql(AUTHORS_QUERY.format(after=after, org=ORG, start=start, end=end))["search"]
        if page["issueCount"] > SEARCH_LIMIT and start < end:
            middle = start + (end - start) // 2
            return merged_pull_request_authors(start, middle) | merged_pull_request_authors(middle + timedelta(days=1), end)
        for node in page["nodes"]:
            author = node["author"]
            if author is None:
                continue
            login = author["login"]
            if author["__typename"] == "Bot" and not login.endswith("[bot]"):
                login += "[bot]"
            authors.setdefault(login.lower(), login)
        if not page["pageInfo"]["hasNextPage"]:
            return authors
        after = json.dumps(page["pageInfo"]["endCursor"])


def merged_pull_request_counts(users):
    """
    How many pull requests each user has had merged in the organization, as
    a dict, fetched in batches of aliased searches.
    """
    counts = {}
    for start in range(0, len(users), COUNT_BATCH_SIZE):
        batch = users[start : start + COUNT_BATCH_SIZE]
        data = graphql("{ " + " ".join(COUNT_QUERY.format(i=i, org=ORG, user=user) for i, user in enumerate(batch)) + " }")
        counts.update({user: data[f"u{i}"]["issueCount"] for i, user in enumerate(batch)})
    return counts


def main():
    with open(ALLOWLIST) as f:
        lines = f.read().splitlines()
    header = [line for line in lines if line.startswith("#")]
    names = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    listed = {name.lstrip("-").lower() for name in names}

    last_updated = next((LAST_UPDATED.match(line) for line in header if LAST_UPDATED.match(line)), None)
    if last_updated is None:
        sys.exit(f"No '# last-updated: YYYY-MM-DD' line found in {ALLOWLIST}")
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
    entries = "\n".join(
        PR_BODY_ENTRY.format(user=user, user_quoted=urllib.parse.quote(user), count=counts[user], org=ORG) for user in additions
    )
    print(PR_BODY.format(min_merged=MIN_MERGED, org=ORG, entries=entries), end="")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    header = [f"# last-updated: {now}" if LAST_UPDATED.match(line) else line for line in header]
    with open(ALLOWLIST, "w") as f:
        f.write("\n".join(header + sorted(names + additions, key=lambda name: name.lstrip("-").lower())) + "\n")


if __name__ == "__main__":
    main()
