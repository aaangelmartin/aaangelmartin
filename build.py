"""Render my GitHub profile README as a screenshot of my own terminal.

Pulls repo, contribution and lines-of-code figures (public and private) from
the GitHub API and writes dark_mode.svg and light_mode.svg. Run daily from
.github/workflows/build.yaml.

Colours, prompt, ASCII art and neofetch field order mirror my real setup:
~/.config/neofetch/config.conf and the "aaa" Terminal.app profile.
"""

import base64
import hashlib
import json
import os
import pathlib
import sys
from datetime import date
from xml.sax.saxutils import escape

import requests

ROOT = pathlib.Path(__file__).parent
CACHE = ROOT / "cache" / "stats.json"
FONT = ROOT / "fonts" / "DejaVuSansMono-subset.woff2"

LOGIN = "aaangelmartin"
BIRTHDAY = date(2007, 6, 1)
API = "https://api.github.com/graphql"

# SF Mono advances 1266/2048 em; DejaVu Sans Mono 1233/2048. Scaling DejaVu by
# 1266/1233 makes both fonts occupy identical horizontal space, so the layout is
# byte-identical whether or not the viewer has SF Mono. Apple's licence forbids
# embedding SF Mono itself, hence the local() reference plus a free fallback.
SF_ADVANCE = 1266 / 2048
DEJAVU_ADVANCE = 1233 / 2048
SIZE_ADJUST = SF_ADVANCE / DEJAVU_ADVANCE

FONT_SIZE = 16
CHAR_W = FONT_SIZE * SF_ADVANCE
LINE_H = 20
PAD_X = 24
PAD_Y = 26
FIRST_BASELINE = PAD_Y + FONT_SIZE
INFO_COL = 24    # neofetch offsets the info block 24 columns from the art
TERM_COLS = 80   # a standard terminal is exactly 80 columns wide

ASCII_ART = [
    r"  __ _  __ _  __ _   ",
    r" / _` |/ _` |/ _` |  ",
    r"| (_| | (_| | (_| |_ ",
    r" \__,_|\__,_|\__,_(_)",
]

THEMES = {
    "dark_mode.svg": {
        "bg": "#0a0a0a", "border": "#1f1f1f",
        "fg": "#ffffff", "cyan": "#00b5e2",
        "add": "#3fb950", "del": "#f85149",
    },
    "light_mode.svg": {
        "bg": "#ffffff", "border": "#e2e2e2",
        "fg": "#1c1c1c", "cyan": "#0088ad",
        "add": "#1a7f37", "del": "#cf222e",
    },
}


def uptime(today):
    """Age as neofetch would print an uptime."""
    years = today.year - BIRTHDAY.year
    months = today.month - BIRTHDAY.month
    days = today.day - BIRTHDAY.day
    if days < 0:
        months -= 1
        prev = (today.replace(day=1) - date.resolution)
        days += prev.day
    if months < 0:
        years -= 1
        months += 12
    parts = [(years, "year"), (months, "month"), (days, "day")]
    return ", ".join(f"{n} {u}{'s' if n != 1 else ''}" for n, u in parts)


