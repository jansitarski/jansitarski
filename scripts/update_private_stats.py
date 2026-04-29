#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape


API_URL = "https://api.github.com/graphql"
REST_API_URL = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "assets" / "private-github-stats.svg"


STATS_QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER) {
      totalCount
    }
    publicRepositories: repositories(ownerAffiliations: OWNER, privacy: PUBLIC) {
      totalCount
    }
    privateRepositories: repositories(ownerAffiliations: OWNER, privacy: PRIVATE) {
      totalCount
    }
    pullRequests(states: MERGED) {
      totalCount
    }
    repositoriesContributedTo(
      contributionTypes: [COMMIT, PULL_REQUEST, PULL_REQUEST_REVIEW]
      includeUserRepositories: true
      first: 1
    ) {
      totalCount
    }
  }
}
""".strip()


PR_ADDITIONS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    pullRequests(states: MERGED, first: 100, after: $cursor) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        additions
      }
    }
  }
}
""".strip()


def fetch_pr_additions(token: str, username: str) -> int:
    total = 0
    cursor: str | None = None
    page_num = 0
    while True:
        variables: dict[str, object] = {"login": username, "cursor": cursor}
        data = graphql_request(token, PR_ADDITIONS_QUERY, variables)
        user = require_dict(data.get("user"), "data.user")
        prs = require_dict(user.get("pullRequests"), "pullRequests")
        if page_num == 0:
            print(f"Merged PRs to scan: {prs.get('totalCount', '?')}", file=sys.stderr)
        for node in prs.get("nodes") or []:
            if isinstance(node, dict):
                total += node.get("additions", 0)
        page_info = prs.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        page_num += 1
    return total


def graphql_request(
    token: str, query: str, variables: dict[str, object]
) -> dict[str, object]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "jansitarski-private-stats-updater",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub GraphQL request failed with {exc.code}: {error_body}"
        ) from exc

    parsed = json.loads(body)
    if parsed.get("errors"):
        raise RuntimeError(json.dumps(parsed["errors"], indent=2))

    data = parsed.get("data")
    if not data:
        raise RuntimeError(f"No data returned from GitHub GraphQL API: {body}")

    return cast(dict[str, object], data)


def rest_request(token: str, path: str, params: dict[str, str]) -> dict[str, object]:
    query_string = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{REST_API_URL}{path}?{query_string}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.cloak-preview+json",
            "User-Agent": "jansitarski-private-stats-updater",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub REST request failed with {exc.code}: {error_body}"
        ) from exc

    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("Unexpected REST response payload.")
    return cast(dict[str, object], parsed)


