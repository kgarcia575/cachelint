# cachelint

A small linter for HTTP response headers, focused on `Cache-Control` and
the headers that interact with it (`Vary`, `ETag`, `Last-Modified`,
`Set-Cookie`). It reads a file of raw headers and reports problems with
line numbers, the way a normal linter would.

Cache headers are easy to get subtly wrong: `no-store` and `max-age` on
the same response, `Cache-Control: public` next to a `Set-Cookie`,
`Vary: *` paired with a long freshness lifetime that no shared cache can
ever actually use. None of these are syntax errors, so nothing warns you
about them. This tries to.

## Usage

Save a response's headers to a file, for example by redirecting the
output of `curl -sI`:

```
curl -sI https://example.com/ > headers.txt
```

A full `curl -v` transcript works too, including redirects:

```
curl -v https://example.com/ > headers.txt 2>&1
```

cachelint reads the `< ...` response lines and ignores the rest (the
`> ` request lines and `* ` connection chatter). If the transcript has
more than one response in it, because of a redirect or a 100-continue,
each one is checked separately.

or write one by hand:

```
HTTP/1.1 200 OK
Cache-Control: public, max-age=3600
Set-Cookie: session=abc123
Vary: *
```

Then run:

```
python3 cachelint.py headers.txt
```

Example output for the file above:

```
headers.txt:2: warning: Vary: * combined with a positive freshness lifetime means shared caches can almost never reuse this response [vary-star-defeats-cache]
headers.txt:3: error: Cache-Control: public alongside Set-Cookie can leak session data through shared caches [public-with-set-cookie]
```

The exit code is `1` if any finding is an `error`, `0` otherwise, so it
can be dropped into a CI step.

You can pass more than one file:

```
python3 cachelint.py headers/*.txt
```

For CI tooling, pass `--format json` to get a single JSON array on stdout
instead of one line per finding:

```
python3 cachelint.py --format json headers.txt
```

```json
[
  {
    "path": "headers.txt",
    "line": 2,
    "severity": "warning",
    "code": "vary-star-defeats-cache",
    "message": "Vary: * combined with a positive freshness lifetime means shared caches can almost never reuse this response"
  },
  {
    "path": "headers.txt",
    "line": 3,
    "severity": "error",
    "code": "public-with-set-cookie",
    "message": "Cache-Control: public alongside Set-Cookie can leak session data through shared caches"
  }
]
```

A file that can't be read shows up as `{"path": ..., "error": ...}` instead
of a finding. The exit code rule is unchanged either way.

## What it checks right now

- `no-store` combined with `max-age` (the max-age is dead weight)
- `Cache-Control: public` and `Cache-Control: private` on the same response
- `immutable` without a `max-age`
- a `max-age` that isn't a non-negative integer
- `stale-while-revalidate` or `stale-if-error` that isn't a non-negative integer
- `stale-while-revalidate` or `stale-if-error` combined with `no-store` (dead weight, same problem as `max-age`)
- `stale-while-revalidate` with no `max-age`/`s-maxage` to extend
- `Cache-Control: public` next to `Set-Cookie` (cookie leakage into shared caches)
- `Vary: *` next to a cacheable `max-age`/`public`
- `no-cache` with no `ETag` or `Last-Modified` to revalidate against
- a cacheable status code with no `Cache-Control` at all (informational)

## Install

No dependencies beyond the standard library.

```
pip install -e .
cachelint headers.txt
```

or just run `python3 cachelint.py headers.txt` directly without installing.