def query(token, gql, **variables):
    r = requests.post(API, json={"query": gql, "variables": variables},
                      headers={"Authorization": f"bearer {token}"}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(body["errors"])
    return body["data"]


# Every repo I can see and might have committed to: mine (public and private),
# ones I collaborate on, and ones belonging to orgs I am a member of.
AFFILIATIONS = "[OWNER, COLLABORATOR, ORGANIZATION_MEMBER]"

REPOS_Q = """
query($login:String!, $after:String){
  user(login:$login){
    repositories(first:100, after:$after, ownerAffiliations:%s, isFork:false){
      pageInfo{ hasNextPage endCursor }
      nodes{
        name isPrivate owner{ login }
        defaultBranchRef{ target{ ... on Commit{ oid } } }
      }
    }
  }
}""" % AFFILIATIONS

OWNED_Q = """
query($login:String!){
  user(login:$login){
    repositories(ownerAffiliations:OWNER, isFork:false){ totalCount }
  }
}"""

CONTRIB_Q = """
query($login:String!){
  user(login:$login){
    repositoriesContributedTo(includeUserRepositories:true,
      contributionTypes:[COMMIT, PULL_REQUEST, ISSUE, PULL_REQUEST_REVIEW]){
      totalCount
    }
  }
}"""

# The contribution calendar total matches what my GitHub profile shows and
# includes private contributions, but never their type. So I take that total,
# count PRs / issues / reviews (public and private) through search, and back out
# commits as the remainder — the only way to reach a private commit count, and
# it keeps the breakdown summing to the number on my profile.
CALENDAR_Q = ("query($login:String!){ user(login:$login){"
              " contributionsCollection{ contributionCalendar{ totalContributions } } } }")
SEARCH_Q = "query($q:String!){ search(query:$q, type:ISSUE){ issueCount } }"

HISTORY_Q = """
query($owner:String!, $name:String!, $id:ID!, $after:String){
  repository(owner:$owner, name:$name){
    defaultBranchRef{ target{ ... on Commit{
      history(first:100, after:$after, author:{id:$id}){
        totalCount
        pageInfo{ hasNextPage endCursor }
        nodes{ additions deletions }
      }}}}
}}"""


def cache_key(owner, name):
    """Private repo names must never reach this public repo's cache or CI logs."""
    return hashlib.sha256(f"{owner}/{name}".encode()).hexdigest()[:16]


def _search_count(token, q):
    return query(token, SEARCH_Q, q=q)["search"]["issueCount"]


def contributions_breakdown(token):
    """Contribution total (matches my profile) split by type, public + private."""
    total = query(token, CALENDAR_Q, login=LOGIN)[
        "user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
    prs = _search_count(token, f"author:{LOGIN} is:pr")
    issues = _search_count(token, f"author:{LOGIN} is:issue")
    reviews = _search_count(token, f"reviewed-by:{LOGIN} is:pr")
    commits = max(0, total - prs - issues - reviews)
    return {"total": total, "commits": commits, "prs": prs, "issues": issues,
            "reviews": reviews}


def fetch(token):
    """Commit/LOC totals for every non-fork repo I am affiliated with.

    cache/stats.json keys each repo by a hash of its name and stores the
    default-branch HEAD oid, so a repo whose HEAD has not moved is never
    walked again.
    """
    user_id = query(token, "query($login:String!){user(login:$login){id}}",
                    login=LOGIN)["user"]["id"]
    owned = query(token, OWNED_Q, login=LOGIN)["user"]["repositories"]["totalCount"]
    contributed = query(token, CONTRIB_Q, login=LOGIN)[
        "user"]["repositoriesContributedTo"]["totalCount"]
    contributions = contributions_breakdown(token)

    repos, after = [], None
    while True:
        page = query(token, REPOS_Q, login=LOGIN, after=after)["user"]["repositories"]
        repos += [n for n in page["nodes"] if n["defaultBranchRef"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    fresh = {}
    for repo in repos:
        name, owner = repo["name"], repo["owner"]["login"]
        oid = repo["defaultBranchRef"]["target"]["oid"]
        key = cache_key(owner, name)
        if cache.get(key, {}).get("oid") == oid:
            fresh[key] = cache[key]
            continue

        commits = adds = dels = 0
        after = None
        while True:
            branch = query(token, HISTORY_Q, owner=owner, name=name,
                           id=user_id, after=after)["repository"]["defaultBranchRef"]
            history = branch["target"]["history"]
            commits = history["totalCount"]
            for node in history["nodes"]:
                adds += node["additions"]
                dels += node["deletions"]
            if not history["pageInfo"]["hasNextPage"]:
                break
            after = history["pageInfo"]["endCursor"]

        fresh[key] = {"oid": oid, "commits": commits, "adds": adds, "dels": dels}
        label = f"{owner}/{name}" if not repo["isPrivate"] else f"<private {key}>"
        print(f"  walked {label}: {commits} commits, +{adds} -{dels}", file=sys.stderr)

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")

    return {
        "repos": owned,
        "contributed": contributed,
        "contributions": contributions,
        "adds": sum(r["adds"] for r in fresh.values()),
        "dels": sum(r["dels"] for r in fresh.values()),
    }


def _kv(key, value):
    """`key: value`, left-packed, exactly as my neofetch prints it."""
    return [(key, "cyan"), (":", "cyan"), (f" {value}", "fg")]


def _loc(added, deleted):
    net = added - deleted
    return [("Lines of Code", "cyan"), (":", "cyan"), (f" {net:,} (", "fg"),
            (f"{added:,}++", "add"), (", ", "fg"), (f"{deleted:,}--", "del"),
            (")", "fg")]


def _title(name):
    """Section label — a plain heading, no rule."""
    return [(name, "cyan")]


def _repos(owned, contributed):
    return [("Repos", "cyan"), (":", "cyan"), (f" {owned:,} ", "fg"),
            ("(", "fg"), ("Contributed", "cyan"), (":", "cyan"),
            (f" {contributed:,}", "fg"), (")", "fg")]


def _indent(key, value):
    """A key/value nested one level under its parent line."""
    return [("  " + key, "cyan"), (":", "cyan"), (f" {value:,}", "fg")]


def info_lines(stats, today):
    c = stats["contributions"]
    body = [
        _kv("OS", "aaaOS arm64"),
        _kv("Host", "aaangelmartin.com"),
        _kv("Uptime", uptime(today)),
        _kv("Shell", "zsh aaa"),
        _kv("Terminal", "terminaaal"),
        _kv("Terminal Font", "aaa Mono Nerd Font"),
        _kv("CPU", "aaa Pro"),
        _kv("GPU", "aaa Pro"),
        [],
        _title("GitHub Stats"),
        _repos(stats["repos"], stats["contributed"]),
        _kv("Contributions", f"{c['total']:,}"),
        _indent("Commits", c["commits"]),
        _indent("PRs", c["prs"]),
        _indent("Issues", c["issues"]),
        _indent("Reviews", c["reviews"]),
        _loc(stats["adds"], stats["dels"]),
    ]
    # U+2500 joins across cells into one unbroken rule (a row of "-" leaves gaps),
    # spanning the info column to the right edge of the 80-col terminal.
    return [
        [("angel", "cyan"), ("@", "fg"), ("aaangelmartin.com", "cyan")],
        [("─" * (TERM_COLS - INFO_COL), "fg")],
        *body,
    ]


def spans(runs, col, row):
    """One <tspan> per coloured run, anchored at a character grid cell."""
    x = PAD_X + col * CHAR_W
    y = FIRST_BASELINE + row * LINE_H
    out = [f'<tspan x="{x:.2f}" y="{y}" class="{runs[0][1]}">{escape(runs[0][0])}</tspan>']
    out += [f'<tspan class="{cls}">{escape(text)}</tspan>' for text, cls in runs[1:]]
    return "".join(out)


def render(theme, stats, today):
    font = base64.b64encode(FONT.read_bytes()).decode()
    info = info_lines(stats, today)

    art_cols = max(len(line) for line in ASCII_ART)
    # Centre the art in its column: vertically against the info block, and
    # horizontally in the gutter to the left of the info column. Kept fractional
    # so the art lands exactly on the block's midpoint, not a rounded row.
    art_off = max(0, (len(info) - len(ASCII_ART)) / 2)
    art_x = (INFO_COL - art_cols) / 2
    rows = [spans([(line, "cyan")], art_x, art_off + i)
            for i, line in enumerate(ASCII_ART)]
    rows += [spans(runs, INFO_COL, i) for i, runs in enumerate(info) if runs]

    n_rows = max(len(info), art_off + len(ASCII_ART))
    width = round(PAD_X * 2 + TERM_COLS * CHAR_W)
    height = round(PAD_Y * 2 + (n_rows - 1) * LINE_H + FONT_SIZE)

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}px" height="{height}px"
     viewBox="0 0 {width} {height}" font-size="{FONT_SIZE}px">
<style>
@font-face {{
  font-family: 'sfmono';
  src: local('SF Mono'), local('SFMono-Regular'), local('SFMonoNerdFont-Regular'),
       local('SFMono Nerd Font');
}}
@font-face {{
  font-family: 'dejavu';
  src: url(data:font/woff2;base64,{font}) format('woff2');
  size-adjust: {SIZE_ADJUST:.2%};
}}
text {{ font-family: 'sfmono', 'dejavu', monospace; }}
text, tspan {{ white-space: pre; }}
.fg {{ fill: {theme['fg']}; }}
.cyan {{ fill: {theme['cyan']}; }}
.add {{ fill: {theme['add']}; }}
.del {{ fill: {theme['del']}; }}
</style>
<rect width="{width}" height="{height}" rx="14" fill="{theme['bg']}"
      stroke="{theme['border']}"/>
<text>
{chr(10).join(rows)}
</text>
</svg>
'''


def main():
    # A personal access token that can read my private repos and org membership;
    # the Action's built-in GITHUB_TOKEN sees neither, which is where most of the
    # code and contributions live.
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("set ACCESS_TOKEN to a token that can read private repos")
    today = date.today()
    stats = fetch(token)
    print(f"repos={stats['repos']} contributions={stats['contributions']['total']} "
          f"loc={stats['adds'] - stats['dels']:,}", file=sys.stderr)
    for filename, theme in THEMES.items():
        (ROOT / filename).write_text(render(theme, stats, today))
        print(f"wrote {filename}", file=sys.stderr)


if __name__ == "__main__":
    main()
