#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lievyljucy
#
# 本文件为本仓库原创实现，未包含任何第三方源码片段；运行时仅使用 Python 标准库。
"""Offline + live self-test. Runs before every archive step in CI.

No version string is hardcoded as an assertion: the live feed is fetched and
its OWN redundant fields are cross-checked. Fails closed on any inconsistency.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_dsh as w  # noqa: E402

FEED = "https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml"


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"SELFTEST FAIL: {name} {detail}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    print(f"SELFTEST OK: {name} {detail}", flush=True)


def main() -> int:
    # --- offline: YAML subset parser, nested sequence + folded scalars ---
    sample = """version: 1.2.3-rc.4
files:
  - url: >-
      https://download.deepseek.com/dsh-desk/bin/win-x64/a.exe
    size: 123
    sha512: >-
      AAAA
path: >-
  https://download.deepseek.com/dsh-desk/bin/win-x64/a.exe
sha512: >-
  AAAA
stagingPercentage: 100
releaseDate: '2026-01-01T00:00:00.000Z'
"""
    d = w.parse_updater_yaml(sample)
    check("yaml.version", d.get("version") == "1.2.3-rc.4", repr(d.get("version")))
    check("yaml.files_seq", isinstance(d.get("files"), list) and len(d["files"]) == 1)
    f0 = d["files"][0]
    check("yaml.folded_url", f0.get("url", "").endswith("a.exe"), repr(f0.get("url")))
    check("yaml.nested_size", f0.get("size") == "123", repr(f0.get("size")))
    check("yaml.nested_sha512", f0.get("sha512") == "AAAA", repr(f0.get("sha512")))
    check("yaml.quoted_date", d.get("releaseDate") == "2026-01-01T00:00:00.000Z")

    # --- offline: digest normalisation (base64 -> 64 bytes -> hex) ---
    b64 = base64.b64encode(bytes(range(64))).decode()
    hexd = bytes(range(64)).hex()
    check("digest.base64", w.normalize_digest(b64) == hexd)
    check("digest.hex_passthrough", w.normalize_digest(hexd.upper()) == hexd)
    check("digest.whitespace", w.normalize_digest("  " + b64[:10] + "\n" + b64[10:] + " ") == hexd)
    check("digest.empty_is_none", w.normalize_digest("") is None)

    # --- offline: version sort (prerelease below final) ---
    ordered = sorted(["0.2.1", "0.2.1-rc.3", "0.2.0-rc.2", "0.2.2-rc.1"],
                     key=w.version_sort_key)
    check("sort.semver", ordered == ["0.2.0-rc.2", "0.2.1-rc.3", "0.2.1", "0.2.2-rc.1"],
          repr(ordered))

    # --- offline: URL allowlist (accept + reject paths) ---
    check("url.allow", w.safe_url("https://download.deepseek.com/x") ==
          "https://download.deepseek.com/x")
    check("url.allow_cdn", w.safe_url("https://cdn.deepseek.com/x") ==
          "https://cdn.deepseek.com/x")
    for bad in ("http://download.deepseek.com/x", "https://evil.example/x",
                "https://download.deepseek.com.evil.test/x"):
        try:
            w.safe_url(bad)
        except SystemExit:
            continue
        print(f"SELFTEST FAIL: url.reject accepted {bad}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    print("SELFTEST OK: url.reject (http / foreign host / suffix-spoof)", flush=True)

    # --- offline: feed with inconsistent redundant fields must be refused ---
    class _Resp:
        def __init__(self, text: str):
            self._b = text.encode()
            self.headers = {"ETag": '"test"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"}
            self.status = 200
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False
    mismatched = sample.replace(
        "https://download.deepseek.com/dsh-desk/bin/win-x64/a.exe\nsha512",
        "https://download.deepseek.com/dsh-desk/bin/win-x64/OTHER.exe\nsha512")
    orig = w.http_request
    w.http_request = lambda *a, **k: _Resp(mismatched)
    try:
        try:
            w.fetch_feed(FEED, None)
        except SystemExit:
            print("SELFTEST OK: feed.reject_path_url_mismatch", flush=True)
        else:
            print("SELFTEST FAIL: feed.reject_path_url_mismatch", file=sys.stderr, flush=True)
            raise SystemExit(1)
    finally:
        w.http_request = orig

    # --- live: feed parse, field cross-check, ETag 304, HEAD proof ---
    meta, etag, changed = w.fetch_feed(FEED, None)
    check("live.feed_version", bool(meta and meta.get("version")), repr(meta.get("version")))
    check("live.feed_size", isinstance(meta.get("size"), int) and meta["size"] > 0,
          str(meta.get("size")))
    check("live.feed_sha512_hex", bool(meta.get("sha512")) and len(meta["sha512"]) == 128,
          repr(meta.get("sha512")))
    check("live.feed_etag", bool(etag), repr(etag))
    check("live.feed_changed", changed is True)

    meta2, etag2, changed2 = w.fetch_feed(FEED, etag)
    check("live.etag_304", meta2 is None and etag2 == etag and changed2 is False)

    size, status = w.probe_url(meta["url"])
    check("live.exe_head_200", status == 200, f"status={status}")
    check("live.exe_size_matches_feed", size == meta["size"], f"HEAD={size} feed={meta['size']}")

    print("SELFTEST SUMMARY: " + json.dumps({
        "feed": FEED,
        "version": meta["version"],
        "size": meta["size"],
        "sha512": meta["sha512"],
        "etag": etag,
        "exe_http": status,
        "exe_content_length": size,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
