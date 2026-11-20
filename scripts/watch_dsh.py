#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lievyljucy
#
# 本文件为本仓库原创实现，未包含任何第三方源码片段；运行时仅使用 Python 标准库。
"""
DeepSeek Harness Windows installer watcher / archiver.

Monitors the official electron-updater feed, downloads every NEW version it can
PROVE exists, verifies size + SHA-512, and emits files for the CI job to publish
as GitHub Release assets (permanent, non-expiring storage).

Design rules (rigor):
  * Nothing is ever assumed to exist. Every version candidate must be backed by
    a real HTTP 200 (feed payload, HEAD probe, or gap probe).
  * The feed's own size/sha512 are mandatory. A feed version without them is
    rejected, never downloaded unverified.
  * "Already archived" is decided by the REMOTE GitHub Release asset, and its
    size must match the size we expect. A truncated upload is retried.
  * versions.json is a DERIVED index, never the truth source.
  * Zero third-party dependencies: Python 3.9+ standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FEED_BASE = "https://download.deepseek.com/dsh-desk/feeds/win-x64"
BIN_BASE = "https://download.deepseek.com/dsh-desk/bin/win-x64"
EXE_TEMPLATE = "deepseek-harness-{version}-win-x64.exe"
ALLOWED_HOSTS = {"download.deepseek.com", "cdn.deepseek.com"}
UA = "dsh-harness-archiver/2.0 (+GitHub Actions)"
CHUNK = 1024 * 1024
BLOCK_MARKERS = (">-", ">", ">+", "|-", "|", "|+")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[watch-dsh] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[watch-dsh] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# Minimal YAML subset parser for electron-updater generic feeds.
# Supports mappings, sequences of mappings, quoted scalars and >-/|- blocks.
# --------------------------------------------------------------------------
def parse_updater_yaml(text: str) -> dict:
    lines = []
    for raw in text.replace("\r\n", "\n").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    if not lines:
        return {}

    def scalar(s: str) -> str:
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
            return s[1:-1]
        return s

    def collect_block(j: int, key_indent: int, marker: str):
        parts, k = [], j + 1
        while k < len(lines) and lines[k][0] > key_indent:
            parts.append(lines[k][1])
            k += 1
        sep = "\n" if marker.startswith("|") else " "
        return sep.join(parts).strip(), k

    def parse_line(j: int, indent: int, content: str):
        key, _, val = content.partition(":")
        key, val = key.strip(), val.strip()
        if val in BLOCK_MARKERS:
            return {key: collect_block(j, indent, val)[0]}, collect_block(j, indent, val)[1]
        return {key: scalar(val)}, j + 1

    def parse_map(j: int, indent: int):
        result: dict = {}
        while j < len(lines) and lines[j][0] == indent and not lines[j][1].startswith("- "):
            if not re.match(r"^[A-Za-z0-9_]+:", lines[j][1]):
                j += 1
                continue
            item, j = parse_line(j, indent, lines[j][1])
            # a bare key followed by a deeper block
            if j < len(lines) and lines[j][0] > indent and list(item.values())[0] == "":
                child_indent = lines[j][0]
                if lines[j][1].startswith("- "):
                    result[list(item)[0]], j = parse_seq(j, child_indent)
                else:
                    result[list(item)[0]], j = parse_map(j, child_indent)
            else:
                result.update(item)
        return result, j

    def parse_seq(j: int, indent: int):
        items = []
        while j < len(lines) and lines[j][0] == indent and lines[j][1].startswith("- "):
            content = lines[j][1][2:].strip()
            item: dict = {}
            if content:
                first, j2 = parse_line(j, indent + 2, content)
                item.update(first)
                j = j2
            else:
                j += 1
            if j < len(lines) and lines[j][0] == indent + 2 and not lines[j][1].startswith("- "):
                rest, j = parse_map(j, indent + 2)
                item.update(rest)
            items.append(item)
        return items, j

    root, _ = parse_map(0, lines[0][0])
    return root


def safe_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS:
        die(f"refusing non-official URL: {url!r}")
    return url


def http_request(url: str, method: str = "GET", headers: dict | None = None,
                 timeout: int = 120):
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        **(headers or {}),
    })
    return urllib.request.urlopen(req, timeout=timeout)


# --------------------------------------------------------------------------
# Feed: conditional GET (ETag) + strict parse & cross-validation
# --------------------------------------------------------------------------
def normalize_digest(s: str | None) -> str | None:
    """electron-updater ships sha512 as base64; accept hex too -> lowercase hex."""
    s = re.sub(r"\s+", "", s or "")
    if not s:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{128}", s):
        return s.lower()
    try:
        b = base64.b64decode(s, validate=True)
        if len(b) == 64:
            return b.hex()
    except Exception:
        pass
    die(f"unrecognised sha512 digest in feed: {s[:40]}...")
    return None


def fetch_feed(feed_url: str, etag: str | None):
    """returns (meta | None, new_etag, changed) ; meta None on 304."""
    hdrs = {"If-None-Match": etag} if etag else {}
    try:
        with http_request(safe_url(feed_url), "GET", hdrs, timeout=60) as r:
            text = r.read().decode("utf-8", "replace")
            new_etag = r.headers.get("ETag")
            last_mod = r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag, False
        die(f"feed HTTP {e.code}: {feed_url}")
    except Exception as e:
        die(f"feed request failed: {e}")

    raw = parse_updater_yaml(text)
    version = str(raw.get("version") or "").strip()
    if not version or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
        die(f"feed has no usable version: {raw!r}")

    files = raw.get("files") if isinstance(raw.get("files"), list) else []
    f0 = files[0] if files and isinstance(files[0], dict) else {}

    # Cross-check the redundant fields electron-updater emits. A mismatch means
    # the feed format changed (or is inconsistent) -> refuse to act on it.
    path = str(raw.get("path") or "").strip()
    if path and f0.get("url") and str(f0["url"]).strip() != path:
        die(f"feed path/files[0].url disagree: {path!r} vs {f0['url']!r}")
    top_hash = str(raw.get("sha512") or "").strip()
    file_hash = str(f0.get("sha512") or "").strip()
    if top_hash and file_hash and top_hash != file_hash:
        die("feed sha512/files[0].sha512 disagree")

    url = path or str(f0.get("url") or "").strip()
    if not url:
        url = f"{BIN_BASE}/{EXE_TEMPLATE.format(version=version)}"
    safe_url(url)

    size = None
    for candidate in (f0.get("size"), raw.get("size")):
        if candidate is None:
            continue
        try:
            size = int(str(candidate).strip())
            break
        except ValueError:
            pass
    if not size or size <= 0:
        die(f"feed {version} has no positive size; refusing unverified download")

    digest = normalize_digest(top_hash or file_hash)
    if not digest:
        die(f"feed {version} has no sha512; refusing unverified download")

    meta = {
        "version": version,
        "url": url,
        "fileName": urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name),
        "size": size,
        "sha512": digest,
        "releaseDate": raw.get("releaseDate") or now_iso(),
        "stagingPercentage": raw.get("stagingPercentage"),
        "channel": Path(urllib.parse.urlparse(feed_url).path).stem,
        "feed": feed_url,
        "feedETag": new_etag,
        "feedLastModified": last_mod,
        "source": "feed",
    }
    return meta, new_etag, True


# --------------------------------------------------------------------------
# Archived set: REMOTE GitHub Release assets, with size validation
# --------------------------------------------------------------------------
def gh_release_assets() -> dict | None:
    """tag -> list of exe-asset sizes. None means gh unavailable (offline)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or not shutil.which("gh"):
        return None
    jq = ('.[] | .tag_name as $t | (.assets // [])[] '
          '| select(.name | endswith(".exe")) '
          '| {tag: $t, name: .name, size: .size}')
    try:
        p = subprocess.run(
            ["gh", "api", "--paginate", "-H", "Accept: application/vnd.github+json",
             f"repos/{repo}/releases", "--jq", jq],
            capture_output=True, text=True, timeout=180,
        )
        if p.returncode != 0:
            log(f"gh release list unavailable ({p.stderr.strip()[:160]}); offline mode")
            return None
        out: dict = {}
        for line in p.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                out.setdefault(rec["tag"], []).append(int(rec.get("size") or 0))
            except Exception as e:
                log(f"ignoring unparsable gh asset line ({e}): {line[:120]}")
        return out
    except Exception as e:
        log(f"gh release list failed ({e}); offline mode")
        return None


