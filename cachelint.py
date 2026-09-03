"""Check HTTP response headers for cache-control mistakes.

Reads header dumps (the kind of thing `curl -sI` prints) and flags
Cache-Control combinations that are contradictory, wasteful, or
probably not what the author meant.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

STATUS_LINE_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})")
HEADER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.*)$")

# Status codes where a cache would normally expect some cache-control
# guidance. Not exhaustive, just the common ones worth a nudge.
CACHEABLE_STATUSES = {200, 203, 204, 206, 300, 301, 404, 405, 410, 414, 501}


@dataclass
class Header:
    name: str
    value: str
    line: int

    @property
    def lower(self) -> str:
        return self.name.lower()


@dataclass
class Finding:
    line: int
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str

    def format(self, path: str) -> str:
        return f"{path}:{self.line}: {self.severity}: {self.message} [{self.code}]"


def parse_headers(text: str) -> Tuple[Optional[int], List[Header]]:
    status: Optional[int] = None
    headers: List[Header] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if status is None:
            m = STATUS_LINE_RE.match(line)
            if m:
                status = int(m.group(1))
                continue
        m = HEADER_LINE_RE.match(line)
        if m:
            headers.append(Header(m.group(1), m.group(2).strip(), i))
    return status, headers


def find_header(headers: List[Header], name: str) -> Optional[Header]:
    name = name.lower()
    # A repeated header means the last one is what actually reaches the
    # client, so search from the end rather than taking the first match.
    for h in reversed(headers):
        if h.lower == name:
            return h
    return None


def cache_control_directives(value: str) -> Dict[str, Optional[str]]:
    directives: Dict[str, Optional[str]] = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            directives[key.strip().lower()] = val.strip().strip('"')
        else:
            directives[part.lower()] = None
    return directives


def check_missing_cache_control(status: Optional[int], headers: List[Header]) -> List[Finding]:
    if status in CACHEABLE_STATUSES and find_header(headers, "Cache-Control") is None:
        return [Finding(1, "info", "missing-cache-control",
                         f"status {status} has no Cache-Control header; caches are left to guess")]
    return []


def check_contradictory_directives(headers: List[Header]) -> List[Finding]:
    cc = find_header(headers, "Cache-Control")
    if cc is None:
        return []
    d = cache_control_directives(cc.value)
    findings = []
    if "no-store" in d and ("max-age" in d or "s-maxage" in d):
        findings.append(Finding(cc.line, "error", "no-store-with-max-age",
                                 "no-store makes max-age pointless; the response is never stored"))
    if "public" in d and "private" in d:
        findings.append(Finding(cc.line, "error", "public-and-private",
                                 "Cache-Control cannot be both public and private"))
    if "immutable" in d and "max-age" not in d:
        findings.append(Finding(cc.line, "warning", "immutable-without-max-age",
                                 "immutable without max-age gives caches nothing to hold onto"))
    if "max-age" in d:
        raw = d["max-age"]
        try:
            if raw is None or int(raw) < 0:
                raise ValueError
        except ValueError:
            findings.append(Finding(cc.line, "error", "bad-max-age",
                                     f"max-age value {raw!r} is not a non-negative integer"))
    return findings


def check_stale_directives(headers: List[Header]) -> List[Finding]:
    cc = find_header(headers, "Cache-Control")
    if cc is None:
        return []
    d = cache_control_directives(cc.value)
    findings = []
    for name in ("stale-while-revalidate", "stale-if-error"):
        if name not in d:
            continue
        raw = d[name]
        try:
            if raw is None or int(raw) < 0:
                raise ValueError
        except ValueError:
            findings.append(Finding(cc.line, "error", f"bad-{name}",
                                     f"{name} value {raw!r} is not a non-negative integer"))
        if "no-store" in d:
            findings.append(Finding(cc.line, "error", f"{name}-with-no-store",
                                     f"no-store makes {name} pointless; the response is never stored"))
    if "stale-while-revalidate" in d and "max-age" not in d and "s-maxage" not in d:
        findings.append(Finding(cc.line, "warning", "stale-while-revalidate-without-max-age",
                                 "stale-while-revalidate extends a freshness lifetime that "
                                 "max-age/s-maxage never set"))
    return findings


def check_public_with_set_cookie(headers: List[Header]) -> List[Finding]:
    cc = find_header(headers, "Cache-Control")
    cookie = find_header(headers, "Set-Cookie")
    if cc is None or cookie is None:
        return []
    d = cache_control_directives(cc.value)
    if "public" in d and "private" not in d and "no-store" not in d:
        return [Finding(cc.line, "error", "public-with-set-cookie",
                         "Cache-Control: public alongside Set-Cookie can leak session data through shared caches")]
    return []


def check_vary_star(headers: List[Header]) -> List[Finding]:
    vary = find_header(headers, "Vary")
    cc = find_header(headers, "Cache-Control")
    if vary is None or cc is None or vary.value.strip() != "*":
        return []
    d = cache_control_directives(cc.value)
    if "max-age" in d or "public" in d:
        return [Finding(vary.line, "warning", "vary-star-defeats-cache",
                         "Vary: * combined with a positive freshness lifetime means shared caches "
                         "can almost never reuse this response")]
    return []


def check_no_cache_without_validator(headers: List[Header]) -> List[Finding]:
    cc = find_header(headers, "Cache-Control")
    if cc is None:
        return []
    d = cache_control_directives(cc.value)
    if ("no-cache" in d and find_header(headers, "ETag") is None
            and find_header(headers, "Last-Modified") is None):
        return [Finding(cc.line, "warning", "no-cache-without-validator",
                         "no-cache forces revalidation but there is no ETag or Last-Modified to revalidate against")]
    return []


CHECKS = [
    check_contradictory_directives,
    check_stale_directives,
    check_public_with_set_cookie,
    check_vary_star,
    check_no_cache_without_validator,
]


def lint_text(text: str) -> List[Finding]:
    status, headers = parse_headers(text)
    findings: List[Finding] = list(check_missing_cache_control(status, headers))
    for check in CHECKS:
        findings.extend(check(headers))
    return sorted(findings, key=lambda f: f.line)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cachelint",
        description="Check HTTP response headers for cache-control mistakes.",
    )
    parser.add_argument("paths", nargs="+", help="files containing raw HTTP response headers")
    args = parser.parse_args(argv)

    had_error = False
    for path in args.paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"{path}: {e.strerror}", file=sys.stderr)
            had_error = True
            continue
        for finding in lint_text(text):
            print(finding.format(path))
            if finding.severity == "error":
                had_error = True

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