def require_dict(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected object at {path}, got {type(value).__name__}")
    return cast(dict[str, Any], value)


def require_int(value: object, path: str) -> int:
    if not isinstance(value, int):
        raise RuntimeError(f"Expected integer at {path}, got {type(value).__name__}")
    return value


def format_number(value: int) -> str:
    return f"{value:,}"


def format_compact(value: int) -> str:
    if value >= 1_000_000:
        s = f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        s = f"{value / 1_000:.1f}k"
    else:
        return str(value)
    # drop trailing ".0" → "50.0k" → "50k"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
        idx = max(s.index("M") if "M" in s else -1, s.index("k") if "k" in s else -1)
        # re-attach suffix if rstrip ate it (shouldn't happen, but be safe)
        _ = idx
    return s


def collect_metrics(username: str, token: str) -> dict[str, int]:
    data = graphql_request(token, STATS_QUERY, {"login": username})
    user = require_dict(data.get("user"), "data.user")

    public_repos = require_int(
        require_dict(user["publicRepositories"], "publicRepositories")["totalCount"],
        "publicRepositories.totalCount",
    )
    private_repos = require_int(
        require_dict(user["privateRepositories"], "privateRepositories")["totalCount"],
        "privateRepositories.totalCount",
    )
    merged_prs = require_int(
        require_dict(user["pullRequests"], "pullRequests")["totalCount"],
        "pullRequests.totalCount",
    )

    contributed_repos = require_int(
        require_dict(user["repositoriesContributedTo"], "repositoriesContributedTo")["totalCount"],
        "repositoriesContributedTo.totalCount",
    )
    print(f"Contributed repos: {contributed_repos}", file=sys.stderr)

    print("Calculating lines added from merged PRs...", file=sys.stderr)
    lines_added = fetch_pr_additions(token, username)
    print(f"Total lines added: {lines_added:,}", file=sys.stderr)

    commits_response = rest_request(
        token,
        "/search/commits",
        {"q": f"author:{username}"},
    )
    total_commits = require_int(
        commits_response.get("total_count"),
        "rest.search.commits.total_count",
    )

    return {
        "public_repos": public_repos,
        "private_repos": private_repos,
        "merged_prs": merged_prs,
        "total_commits": total_commits,
        "contributed_repos": contributed_repos,
        "lines_added": lines_added,
    }


# The new design does not use complex icons.
STAT_LABELS = {
    "public_repos": "Public Repos",
    "private_repos": "Private Repos",
    "merged_prs": "Merged PRs",
    "total_commits": "Total Commits",
}


def render_svg(username: str, metrics: dict[str, int], generated_at: str) -> str:
    escaped_username = escape(username)
    escaped_generated_at = escape(generated_at)

    fmt_public_repos = f'{metrics.get("public_repos", 0):,}'
    fmt_private_repos = f'{metrics.get("private_repos", 0):,}'
    fmt_merged_prs = f'{metrics.get("merged_prs", 0):,}'
    fmt_total_commits = f'{metrics.get("total_commits", 0):,}'
    fmt_contributed_repos = f'{metrics.get("contributed_repos", 0):,}'
    fmt_lines_added = format_compact(metrics.get("lines_added", 0))

    return f'''<svg width="495" height="195" viewBox="0 0 495 195" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title">
    <title id="title">{escaped_username}'s GitHub Stats</title>
    <style>
        .bg {{ fill: #0D1117; }}
        .border {{ stroke: rgba(56, 139, 253, 0.4); }}
        .grid-line {{ stroke: rgba(56, 139, 253, 0.1); }}
        .title, .label {{ fill: #C9D1D9; }}
        .value {{ fill: #F0F6FC; }}
        .accent {{ fill: #58A6FF; }}
        .timestamp {{ fill: #8B949E; }}

        @media (prefers-color-scheme: light) {{
            .bg {{ fill: #F6F8FA; }}
            .border {{ stroke: rgba(31, 35, 40, 0.2); }}
            .grid-line {{ stroke: rgba(31, 35, 40, 0.07); }}
            .title, .label {{ fill: #57606A; }}
            .value {{ fill: #1F2328; }}
            .accent {{ fill: #0969DA; }}
            .timestamp {{ fill: #656D76; }}
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; }}
            to {{ opacity: 1; }}
        }}
        @keyframes slideIn {{
            from {{ transform: translateY(8px); opacity: 0; }}
            to {{ transform: translateY(0); opacity: 1; }}
        }}

        /* Animate the inner group so the CSS transform doesn't override the
           outer SVG transform attribute that holds the grid position. */
        .stats-grid > g > g {{
            animation: slideIn 0.5s ease-out forwards;
            opacity: 0;
        }}
        .stats-grid > g:nth-child(1) > g {{ animation-delay: 0.1s; }}
        .stats-grid > g:nth-child(2) > g {{ animation-delay: 0.15s; }}
        .stats-grid > g:nth-child(3) > g {{ animation-delay: 0.2s; }}
        .stats-grid > g:nth-child(4) > g {{ animation-delay: 0.3s; }}
        .stats-grid > g:nth-child(5) > g {{ animation-delay: 0.35s; }}
        .stats-grid > g:nth-child(6) > g {{ animation-delay: 0.4s; }}
        .title {{ animation: fadeIn 0.5s ease-out; }}

        .title {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
            font-weight: 400;
            font-size: 16px;
        }}
        .label {{
            font-family: "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 11px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}
        .value {{
            font-family: "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-weight: 700;
            font-size: 28px;
            letter-spacing: -1px;
        }}
        .timestamp {{
            font-family: "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 10px;
        }}
    </style>

    <defs>
        <pattern id="grid" width="20" height="20" patternUnits="userSpaceOnUse">
            <path d="M 20 0 L 0 0 0 20" fill="none" class="grid-line" stroke-width="0.5"/>
        </pattern>
        <filter id="noise">
            <feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="3" stitchTiles="stitch"/>
            <feComposite operator="in" in2="SourceGraphic" result="monoNoise"/>
            <feBlend in="SourceGraphic" in2="monoNoise" mode="multiply" opacity="0.05" />
        </filter>
    </defs>

    <rect x="0.5" y="0.5" width="494" height="194" rx="6" class="bg border" stroke-width="1"/>
    <rect width="495" height="195" fill="url(#grid)" filter="url(#noise)" clip-path="url(#clip)"/>
    <clipPath id="clip"><rect x="0.5" y="0.5" width="494" height="194" rx="6"/></clipPath>

    <g transform="translate(25, 25)">
        <text class="title" x="0" y="16">{escaped_username} / GitHub Activity</text>
    </g>

    <!-- 3-column × 2-row grid; columns at x=0, 148, 296 (spacing=148 in 445px work area) -->
    <g class="stats-grid" transform="translate(25, 55)">

        <!-- Row 1, Col 1: Public Repos -->
        <g transform="translate(0, 0)">
            <g>
                <rect class="accent" x="0" y="5" width="3" height="18" rx="1"/>
                <text class="value" x="10" y="28">{fmt_public_repos}</text>
                <text class="label" x="10" y="42">Public Repos</text>
            </g>
        </g>

        <!-- Row 1, Col 2: Private Repos -->
        <g transform="translate(148, 0)">
            <g>
                <rect class="accent" x="0" y="5" width="3" height="18" rx="1"/>
                <text class="value" x="10" y="28">{fmt_private_repos}</text>
                <text class="label" x="10" y="42">Private Repos</text>
            </g>
        </g>

        <!-- Row 1, Col 3: Contributed Repos -->
        <g transform="translate(296, 0)">
            <g>
                <rect class="accent" x="0" y="5" width="3" height="18" rx="1"/>
                <text class="value" x="10" y="28">{fmt_contributed_repos}</text>
                <text class="label" x="10" y="42">Contrib Repos</text>
            </g>
        </g>

        <!-- Row 2, Col 1: Merged PRs -->
        <g transform="translate(0, 60)">
            <g>
                <rect class="accent" x="0" y="5" width="3" height="18" rx="1"/>
                <text class="value" x="10" y="28">{fmt_merged_prs}</text>
                <text class="label" x="10" y="42">Merged PRs</text>
            </g>
        </g>

        <!-- Row 2, Col 2: Total Commits -->
        <g transform="translate(148, 60)">
            <g>
                <rect class="accent" x="0" y="5" width="3" height="18" rx="1"/>
                <text class="value" x="10" y="28">{fmt_total_commits}</text>
                <text class="label" x="10" y="42">Total Commits</text>
            </g>
        </g>

        <!-- Row 2, Col 3: Lines Added -->
        <g transform="translate(296, 60)">
            <g>
                <rect class="accent" x="0" y="5" width="3" height="18" rx="1"/>
                <text class="value" x="10" y="28">{fmt_lines_added}</text>
                <text class="label" x="10" y="42">Lines Added</text>
            </g>
        </g>

    </g>

    <text class="timestamp" x="470" y="183" text-anchor="end">updated: {escaped_generated_at}</text>
</svg>'''

def main() -> int:
    username = os.environ.get("GITHUB_USERNAME", "jansitarski")
    token = os.environ.get("PROFILE_STATS_TOKEN") or os.environ.get("GH_TOKEN")

    if not token:
        print(
            "Missing PROFILE_STATS_TOKEN (or GH_TOKEN). Provide a token with repo and read:user scopes.",
            file=sys.stderr,
        )
        return 1

    metrics = collect_metrics(username, token)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    svg_content = render_svg(username, metrics, generated_at)

    svg_path = ROOT / "gh_stats.svg"
    svg_path.write_text(svg_content, encoding="utf-8")
    print(f"Updated {svg_path}")

    readme_path = ROOT / "README.md"
    with open(readme_path, "r") as f:
        readme_content = f.read()

    start_marker = "<!-- private-stats:start -->"
    end_marker = "<!-- private-stats:end -->"
    
    start_index = readme_content.find(start_marker)
    end_index = readme_content.find(end_marker)

    if start_index != -1 and end_index != -1:
        
        injection_block = f'''{start_marker}
<p align="center">
  <img src="./gh_stats.svg" alt="Jan Sitarski GitHub Stats" />
</p>
{end_marker}'''
        
        new_readme_content = (
            readme_content[:start_index]
            + injection_block
            + readme_content[end_index + len(end_marker):]
        )

        with open(readme_path, "w") as f:
            f.write(new_readme_content)
        print("Successfully updated README.md")
    else:
        print("Could not find markers in README.md")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