def load_index(path: Path) -> dict:
    if not path.exists():
        return {"updatedAt": None, "versions": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(d.get("versions"), list):
            raise ValueError("versions is not a list")
        return d
    except Exception as e:
        log(f"index unreadable ({e}), starting fresh")
        return {"updatedAt": None, "versions": []}


def version_sort_key(v: str):
    """Rough semver: prerelease ranks BELOW its final release."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-?([A-Za-z]+)\.?(\d+)?)?(?:\.(\d+))?", v)
    if m:
        core = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        pre = m.group(4) or ""
        num = int(m.group(5) or 0)
        # final release => rank 1 ; prerelease => rank 0
        return core + (1 if not pre else 0, num, int(m.group(6) or 0))
    return (0, 0, 0, 0, 0) + tuple(int(x) for x in re.findall(r"\d+", v))


# --------------------------------------------------------------------------
# HTTP probing / download / verification
# --------------------------------------------------------------------------
def probe_url(url: str) -> tuple[int | None, int | None]:
    """returns (Content-Length, HTTP status or None on network error)."""
    try:
        with http_request(safe_url(url), "HEAD", timeout=60) as r:
            cl = r.headers.get("Content-Length")
            return (int(cl) if cl else None), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception as e:
        log(f"HEAD failed for {url}: {e}")
        return None, None


def verify_file(path: Path, meta: dict) -> str:
    """Stream-hash an existing file; enforce size + sha512 when known."""
    h = hashlib.sha512()
    got = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            got += len(chunk)
    digest = h.hexdigest()
    if meta.get("size") and got != meta["size"]:
        raise ValueError(f"size mismatch: got {got}, expected {meta['size']}")
    if meta.get("sha512") and digest != meta["sha512"]:
        raise ValueError(f"sha512 mismatch: got {digest[:24]}..., expected {meta['sha512'][:24]}...")
    meta["size"], meta["sha512"] = got, digest
    return digest


def download_verified(meta: dict, dest: Path, retries: int = 3) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = safe_url(meta["url"])

    for attempt in range(1, retries + 1):
        try:
            tmp.unlink(missing_ok=True)
            h = hashlib.sha512()
            got = 0
            next_log = 64 * 1024 * 1024
            with http_request(url, "GET", {"Accept-Encoding": "identity"}, timeout=180) as r, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if got >= next_log:
                        log(f"  ... {got / 1048576:.0f} MiB")
                        next_log += 64 * 1024 * 1024
            digest = h.hexdigest()
            if meta.get("size") and got != meta["size"]:
                raise ValueError(f"size mismatch: got {got}, feed says {meta['size']}")
            if meta.get("sha512") and digest != meta["sha512"]:
                raise ValueError(f"sha512 mismatch: got {digest[:24]}..., expected {meta['sha512'][:24]}...")
            tmp.replace(dest)
            log(f"OK {dest.name}: {got} bytes, sha512={digest}")
            meta["size"], meta["sha512"] = got, digest
            return meta
        except Exception as e:
            log(f"download attempt {attempt}/{retries} failed: {e}")
            tmp.unlink(missing_ok=True)
            if attempt == retries:
                die(f"giving up on {meta['version']}")
            time.sleep(3 * attempt)
    return meta


# --------------------------------------------------------------------------
# Candidate generation: backfill list + optional gap probing
# --------------------------------------------------------------------------
def make_probe_meta(version: str, url: str, size: int, source: str) -> dict:
    return {
        "version": version,
        "url": url,
        "fileName": EXE_TEMPLATE.format(version=version),
        "size": size,
        "sha512": None,
        "releaseDate": None,
        "stagingPercentage": None,
        "channel": source,
        "feed": None,
        "source": source,
    }


def read_backfill(path: Path | None, known: set) -> list:
    if not path or not path.exists():
        return []
    out = []
    seen = set(known)
    for line in path.read_text(encoding="utf-8").splitlines():
        v = line.split("#", 1)[0].strip()
        if not v or v in seen or not re.fullmatch(r"[A-Za-z0-9._+-]+", v):
            continue
        seen.add(v)
        size, status = probe_url(f"{BIN_BASE}/{EXE_TEMPLATE.format(version=v)}")
        if status != 200 or not size:
            log(f"  backfill {v}: HTTP {status} -> skip")
            continue
        log(f"  backfill {v}: HTTP 200, {size} bytes")
        out.append(make_probe_meta(v, f"{BIN_BASE}/{EXE_TEMPLATE.format(version=v)}", size, "backfill"))
    return out


def gap_probe(archived: set, feed_versions: set, steps: int, known: set) -> list:
    """The official feed exposes ONLY the currently staged build. Verified
    2026-11-13: 0.2.1-rc.5 and 0.2.2-rc.2 exist on the CDN but are absent from
    nightly.yml. So on EVERY poll we enumerate a BOUNDED set of plausible
    version strings and admit ONLY those that actually return HTTP 200.

    No version is invented as fact: every admitted one is proven by a live
    HEAD response. Bases come from archived tags, feed versions, and the
    verified backfill list (all passed in via `known`).
    """
    if steps <= 0:
        return []
    bases: set = set()
    for v in list(archived) + list(feed_versions) + list(known):
        m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-rc\.\d+)?", v)
        if m:
            bases.add(m.group(1))
    if not bases:
        return []
    cand: set = set()
    for b in bases:
        for n in range(0, steps + 1):
            cand.add(f"{b}-rc.{n}")
        parts = b.split(".")
        try:
            nxt = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
            for n in range(0, min(steps + 1, 3)):
                cand.add(f"{nxt}-rc.{n}")
            cand.add(nxt)
        except (ValueError, IndexError):
            pass
    cand -= set(known) | set(feed_versions) | set(archived)
    out = []
    for v in sorted(cand, key=version_sort_key):
        url = f"{BIN_BASE}/{EXE_TEMPLATE.format(version=v)}"
        size, status = probe_url(url)
        if status == 200 and size:
            log(f"  gap-probe {v}: HTTP 200, {size} bytes -> ADMIT")
            out.append(make_probe_meta(v, url, size, "gap-probe"))
    return out


def write_notes(meta: dict, path: Path) -> None:
    src = meta.get("source")
    if src == "feed":
        proof = "Size and SHA-512 verified against the official updater feed."
    elif src == "gap-probe":
        proof = "Existence proven by live HTTP 200; size taken from Content-Length; SHA-512 computed locally (no feed metadata)."
    else:
        proof = "Existence proven by live HTTP 200; size taken from Content-Length; SHA-512 computed locally (historical backfill, no feed metadata)."
    lines = [
        f"# DeepSeek Harness {meta['version']}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Version | `{meta['version']}` |",
        f"| Channel | `{meta.get('channel') or '-'}` |",
        f"| Release date | `{meta.get('releaseDate') or '-'}` |",
        f"| Size | `{meta.get('size') or '-'}` bytes |",
        f"| SHA-512 | `{meta.get('sha512') or '-'}` |",
        f"| Source | `{src}` |",
        f"| Official URL | <{meta.get('url')}> |",
        f"| Archived at | `{meta.get('archivedAt')}` |",
        "",
        proof,
        "",
        "Auto-archived by `archive-dsh.yml`.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_outputs(values: dict) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    import uuid
    with open(target, "a", encoding="utf-8") as f:
        for k, v in values.items():
            s = str(v)
            if "\n" in s:
                delim = f"ghadelim_{uuid.uuid4().hex}"
                f.write(f"{k}<<{delim}\n{s}\n{delim}\n")
            else:
                f.write(f"{k}={s}\n")


def append_summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(text + "\n")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Watch & archive DeepSeek Harness installers")
    ap.add_argument("--feed", action="append", default=[])
    ap.add_argument("--channel", default="nightly")
    ap.add_argument("--index", default="versions.json")
    ap.add_argument("--etag-file", default=".feed-etags.json")
    ap.add_argument("--cache-file", default=".feed-cache.json")
    ap.add_argument("--out-dir", default="dist")
    ap.add_argument("--backfill", default=None)
    ap.add_argument("--max-new", type=int, default=2)
    ap.add_argument("--probe-steps", type=int, default=12,
                    help="bounded CDN enumeration, runs on EVERY poll (0=off). "
                         "Feed exposes only the staged version; see VERIFICATION.md")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect only, no download and NO writes of any state file")
    ap.add_argument("--force", action="store_true", help="ignore ETag 304 shortcut")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    feeds = args.feed or [f"{FEED_BASE}/{args.channel}.yml"]

    def rp(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else root / q

    index_path, etag_path, cache_path, out_dir = map(
        rp, (args.index, args.etag_file, args.cache_file, args.out_dir))
    backfill = rp(args.backfill) if args.backfill else None

    etags, cache = {}, {}
    if etag_path.exists():
        try:
            etags = json.loads(etag_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    index = load_index(index_path)
    old_by_v = {v["version"]: v for v in index.get("versions", []) if isinstance(v, dict) and v.get("version")}

    def persist() -> None:
        for p in (etag_path, cache_path, index_path):
            p.parent.mkdir(parents=True, exist_ok=True)
        etag_path.write_text(json.dumps(etags, indent=2) + "\n", encoding="utf-8")
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 1) Poll feeds. On 304 replay the cached payload so a failed publish retries.
    feed_meta: dict = {}
    for u in feeds:
        log(f"polling {u}")
        meta, new_etag, _ = fetch_feed(u, None if args.force else etags.get(u))
        if meta is None:
            cached = cache.get(u)
            if not cached or not cached.get("version"):
                log("  304 but no cached payload; refetching without If-None-Match")
                meta, new_etag, _ = fetch_feed(u, None)
            else:
                log(f"  304 Not Modified; replaying cached {cached['version']}")
                meta = dict(cached)
        if meta is None:
            continue
        log(f"  version={meta['version']} size={meta['size']} date={meta['releaseDate']} etag={new_etag}")
        log(f"  sha512={meta['sha512']}")
        feed_meta[meta["version"]] = meta
        cache[u] = {k: v for k, v in meta.items() if k not in ("feedETag", "feedLastModified")}
        if new_etag:
            etags[u] = new_etag

    # 2) Truth source: remote Release assets, validated by size.
    expected_sizes: dict = {}
    for v, rec in old_by_v.items():
        if isinstance(rec.get("size"), int) and rec["size"] > 0:
            expected_sizes[v] = rec["size"]
    for c in cache.values():
        if isinstance(c, dict) and c.get("version") and isinstance(c.get("size"), int):
            expected_sizes.setdefault(c["version"], c["size"])

    assets = gh_release_assets()
    if assets is None:
        archived = set(old_by_v)
        log(f"offline mode: {len(archived)} version(s) from local index (NOT authoritative)")
    else:
        archived = set()
        for tag, sizes in assets.items():
            exp = expected_sizes.get(tag)
            ok = any(s > 0 and (exp is None or s == exp) for s in sizes)
            if ok:
                archived.add(tag)
            else:
                log(f"  {tag}: remote exe size {sizes} != expected {exp} -> RETRY")
        log(f"remote truth source: {len(archived)} fully archived version(s)")

    # 3) Candidates. Feed version first (newest), then proven historical ones.
    #    Gap probing runs EVERY poll: the feed is not a complete version list.
    fresh = [feed_meta[v] for v in sorted(feed_meta, key=version_sort_key, reverse=True)
             if v not in archived]
    known = set(archived) | set(feed_meta) | set(old_by_v)
    historical = read_backfill(backfill, known)
    for m in historical:
        known.add(m["version"])
    probed = gap_probe(archived, set(feed_meta), args.probe_steps, known)
    for m in probed:
        known.add(m["version"])
    historical += probed
    historical.sort(key=lambda m: version_sort_key(m["version"]))
    candidates = fresh + historical
    if args.max_new > 0:
        candidates = candidates[:args.max_new]

    if not candidates:
        log("no new version. nothing to do.")
        persist()
        set_outputs({"changed": "false", "new_versions": "[]", "new_files": ""})
        append_summary("## DSH archiver\nNo new version proven on the official feed.")
        return 0

    log(f"{len(candidates)} candidate(s): {', '.join(m['version'] for m in candidates)}")

    # 4) Dry run: candidates already proven by HEAD; absolutely no writes.
    if args.dry_run:
        for m in candidates:
            log(f"  DRY-RUN {m['version']}: size={m['size']} "
                f"sha512={'feed' if m.get('sha512') else 'computed-on-download'}")
        log("  dry-run: no download, no index/etag/cache writes")
        set_outputs({"changed": "false", "new_versions": "[]", "new_files": ""})
        return 0

    # 5) Download (or re-verify a reused local file) + hash.
    out_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for m in candidates:
        dest = out_dir / m["fileName"]
        if dest.exists():
            try:
                verify_file(dest, m)
                log(f"  reuse+verified {dest.name}: sha512={m['sha512']}")
            except Exception as e:
                log(f"  reuse rejected ({e}); re-downloading")
                download_verified(m, dest)
        else:
            download_verified(m, dest)
        m["archivedAt"] = now_iso()
        m["releaseTag"] = m["version"]
        write_notes(m, out_dir / f"{m['fileName']}.notes.md")
        done.append(m)
        append_summary(f"- `{m['version']}` — {m['size']} bytes — `{m['sha512']}`")

    # 6) Rewrite derived index.
    merged = dict(old_by_v)
    for v in archived:
        merged.setdefault(v, {"version": v, "source": "release",
                              "archivedAt": old_by_v.get(v, {}).get("archivedAt")})
    for m in done:
        merged[m["version"]] = {k: v for k, v in m.items()
                                if k not in ("feedETag", "feedLastModified")}
    index = {
        "updatedAt": now_iso(),
        "feed": feeds[0] if len(feeds) == 1 else feeds,
        "count": len(merged),
        "versions": [merged[k] for k in sorted(merged, key=version_sort_key)],
    }
    # NOTE: persist() creates the parent dirs but is called AFTER this write,
    # so the index parent must be created here first (state/ may not exist yet
    # on a fresh runner / first run with a new version).
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist()

    set_outputs({
        "changed": "true" if done else "false",
        "new_versions": json.dumps([m["version"] for m in done]),
        "new_files": "\n".join(str(out_dir / m["fileName"]) for m in done),
    })
    append_summary(f"\n## DSH archiver\nArchived {len(done)} version(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
