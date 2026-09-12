#!/usr/bin/env python3
"""
WRM URL Merge Tool

Make the list of active URLs for a single WRM merchant match a plain text
file of URLs. URLs in the file that the merchant does not have are added.
Active URLs the merchant has that are not in the file are disabled.

This works around a limitation of the WRM Bulk Import "advanced merge" mode,
where all URLs for a merchant must fit into a single semicolon separated
spreadsheet cell.

Commands:
    sponsors            List the sponsors your user can see
    search              Find merchants and show their merchantId
    sync                Merge a URL file into a merchant

Credentials are read from the environment (or a .env file in the current
directory):
    WRM_USERNAME        VikingCloud Portal user name (API enabled)
    WRM_PASSWORD        VikingCloud Portal password
    WRM_API_URL         Optional. Defaults to https://api.vikingcloud.com

API reference: https://developer.vikingcloud.com/openapi/wrm/
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

__version__ = "1.0.0"

DEFAULT_API_URL = "https://api.vikingcloud.com"
TOKEN_PATH = "/token"
WRM_BASE_PATH = "/wrm/v1"

# Pattern published in the WRM API schema for the "url" field.
API_URL_PATTERN = re.compile(
    r"^(https?)://[-a-zA-Z0-9+&@#/%?=~_|!:,.;]*[-a-zA-Z0-9+&@#/%=~_|]$"
)
API_URL_MIN_LEN = 7
API_URL_MAX_LEN = 255

HOST_LABEL = re.compile(r"^(?!-)[a-zA-Z0-9-]{1,63}(?<!-)$")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_INPUT = 2
EXIT_ABORTED = 3

log = logging.getLogger("wrm_url_merge")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class WrmError(Exception):
    """Base error for this tool."""


class WrmAuthError(WrmError):
    """Authentication with the Token API failed."""


class WrmApiError(WrmError):
    """The WRM API returned an error response."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class Config:
    username: str
    password: str
    api_url: str

    @classmethod
    def from_env(cls) -> "Config":
        # Look for .env in the current directory first, then next to this
        # script. load_dotenv never overrides variables already set in the
        # shell, so real environment variables always win over the file.
        for candidate in (
            os.path.join(os.getcwd(), ".env"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ):
            if os.path.isfile(candidate):
                load_dotenv(candidate)
                log.debug("Loaded %s", candidate)
                break
        username = os.getenv("WRM_USERNAME", "").strip()
        password = os.getenv("WRM_PASSWORD", "")
        api_url = os.getenv("WRM_API_URL", DEFAULT_API_URL).strip().rstrip("/")
        missing = [
            name
            for name, value in (("WRM_USERNAME", username), ("WRM_PASSWORD", password))
            if not value
        ]
        if missing:
            raise WrmError(
                "Missing credentials: "
                + ", ".join(missing)
                + ". Set them as environment variables or in a .env file."
            )
        if not api_url.lower().startswith(("http://", "https://")):
            raise WrmError(f"WRM_API_URL must start with http:// or https:// (got {api_url!r})")
        return cls(username=username, password=password, api_url=api_url)


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------


def validate_url(raw: str) -> Tuple[bool, str]:
    """
    Return (ok, reason). A URL is acceptable when it would be accepted by the
    WRM API and has a plausible host name.
    """
    url = raw.strip()
    if not url:
        return False, "empty"
    if len(url) < API_URL_MIN_LEN or len(url) > API_URL_MAX_LEN:
        return False, f"length must be {API_URL_MIN_LEN} to {API_URL_MAX_LEN} characters"
    if any(ch.isspace() for ch in url):
        return False, "contains whitespace"
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return False, f"cannot be parsed ({exc})"
    if parts.scheme.lower() not in ("http", "https"):
        return False, "scheme must be http or https"
    host = parts.hostname
    if not host:
        return False, "missing host name"
    if host != "localhost":
        labels = host.split(".")
        if len(labels) < 2 or not all(HOST_LABEL.match(label) for label in labels):
            # Allow bare IPv4 addresses, reject anything else without a dot.
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
                return False, f"host name {host!r} is not valid"
    try:
        parts.port  # raises ValueError for a bad port
    except ValueError:
        return False, "port is not valid"
    if not API_URL_PATTERN.match(url):
        return False, "contains characters the WRM API does not accept"
    return True, ""


def normalize_url(raw: str) -> str:
    """
    Produce a comparison key so that trivially different spellings of the
    same site are treated as equal:
      - scheme and host are lower cased
      - default ports (80 for http, 443 for https) are dropped
      - the fragment (anything after '#') is dropped
      - trailing slashes on the path are removed
    The original string is still what gets sent to the API when adding.
    """
    url = raw.strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = None
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def read_url_file(path: str) -> List[str]:
    """
    Read URLs from a text file. One URL per line is the expected format, but
    semicolon separated values (the Bulk Import cell format) are also split
    apart. Blank lines and lines beginning with '#' are ignored.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise WrmError(f"Cannot read URL file {path!r}: {exc}") from exc

    urls: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for piece in stripped.split(";"):
            piece = piece.strip()
            if piece:
                urls.append(piece)
    return urls


# --------------------------------------------------------------------------
# WRM API client
# --------------------------------------------------------------------------


class WrmClient:
    """Minimal client for the Token API and the WRM API endpoints this tool uses."""

    def __init__(self, config: Config, timeout: float = 30.0, max_retries: int = 4):
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"wrm-url-merge/{__version__}",
            }
        )
        self._token_expires_at = 0.0

    # -- auth -------------------------------------------------------------

    def login(self) -> None:
        url = self.config.api_url + TOKEN_PATH
        log.debug("Requesting token from %s", url)
        try:
            resp = self.session.post(
                url,
                json={"username": self.config.username, "password": self.config.password},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WrmAuthError(f"Could not reach the Token API at {url}: {exc}") from exc

        if resp.status_code != 200:
            raise WrmAuthError(
                f"Token API returned HTTP {resp.status_code}. "
                "Check WRM_USERNAME / WRM_PASSWORD and that the user is enabled for API access."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise WrmAuthError("Token API returned a non-JSON response") from exc

        id_token = data.get("idToken") or data.get("IdToken")
        if not id_token:
            raise WrmAuthError("Token API response did not contain an idToken")
        token_type = data.get("tokenType") or "Bearer"
        self.session.headers["Authorization"] = f"{token_type} {id_token}"

        # The Token API docs describe an AccessToken that goes in x-jwt-assertion.
        access_token = data.get("accessToken") or data.get("AccessToken")
        if access_token:
            self.session.headers["x-jwt-assertion"] = access_token

        expires_in = data.get("expiresIn") or 3600
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = 3600
        # Refresh a little early so a long run never sends an expired token.
        self._token_expires_at = time.monotonic() + max(expires_in - 120, 60)
        log.debug("Token obtained, valid for about %s seconds", expires_in)

    def _ensure_token(self) -> None:
        if time.monotonic() >= self._token_expires_at:
            self.login()

    # -- low level request ------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = self.config.api_url + WRM_BASE_PATH + path
        attempt = 0
        reauthed = False
        while True:
            attempt += 1
            self._ensure_token()
            log.debug("%s %s params=%s", method, url, kwargs.get("params"))
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt <= self.max_retries:
                    delay = min(2 ** attempt, 30)
                    log.warning("Request failed (%s); retrying in %ss", exc, delay)
                    time.sleep(delay)
                    continue
                raise WrmApiError(f"{method} {url} failed: {exc}") from exc

            if resp.status_code == 401 and not reauthed:
                log.info("Token rejected, re-authenticating once")
                reauthed = True
                self.login()
                continue

            if resp.status_code == 429 and attempt <= self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 60)
                except ValueError:
                    delay = min(2 ** attempt, 60)
                if delay > 300:
                    raise WrmApiError(
                        f"Rate limited; the API asked us to wait {int(delay)} seconds. "
                        "Please try again later.",
                        status=429,
                    )
                log.warning("Rate limited (429); waiting %ss", int(delay))
                time.sleep(delay)
                continue

            if resp.status_code in (500, 502, 503, 504) and attempt <= self.max_retries:
                delay = min(2 ** attempt, 30)
                log.warning("Server error %s; retrying in %ss", resp.status_code, delay)
                time.sleep(delay)
                continue

            return resp

    @staticmethod
    def _raise_for_status(resp: requests.Response, what: str) -> None:
        if 200 <= resp.status_code < 300:
            return
        body = resp.text.strip()
        detail = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail = data.get("message") or data.get("error") or data.get("detail") or ""
                if isinstance(detail, list):
                    detail = "; ".join(str(d) for d in detail)
        except ValueError:
            pass
        message = f"{what}: HTTP {resp.status_code}"
        if detail:
            message += f" ({detail})"
        elif body:
            message += f" ({body[:300]})"
        raise WrmApiError(message, status=resp.status_code, body=body)

    def _json(self, resp: requests.Response, what: str):
        self._raise_for_status(resp, what)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise WrmApiError(f"{what}: response was not JSON") from exc

    # -- endpoints --------------------------------------------------------

    def list_sponsors(self, sponsor_id: Optional[int] = None) -> List[dict]:
        params = {"sponsorId": sponsor_id} if sponsor_id is not None else None
        resp = self._request("GET", "/sponsors", params=params)
        data = self._json(resp, "Listing sponsors")
        return data if isinstance(data, list) else []

    def iter_merchants(self, sponsor_id: Optional[int] = None, page_size: int = 100) -> Iterable[dict]:
        page = 0
        seen = 0
        while True:
            params = {"size": page_size, "page": page, "sort-by": "id", "sort-dir": "asc"}
            if sponsor_id is not None:
                params["sponsorId"] = sponsor_id
            resp = self._request("GET", "/merchants", params=params)
            data = self._json(resp, f"Listing merchants (page {page})") or {}
            items = data.get("pageItems") or []
            for item in items:
                yield item
            seen += len(items)
            total = data.get("totalItems")
            if not items or (isinstance(total, int) and seen >= total):
                return
            page += 1

    def get_merchant(self, merchant_id: int) -> dict:
        resp = self._request("GET", f"/merchants/{merchant_id}")
        if resp.status_code == 404:
            raise WrmApiError(f"Merchant {merchant_id} was not found", status=404)
        return self._json(resp, f"Fetching merchant {merchant_id}") or {}

    def list_urls(self, merchant_id: int, potential: bool = False) -> List[dict]:
        resp = self._request(
            "GET",
            f"/merchants/{merchant_id}/urls",
            params={"potential": "true" if potential else "false"},
        )
        data = self._json(resp, f"Listing URLs for merchant {merchant_id}")
        return data if isinstance(data, list) else []

    def add_urls(self, merchant_id: int, urls: Sequence[str], boarding_scan: bool = False) -> List[dict]:
        payload = [{"url": u, "boardingScan": boarding_scan} for u in urls]
        resp = self._request("POST", f"/merchants/{merchant_id}/urls", json=payload)
        data = self._json(resp, f"Adding {len(urls)} URL(s) to merchant {merchant_id}")
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []

    def disable_url(self, merchant_id: int, url_id: int) -> None:
        resp = self._request("DELETE", f"/merchants/{merchant_id}/urls/{url_id}")
        self._raise_for_status(resp, f"Disabling URL {url_id} on merchant {merchant_id}")


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class UrlRecord:
    url_id: int
    url: str
    key: str
    raw: dict = field(default_factory=dict)


@dataclass
class Plan:
    to_add: List[str]
    to_disable: List[UrlRecord]
    unchanged: List[UrlRecord]

    @property
    def is_noop(self) -> bool:
        return not self.to_add and not self.to_disable


def url_is_active(item: dict) -> bool:
    """
    The documented URL object has no status field; the list endpoint returns
    the merchant's URLs. Be defensive in case a status style field is present.
    """
    if item.get("potential") is True:
        return False
    for key in ("disabled", "deleted", "isDisabled"):
        if item.get(key) is True:
            return False
    for key in ("enabled", "active", "isActive"):
        if item.get(key) is False:
            return False
    status = item.get("status")
    if isinstance(status, str) and status.strip().lower() in ("disabled", "inactive", "deleted"):
        return False
    return True


def build_plan(desired: Sequence[str], existing: Sequence[dict]) -> Plan:
    desired_by_key: Dict[str, str] = {}
    for url in desired:
        desired_by_key.setdefault(normalize_url(url), url.strip())

    existing_records: List[UrlRecord] = []
    for item in existing:
        if not url_is_active(item):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        try:
            url_id = int(item.get("urlId"))
        except (TypeError, ValueError):
            log.warning("Skipping URL without a usable urlId: %r", item)
            continue
        existing_records.append(UrlRecord(url_id=url_id, url=url, key=normalize_url(url), raw=item))

    existing_keys = {rec.key for rec in existing_records}
    to_add = [url for key, url in desired_by_key.items() if key not in existing_keys]
    to_disable = [rec for rec in existing_records if rec.key not in desired_by_key]
    unchanged = [rec for rec in existing_records if rec.key in desired_by_key]
    return Plan(to_add=to_add, to_disable=to_disable, unchanged=unchanged)


# --------------------------------------------------------------------------
# Console helpers
# --------------------------------------------------------------------------


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{prompt} [auto-confirmed with --yes]")
        return True
    if not sys.stdin.isatty():
        print(f"{prompt}\nNo interactive terminal available; re-run with --yes to confirm.", file=sys.stderr)
        return False
    while True:
        try:
            answer = input(f"{prompt} [y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer y or n.")


def fmt(value) -> str:
    return "" if value is None else str(value)


def print_table(rows: List[List[str]], headers: List[str]) -> None:
    if not rows:
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def print_merchant(m: dict, url_count: Optional[int] = None) -> None:
    location = ", ".join(
        part for part in (fmt(m.get("city")), fmt(m.get("state")), fmt(m.get("country"))) if part
    )
    print("Merchant details")
    print(f"  merchantId : {fmt(m.get('merchantId'))}")
    print(f"  sponsorId  : {fmt(m.get('sponsorId'))}")
    print(f"  id         : {fmt(m.get('id'))}")
    print(f"  name       : {fmt(m.get('name'))}")
    print(f"  dba        : {fmt(m.get('dba'))}")
    print(f"  mid        : {fmt(m.get('mid'))}")
    print(f"  location   : {location}")
    print(f"  status     : {fmt(m.get('status'))}")
    if url_count is not None:
        print(f"  active URLs: {url_count}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_sponsors(client: WrmClient, args: argparse.Namespace) -> int:
    sponsors = client.list_sponsors(args.sponsor_id)
    if not sponsors:
        print("No sponsors returned.")
        return EXIT_OK
    rows = [
        [fmt(s.get("sponsorId")), fmt(s.get("parentSponsorId")), fmt(s.get("name"))]
        for s in sponsors
    ]
    print_table(rows, ["sponsorId", "parentSponsorId", "name"])
    return EXIT_OK


def merchant_matches(m: dict, needle: str) -> bool:
    haystack = " ".join(
        fmt(m.get(k)) for k in ("name", "dba", "mid", "id", "merchantId", "email", "city")
    ).lower()
    return needle in haystack


def cmd_search(client: WrmClient, args: argparse.Namespace) -> int:
    needle = (args.term or "").strip().lower()
    if not needle and not args.all:
        print("Provide a search term, or use --all to list every merchant.", file=sys.stderr)
        return EXIT_BAD_INPUT

    matches: List[dict] = []
    scanned = 0
    for m in client.iter_merchants(sponsor_id=args.sponsor_id):
        scanned += 1
        if args.active_only and fmt(m.get("status")).lower() != "active":
            continue
        if args.all or merchant_matches(m, needle):
            matches.append(m)
        if args.limit and len(matches) >= args.limit:
            break

    if not matches:
        print(f"No merchants matched {args.term!r} (scanned {scanned}).")
        return EXIT_OK

    rows = [
        [
            fmt(m.get("merchantId")),
            fmt(m.get("sponsorId")),
            fmt(m.get("mid")),
            fmt(m.get("name")),
            fmt(m.get("dba")),
            fmt(m.get("status")),
        ]
        for m in matches
    ]
    print_table(rows, ["merchantId", "sponsorId", "mid", "name", "dba", "status"])
    print(f"\n{len(matches)} merchant(s) shown, {scanned} scanned.")
    print("Use the merchantId column with the sync command.")
    return EXIT_OK


def chunked(seq: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def cmd_sync(client: WrmClient, args: argparse.Namespace) -> int:
    # 1. Read and validate the input file before touching the API.
    raw_urls = read_url_file(args.url_file)
    if not raw_urls and not args.allow_empty:
        print(
            f"The URL file {args.url_file!r} contains no URLs. Refusing to disable every URL "
            "for the merchant. Pass --allow-empty if that is really what you want.",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT

    valid: List[str] = []
    invalid: List[Tuple[str, str]] = []
    seen_keys: Dict[str, str] = {}
    duplicates: List[Tuple[str, str]] = []
    for url in raw_urls:
        ok, reason = validate_url(url)
        if not ok:
            invalid.append((url, reason))
            continue
        key = normalize_url(url)
        if key in seen_keys:
            duplicates.append((url, seen_keys[key]))
            continue
        seen_keys[key] = url.strip()
        valid.append(url.strip())

    print(f"Read {len(raw_urls)} entries from {args.url_file}: {len(valid)} valid, "
          f"{len(invalid)} invalid, {len(duplicates)} duplicate.")
    if duplicates:
        print("Duplicates (ignored):")
        for dup, first in duplicates:
            print(f"  {dup}  (same as {first})")
    if invalid:
        print("Invalid URLs:")
        for bad, reason in invalid:
            print(f"  {bad}  ->  {reason}")
        if not args.skip_invalid:
            print(
                "\nAborting because the file contains invalid URLs. Fix them, or re-run with "
                "--skip-invalid to proceed with only the valid ones.",
                file=sys.stderr,
            )
            return EXIT_BAD_INPUT
        print("Continuing without the invalid URLs (--skip-invalid).")

    if not valid and not args.allow_empty:
        print("No valid URLs remain; refusing to continue.", file=sys.stderr)
        return EXIT_BAD_INPUT

    # 2. Look up the merchant and ask the user to confirm it.
    client.login()
    merchant = client.get_merchant(args.merchant_id)
    existing = client.list_urls(args.merchant_id, potential=False)
    active_existing = [u for u in existing if url_is_active(u)]

    print()
    print_merchant(merchant, url_count=len(active_existing))
    print()
    if not confirm("Is this the correct merchant?", args.yes):
        print("Aborted; no changes were made.")
        return EXIT_ABORTED

    # 3. Build and display the plan.
    plan = build_plan(valid, existing)
    print()
    print("Plan")
    print(f"  URLs already present and kept : {len(plan.unchanged)}")
    print(f"  URLs to add                   : {len(plan.to_add)}")
    print(f"  URLs to disable               : {len(plan.to_disable)}")
    if plan.to_add:
        print("\n  Add:")
        for url in plan.to_add:
            print(f"    + {url}")
    if plan.to_disable:
        print("\n  Disable:")
        for rec in plan.to_disable:
            print(f"    - {rec.url}  (urlId {rec.url_id})")
    if args.verbose and plan.unchanged:
        print("\n  Keep:")
        for rec in plan.unchanged:
            print(f"    = {rec.url}  (urlId {rec.url_id})")

    if plan.is_noop:
        print("\nNothing to do; the merchant's active URLs already match the file.")
        return EXIT_OK

    if args.dry_run:
        print("\nDry run; no changes were made.")
        return EXIT_OK

    print()
    if not confirm(
        f"Apply {len(plan.to_add)} add(s) and {len(plan.to_disable)} disable(s) to merchant "
        f"{args.merchant_id}?",
        args.yes,
    ):
        print("Aborted; no changes were made.")
        return EXIT_ABORTED

    # 4. Apply: add first so the merchant is never left with fewer URLs than intended.
    failures: List[str] = []
    added = 0
    if plan.to_add:
        print("\nAdding URLs...")
        for batch in chunked(plan.to_add, max(1, args.batch_size)):
            try:
                client.add_urls(args.merchant_id, batch, boarding_scan=args.boarding_scan)
                for url in batch:
                    print(f"  + {url}")
                added += len(batch)
            except WrmApiError as exc:
                if len(batch) == 1:
                    print(f"  ! {batch[0]}  ->  {exc}")
                    failures.append(f"add {batch[0]}: {exc}")
                    continue
                # Retry the batch one URL at a time so we can report exactly which failed.
                log.warning("Batch add failed (%s); retrying URLs individually", exc)
                for url in batch:
                    try:
                        client.add_urls(args.merchant_id, [url], boarding_scan=args.boarding_scan)
                        print(f"  + {url}")
                        added += 1
                    except WrmApiError as exc_one:
                        print(f"  ! {url}  ->  {exc_one}")
                        failures.append(f"add {url}: {exc_one}")

    disabled = 0
    if plan.to_disable:
        print("\nDisabling URLs...")
        for rec in plan.to_disable:
            try:
                client.disable_url(args.merchant_id, rec.url_id)
                print(f"  - {rec.url}  (urlId {rec.url_id})")
                disabled += 1
            except WrmApiError as exc:
                print(f"  ! {rec.url}  (urlId {rec.url_id})  ->  {exc}")
                failures.append(f"disable {rec.url} (urlId {rec.url_id}): {exc}")

    # 5. Verify the end state.
    print("\nVerifying...")
    after = client.list_urls(args.merchant_id, potential=False)
    final_plan = build_plan(valid, after)
    print(f"  Added {added}, disabled {disabled}, failed {len(failures)}.")
    print(f"  Merchant now has {len(final_plan.unchanged)} active URL(s) matching the file.")
    if final_plan.is_noop and not failures:
        print("Success: the merchant's active URLs match the input file.")
        return EXIT_OK

    if final_plan.to_add:
        print(f"  Still missing ({len(final_plan.to_add)}):")
        for url in final_plan.to_add:
            print(f"    {url}")
    if final_plan.to_disable:
        print(f"  Still active but not in file ({len(final_plan.to_disable)}):")
        for rec in final_plan.to_disable:
            print(f"    {rec.url}  (urlId {rec.url_id})")
    if failures:
        print("\nErrors:")
        for f in failures:
            print(f"  {f}")
    print("\nCompleted with differences. Review the messages above and re-run to retry.")
    return EXIT_ERROR


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wrm_url_merge.py",
        description=(
            "Make a WRM merchant's active URLs match a text file of URLs. "
            "Credentials come from WRM_USERNAME / WRM_PASSWORD (environment or .env); "
            "WRM_API_URL overrides the API host."
        ),
        epilog=(
            "Examples:\n"
            "  python wrm_url_merge.py search \"Acme\"\n"
            "  python wrm_url_merge.py sync 254 urls.txt --dry-run\n"
            "  python wrm_url_merge.py sync 254 urls.txt\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sponsors = sub.add_parser("sponsors", help="list sponsors visible to your user")
    p_sponsors.add_argument("--sponsor-id", type=int, help="use this sponsor as the parent")
    p_sponsors.set_defaults(func=cmd_sponsors)

    p_search = sub.add_parser("search", help="find merchants and show their merchantId")
    p_search.add_argument("term", nargs="?", help="text to match against name, dba, mid, id, email, city")
    p_search.add_argument("--sponsor-id", type=int, help="only merchants under this sponsor")
    p_search.add_argument("--all", action="store_true", help="list every merchant instead of filtering")
    p_search.add_argument("--active-only", action="store_true", help="hide merchants that are not Active")
    p_search.add_argument("--limit", type=int, default=0, help="stop after this many matches")
    p_search.set_defaults(func=cmd_search)

    p_sync = sub.add_parser("sync", help="merge a URL file into a merchant")
    p_sync.add_argument("merchant_id", type=int, help="WRM merchantId (from the search command)")
    p_sync.add_argument("url_file", help="text file with one URL per line")
    p_sync.add_argument("--dry-run", action="store_true", help="show the plan and make no changes")
    p_sync.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    p_sync.add_argument("--skip-invalid", action="store_true", help="continue even if the file has invalid URLs")
    p_sync.add_argument("--allow-empty", action="store_true", help="allow an empty file (disables all URLs)")
    p_sync.add_argument("--boarding-scan", action="store_true", help="request a boarding scan for added URLs")
    p_sync.add_argument("--batch-size", type=int, default=25, help="URLs per add request (default 25)")
    p_sync.set_defaults(func=cmd_sync)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = Config.from_env()
    except WrmError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if config.api_url != DEFAULT_API_URL:
        print(f"Using API URL: {config.api_url}")

    client = WrmClient(config)
    try:
        if args.command != "sync":
            client.login()
        return args.func(client, args)
    except WrmAuthError as exc:
        print(f"Authentication error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except WrmApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except WrmError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_ABORTED


if __name__ == "__main__":
    sys.exit(main())
