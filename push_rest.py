#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lievyljucy
#
# 本文件为本仓库原创实现，未包含任何第三方源码片段；运行时仅使用 Python 标准库。
"""
Push the local git history to GitHub when git smart-HTTPS transport is blocked.

Why this exists: on some networks a global `url.<base>.insteadOf` rule routes
github.com through a proxy that refuses to forward Basic auth, while the direct
route is reset. api.github.com still works, so this replays the history through
the REST Git Data API instead: blobs -> trees -> commits -> force-update a ref.

Guarantees:
  * Reads objects via `git cat-file` / `git ls-tree -z` (raw bytes, no re-encoding).
  * Incremental: anchors on the remote head's tree and appends only new commits.
  * Verifies at the end that the remote tree hash equals the local tree hash, and
    fails loudly otherwise. Commit SHAs cannot match (GitHub normalises commit
    timestamps to UTC), so tree equality is the meaningful check.
  * The token is read from the git credential manager at runtime and never
    printed, never written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"FAIL  {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def ok(msg: str) -> None:
    log(f"  OK  {msg}")


def info(msg: str) -> None:
    log(f"  ..  {msg}")


# --------------------------------------------------------------------------- git
def git(*args: str, binary: bool = False, cwd: str | None = None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if p.returncode != 0:
        die(f"git {' '.join(args)} -> {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def token_from_credential_manager(cwd: str) -> str:
    p = subprocess.run(["git", "credential", "fill"], cwd=cwd, capture_output=True,
                       input=b"protocol=https\nhost=github.com\n\n")
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("password="):
            tok = line[len("password="):].strip()
            if len(tok) >= 20:
                return tok
    die("no github.com token available from the git credential manager")


# -------------------------------------------------------------------------- http
class NotFound(Exception):
    """Raised when the API answers 404 (e.g. the branch does not exist yet)."""


class Client:
    def __init__(self, token: str, base: str):
        self.token = token
        self.base = base

    def call(self, method: str, path: str, body=None, retries: int = 4):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "dsh-rest-push",
            "Accept": "application/vnd.github+json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read()[:300]!r}"
                if e.code == 404:
                    raise NotFound(path) from None
                if e.code in (409, 429) or e.code >= 500:
                    info(f"retry {attempt}/{retries} after {last}")
                    time.sleep(2 * attempt)
                    continue
                die(f"{method} {path} -> {last}")
            except Exception as e:  # timeouts, resets
                last = f"{type(e).__name__}: {e}"
                info(f"retry {attempt}/{retries} after {last}")
                time.sleep(2 * attempt)
        die(f"{method} {path} failed after {retries} attempts: {last}")


# ------------------------------------------------------------------------ replay
class Replayer:
    def __init__(self, client: Client, root: str):
        self.c = client
        self.root = root
        self.map: dict[str, str] = {}   # local object sha -> remote object sha

    def blob(self, sha: str) -> str:
        if sha in self.map:
            return self.map[sha]
        data = git("cat-file", "blob", sha, binary=True, cwd=self.root)
        import base64
        r = self.c.call("POST", "/git/blobs",
                        {"content": base64.b64encode(data).decode("ascii"),
                         "encoding": "base64"})
        self.map[sha] = r["sha"]
        info(f"  blob {sha[:7]} -> {r['sha'][:7]} ({len(data)} B)")
        return r["sha"]

    def tree(self, sha: str) -> str:
        if sha in self.map:
            return self.map[sha]
        raw = git("ls-tree", "-z", sha, binary=True, cwd=self.root)
        entries = []
        for rec in raw.split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            mode, typ, child = meta.split(b" ")
            mode_s = mode.decode()
            if len(mode_s) == 5:          # API wants 6-digit modes for trees
                mode_s = "0" + mode_s
            typ_s = typ.decode()
            if typ_s == "tree":
                child_remote = self.tree(child.decode())
            elif typ_s == "blob":
                child_remote = self.blob(child.decode())
            else:
                die(f"unsupported tree entry type {typ_s}")
            entries.append({"path": path.decode("utf-8", "surrogateescape"),
                            "mode": mode_s, "type": typ_s, "sha": child_remote})
        r = self.c.call("POST", "/git/trees", {"tree": entries})
        self.map[sha] = r["sha"]
        return r["sha"]


def parse_ident(line: str, kind: str) -> dict:
    # "author Name <email> 1794805347 +0800"
    rest = line[len(kind) + 1:]
    lt = rest.rfind("<")
    gt = rest.rfind(">")
    if lt < 0 or gt < 0:
        die(f"cannot parse {kind}: {line}")
    name = rest[:lt].strip()
    email = rest[lt + 1:gt]
    ts, off = rest[gt + 1:].strip().split()
    epoch = int(ts)
    sign = -1 if off[0] == "-" else 1
    minutes = sign * (int(off[1:3]) * 60 + int(off[3:5]))
    # Preserve the original offset: normalising to UTC would change the object.
    import datetime as dt
    tz = dt.timezone(dt.timedelta(minutes=minutes))
    local = dt.datetime.fromtimestamp(epoch, tz=tz)
    return {"name": name, "email": email,
            "date": local.strftime("%Y-%m-%dT%H:%M:%S") + off[:3] + ":" + off[3:5]}


def split_commit(text: str):
    head, _, message = text.partition("\n\n")
    fields = {}
    for line in head.splitlines():
        if line.startswith("tree "):
            fields["tree"] = line[5:]
        elif line.startswith("author "):
            fields["author"] = line
        elif line.startswith("committer "):
            fields["committer"] = line
    return fields, message


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay git history via the GitHub REST API")
    ap.add_argument("--project-dir", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--full", action="store_true",
                    help="always replay from the root commit instead of appending")
    args = ap.parse_args()

    root = os.path.abspath(args.project_dir)
    base = f"/repos/{args.owner}/{args.repo}"
    client = Client(token_from_credential_manager(root), API + base)
    ok(f"token loaded; target={args.owner}/{args.repo}")

    commits = [c for c in git("rev-list", "--reverse", args.branch, cwd=root).split() if c]
    if not commits:
        die(f"no commits on {args.branch}")
    local_head = git("rev-parse", args.branch, cwd=root).strip()
    local_tree = git("rev-parse", f"{args.branch}^{{tree}}", cwd=root).strip()
    ok(f"{len(commits)} local commit(s); head={local_head[:7]} tree={local_tree[:7]}")

    # remote current state
    remote_head = None
    remote_tree = None
    try:
        ref = client.call("GET", f"/git/refs/heads/{args.branch}")
        remote_head = ref["object"]["sha"]
        remote_tree = client.call("GET", f"/git/commits/{remote_head}")["tree"]["sha"]
        info(f"remote at {remote_head[:7]}, tree {remote_tree[:7]}")
    except NotFound:
        info("remote branch does not exist yet")

    # incremental anchor: newest local commit whose tree equals the remote tree
    parents: list[str] = []
    start = 0
    if remote_head and not args.full:
        for i in range(len(commits) - 1, -1, -1):
            if git("rev-parse", f"{commits[i]}^{{tree}}", cwd=root).strip() == remote_tree:
                start = i + 1
                parents = [remote_head]
                ok(f"remote tree matches local commit {commits[i][:7]}; "
                   f"appending {len(commits) - start} new commit(s)")
                break
        else:
            info("no matching tree; full replay (remote history will be replaced)")
    elif args.full:
        info("--full requested; replaying every commit")

    if start >= len(commits):
        ok("already up to date; nothing to push")
        log(f"\nUP TO DATE: https://github.com/{args.owner}/{args.repo}/tree/{args.branch}")
        return 0

    rep = Replayer(client, root)
    final = None
    for sha in commits[start:]:
        text = git("cat-file", "commit", sha, cwd=root)
        fields, message = split_commit(text)
        body = {
            "message": message.rstrip("\n"),
            "tree": rep.tree(fields["tree"]),
            "author": parse_ident(fields["author"], "author"),
            "committer": parse_ident(fields["committer"], "committer"),
        }
        if parents:
            body["parents"] = parents
        made = client.call("POST", "/git/commits", body)
        final = made["sha"]
        parents = [final]
        ok(f"{sha[:7]} -> {final[:7]}  {message.splitlines()[0][:56]}")

    if remote_head:
        client.call("PATCH", f"/git/refs/heads/{args.branch}", {"sha": final, "force": True})
    else:
        client.call("POST", "/git/refs", {"ref": f"refs/heads/{args.branch}", "sha": final})
    ok(f"refs/heads/{args.branch} -> {final[:7]}")

    # ------------------------------------------------------------- verification
    head_now = client.call("GET", f"/git/refs/heads/{args.branch}")["object"]["sha"]
    tree_now = client.call("GET", f"/git/commits/{head_now}")["tree"]["sha"]
    count_now = len(client.call("GET", f"/commits?sha={args.branch}&per_page=100"))
    if tree_now != local_tree:
        die(f"tree mismatch: local={local_tree} remote={tree_now}")
    ok(f"tree identical: {tree_now}")
    info(f"remote commits: {count_now} (local: {len(commits)})")
    if head_now == local_head:
        ok(f"commit SHA identical: {head_now}")
    else:
        info(f"head SHA differs by design (local={local_head[:7]} remote={head_now[:7]})")
        info("GitHub normalises commit timestamps to UTC; tree equality is the guarantee")

    try:
        client.call("GET", f"/contents/.dsh-probe?ref={args.branch}")
        info("WARNING: leftover init probe file still present")
    except NotFound:
        ok("no leftover init probe file")

    log(f"\nPUSHED: https://github.com/{args.owner}/{args.repo}/tree/{args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
