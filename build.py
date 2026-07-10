"""Render my GitHub profile README as a screenshot of my own terminal.

Fetches public repo/commit/LOC counts from the GitHub GraphQL API and writes
dark_mode.svg and light_mode.svg. Run daily from .github/workflows/build.yaml.

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
TITLEBAR_H = 36
FIRST_BASELINE = TITLEBAR_H + 24
INFO_COL = 24  # neofetch offsets the info block 24 columns from the art
UNDERLINE = 64  # what my terminal actually prints

ASCII_ART = [
    r"  __ _  __ _  __ _   ",
    r" / _` |/ _` |/ _` |  ",
    r"| (_| | (_| | (_| |_ ",
    r" \__,_|\__,_|\__,_(_)",
]

PROMPT = [("aaangel", "cyan"), ("@", "fg"), ("laaabs", "cyan"),
          (" aaangelmartin ", "fg"), ("%", "cyan")]

THEMES = {
    "dark_mode.svg": {
        "bg": "#171717", "titlebar": "#232323", "border": "#2e2e2e",
        "fg": "#ffffff", "cyan": "#00b5e2", "dim": "#6e6e6e",
    },
    "light_mode.svg": {
        "bg": "#ffffff", "titlebar": "#ececec", "border": "#d5d5d5",
        "fg": "#1c1c1c", "cyan": "#0f7fa0", "dim": "#8a8a8a",
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


def fetch(token):
    """Commit/LOC totals for every non-fork repo I am affiliated with.

    cache/stats.json keys each repo by a hash of its name and stores the
    default-branch HEAD oid, so a repo whose HEAD has not moved is never
    walked again.
    """
    user_id = query(token, "query($login:String!){user(login:$login){id}}",
                    login=LOGIN)["user"]["id"]
    owned = query(token, OWNED_Q, login=LOGIN)["user"]["repositories"]["totalCount"]

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
        "commits": sum(r["commits"] for r in fresh.values()),
        "adds": sum(r["adds"] for r in fresh.values()),
        "dels": sum(r["dels"] for r in fresh.values()),
    }


def info_lines(stats, today):
    n = lambda v: f"{v:,}"
    loc = stats["adds"] - stats["dels"]
    return [
        [("aaangel", "cyan"), ("@", "fg"), ("laaabs", "cyan")],
        [("-" * UNDERLINE, "fg")],
        *[[(k, "cyan"), (":", "cyan"), (f" {v}", "fg")] for k, v in [
            ("OS", "macOS 26.0.1 arm64"),
            ("Host", "laaabs."),
            ("Kernel", "computer science @ uc3m"),
            ("Uptime", uptime(today)),
            ("Shell", "zsh 5.9"),
            ("Terminal", "Apple_Terminal"),
            ("Terminal Font", "SF Mono Nerd Font"),
            ("CPU", "Apple M4 Pro"),
            ("GPU", "Apple M4 Pro"),
        ]],
        [],
        *[[(k, "cyan"), (":", "cyan"), (f" {v}", "fg")] for k, v in [
            ("Languages.Programming", "TypeScript, Python, Go, JavaScript"),
            ("Languages.Real", "Spanish, English"),
        ]],
        [],
        *[[(k, "cyan"), (":", "cyan"), (f" {v}", "fg")] for k, v in [
            ("Location", "Madrid, Spain"),
            ("Web", "aaangelmartin.com"),
            ("Twitter", "@aaangelmartin_"),
            ("Email", "hello@aaangelmartin.com"),
        ]],
        [],
        [("Repos", "cyan"), (":", "cyan"), (f" {n(stats['repos'])}", "fg")],
        [("Commits", "cyan"), (":", "cyan"), (f" {n(stats['commits'])}", "fg")],
        [("Lines of Code", "cyan"), (":", "cyan"), (f" {n(loc)}", "fg"),
         (f"  ({n(stats['adds'])}++, {n(stats['dels'])}--)", "dim")],
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

    rows = [spans(PROMPT + [(" neofetch", "fg")], 0, 0)]
    rows += [spans([(line, "cyan")], 0, 2 + i) for i, line in enumerate(ASCII_ART)]
    rows += [spans(runs, INFO_COL, 2 + i) for i, runs in enumerate(info) if runs]

    last = 2 + len(info) + 1
    rows.append(spans(PROMPT, 0, last))
    cursor_x = PAD_X + (sum(len(t) for t, _ in PROMPT) + 1) * CHAR_W
    cursor_y = FIRST_BASELINE + last * LINE_H - FONT_SIZE + 2

    width = round(PAD_X * 2 + (INFO_COL + UNDERLINE) * CHAR_W)
    height = FIRST_BASELINE + last * LINE_H + LINE_H

    lights = "".join(
        f'<circle cx="{22 + i * 20}" cy="{TITLEBAR_H / 2}" r="6" fill="{c}"/>'
        for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")))

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
.dim {{ fill: {theme['dim']}; }}
</style>
<rect width="{width}" height="{height}" rx="10" fill="{theme['bg']}"
      stroke="{theme['border']}"/>
<path d="M0 {TITLEBAR_H}h{width}M10 0h{width - 20}a10 10 0 0 1 10 10v{TITLEBAR_H - 10}H0V10a10 10 0 0 1 10-10z"
      fill="{theme['titlebar']}" stroke="{theme['border']}"/>
{lights}
<text x="{width / 2}" y="{TITLEBAR_H / 2 + 4}" text-anchor="middle" font-size="12px"
      class="dim">aaangel@laaabs — aaangelmartin — zsh</text>
<rect x="{cursor_x:.2f}" y="{cursor_y}" width="{CHAR_W:.2f}" height="{FONT_SIZE}"
      fill="{theme['cyan']}"/>
<text>
{chr(10).join(rows)}
</text>
</svg>
'''


def main():
    # A PAT with repo + read:org: the Action's built-in GITHUB_TOKEN sees
    # neither private repos nor org repos, which is where most of the code is.
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("set ACCESS_TOKEN to a PAT with repo + read:org scopes")
    today = date.today()
    stats = fetch(token)
    print(f"repos={stats['repos']} commits={stats['commits']} "
          f"loc={stats['adds'] - stats['dels']:,}", file=sys.stderr)
    for filename, theme in THEMES.items():
        (ROOT / filename).write_text(render(theme, stats, today))
        print(f"wrote {filename}", file=sys.stderr)


if __name__ == "__main__":
    main()
