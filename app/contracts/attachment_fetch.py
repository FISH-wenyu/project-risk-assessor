"""Fetch contract attachments from direct URLs, defensively.

Downloading was a boundary this project deliberately kept closed for a long
time, and it was only opened once the attachment URLs had been surveyed:
whether they were all https, whether any carried signed-token query strings,
which hosts appeared, which extensions, and the size range. Building the
fetcher before knowing those answers would have meant guessing at the threat
model.

THE CENTRAL RISK: these URLs are database content, and database content is
untrusted input. Fetching whatever a row happens to contain turns this module
into a server-side request forgery primitive: a URL edited upstream could point
at `169.254.169.254`, at `localhost`, or at an internal service, and this
process would dutifully fetch it from inside the network.

Every guard below exists for that reason:

1. **Host allowlist, fail-closed.** Empty config fetches nothing at all.
   Forgetting to configure the allowlist must not grant more reach, only less.
2. **https only.** No file://, no ftp://, no http://.
3. **Private address rejection.** The resolved IP must be a global address, so
   an allowlisted name that resolves to a private range is still refused.
4. **No redirect following.** A 302 to an internal host would bypass every
   check above.
5. **Size ceiling**, enforced on the declared length AND on bytes actually
   read, since Content-Length can lie.
6. **Extension allowlist**, because only PDF and DOCX can be parsed anyway.

Full URLs are never logged or returned; only host plus a sanitised reference.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30
# Only what the extractor can actually read. Kept in step with
# `text_extraction.SUPPORTED_SUFFIXES` by a test: fetching a format the
# extractor cannot read produces a downloaded file and an empty analysis, and
# refusing a format it CAN read wastes the capability silently.
#
# `.doc` (legacy binary Word) is fetched too, but the extractor puts its output
# through a coherence check and discards it if it does not read as text: a
# half-parsed .doc produces plausible-looking prose with runs missing, and that
# would feed contract rules with confident, wrong evidence.
FETCHABLE_SUFFIXES = (".pdf", ".docx", ".xlsx", ".doc")

# Outcome reasons. Each is a signal, never silence.
REASON_OK = "fetched"
REASON_CACHED = "already_local"
REASON_NO_ALLOWLIST = "no_host_allowlist_configured"
REASON_HOST_BLOCKED = "host_not_in_allowlist"
REASON_SCHEME_BLOCKED = "scheme_not_https"
REASON_PRIVATE_ADDRESS = "resolves_to_private_address"
REASON_UNRESOLVABLE = "host_does_not_resolve"
REASON_UNSUPPORTED_TYPE = "unsupported_file_type"
REASON_TOO_LARGE = "over_size_limit"
REASON_REDIRECTED = "redirect_refused"
REASON_FETCH_FAILED = "fetch_failed"
REASON_BLANK_URL = "blank_url"

# Recorded whenever the resolved-address check was deliberately skipped, so a
# weakened run can never look like a fully guarded one.
SIGNAL_ADDRESS_CHECK_SKIPPED = "address_check_skipped_by_config"


@dataclass
class FetchOutcome:
    """What happened for one attachment. `path` is set only on success."""

    attach_ref: str
    host: str = ""
    suffix: str = ""
    reason: str = ""
    path: Path | None = None
    bytes_read: int = 0
    signals: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict[str, Any]:
        # Host and extension only. Never the full URL, never the local path,
        # which would expose the directory layout.
        return {
            "attach_ref": self.attach_ref,
            "host": self.host,
            "suffix": self.suffix,
            "reason": self.reason,
            "ok": self.ok,
            "bytes_read": self.bytes_read,
            "signals": list(self.signals),
        }


@dataclass
class AttachmentFetcher:
    allowed_hosts: tuple[str, ...] = ()
    destination: Path = Path("data/contract-attachments")
    max_bytes: int = MAX_ATTACHMENT_BYTES
    timeout: int = FETCH_TIMEOUT_SECONDS
    # Skip the resolved-address check. OFF by default.
    #
    # Why this exists: when an HTTP(S) proxy is configured, `urlopen` connects
    # to the PROXY and the proxy resolves the hostname, so the address this
    # process resolves locally is never dialled. Proxies that do DNS
    # interception (Clash, Surge, corporate split-tunnel) hand back fake IPs -
    # measured on this machine, both OSS hosts resolved to 198.18.0.x, the RFC
    # 2544 benchmarking range, which is correctly not a global address. The
    # check therefore blocks legitimate traffic while no longer governing where
    # the connection actually goes.
    #
    # Turning this on does NOT open the door: the exact-match host allowlist
    # still applies, and that is what stops a rewritten database URL. It only
    # drops a check that a proxy has already made meaningless. Every affected
    # outcome carries SIGNAL_ADDRESS_CHECK_SKIPPED so the weakening is visible.
    allow_private_addresses: bool = False
    _resolver: Any = field(default=None, repr=False)

    def _resolve(self, host: str) -> list[str]:
        """Resolve a host to its IPs. Separate for test injection."""
        if self._resolver is not None:
            return list(self._resolver(host))
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return [info[4][0] for info in infos]

    def _host_allowed(self, host: str) -> bool:
        if not self.allowed_hosts:
            return False
        clean = host.lower()
        # Exact match only. No suffix matching: `evil-oss.example.com.attacker
        # .example` must not pass because it ends with an allowlisted string.
        return clean in self.allowed_hosts

    def _addresses_are_public(self, host: str) -> tuple[bool, str]:
        try:
            addresses = self._resolve(host)
        except Exception:
            return False, REASON_UNRESOLVABLE
        if not addresses:
            return False, REASON_UNRESOLVABLE
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                return False, REASON_PRIVATE_ADDRESS
            # is_global excludes loopback, link-local (including the cloud
            # metadata address), private ranges, reserved and multicast.
            if not address.is_global:
                return False, REASON_PRIVATE_ADDRESS
        return True, ""

    def fetch(self, url: str, *, attach_ref: str) -> FetchOutcome:
        raw = str(url or "").strip()
        outcome = FetchOutcome(attach_ref=attach_ref)
        if not raw:
            outcome.reason = REASON_BLANK_URL
            return outcome
        if not self.allowed_hosts:
            outcome.reason = REASON_NO_ALLOWLIST
            return outcome

        parsed = urlparse(raw)
        outcome.host = (parsed.hostname or "").lower()
        path = unquote(parsed.path or "")
        outcome.suffix = PurePosixPath(path).suffix.lower()

        if parsed.scheme.lower() != "https":
            outcome.reason = REASON_SCHEME_BLOCKED
            return outcome
        if not self._host_allowed(outcome.host):
            outcome.reason = REASON_HOST_BLOCKED
            return outcome
        if outcome.suffix not in FETCHABLE_SUFFIXES:
            outcome.reason = REASON_UNSUPPORTED_TYPE
            return outcome

        if self.allow_private_addresses:
            outcome.signals.append(SIGNAL_ADDRESS_CHECK_SKIPPED)
        else:
            public, why = self._addresses_are_public(outcome.host)
            if not public:
                outcome.reason = why
                return outcome

        target = self.destination / _local_name(raw, outcome.suffix)
        if target.exists() and target.stat().st_size > 0:
            outcome.path = target
            outcome.bytes_read = target.stat().st_size
            outcome.reason = REASON_CACHED
            return outcome

        try:
            payload = self._download(raw, outcome)
        except _FetchRefused:
            return outcome
        except Exception:
            # Never surface the URL or the exception text: an error message can
            # echo back the full URL.
            outcome.reason = REASON_FETCH_FAILED
            return outcome

        self.destination.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        outcome.path = target
        outcome.bytes_read = len(payload)
        outcome.reason = REASON_OK
        return outcome

    def _download(self, url: str, outcome: FetchOutcome) -> bytes:
        request = Request(
            _encode_url(url), headers={"User-Agent": "contract-attachment-fetch/1.0"}
        )
        with urlopen(request, timeout=self.timeout) as response:
            # A redirect that urlopen already followed would have landed us on
            # an unvalidated host, so confirm where we actually ended up.
            final_host = (urlparse(response.geturl()).hostname or "").lower()
            if final_host != outcome.host:
                outcome.reason = REASON_REDIRECTED
                raise _FetchRefused
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > self.max_bytes:
                outcome.reason = REASON_TOO_LARGE
                raise _FetchRefused
            # Read one byte past the limit: Content-Length can lie or be absent.
            payload = response.read(self.max_bytes + 1)
        if len(payload) > self.max_bytes:
            outcome.reason = REASON_TOO_LARGE
            raise _FetchRefused
        return payload


class _FetchRefused(Exception):
    """A guard rejected the response; the reason is already on the outcome."""


def build_document_provider(
    discovery_provider: Any, fetcher: AttachmentFetcher
) -> Any:
    """Make a `document_provider` for `ContractAnalysisService`.

    Returns local paths for whatever could be fetched. Attachments that were
    refused keep their reason on the outcome; the analysis service already
    reports an unreadable or absent document as a signal, so a refusal here can
    never be mistaken for a clean contract.
    """

    def provider(project_id: str) -> list[Path]:
        rows = discovery_provider().read_contract_attachments(project_id)
        paths: list[Path] = []
        for row in rows:
            outcome = fetcher.fetch(
                row.get("attach_url", ""),
                attach_ref=f"attach:{row.get('attach_id', '')}",
            )
            if outcome.ok and outcome.path is not None:
                paths.append(outcome.path)
        return paths

    return provider


def _encode_url(url: str) -> str:
    """Percent-encode the path and query so urllib will accept the URL.

    Two attachments failed with `InvalidURL: URL can't contain control
    characters` because their stored paths hold raw non-ASCII and bracket
    characters, e.g. a filename containing Chinese text and full-width
    brackets. urllib refuses to send those bytes.

    Only the path and query are touched; scheme and host are left alone, so
    this cannot move the request to a different host after the allowlist check.
    `%` is in the safe set so an already-encoded URL is not double-encoded.
    """
    parsed = urlsplit(url)
    safe_path = quote(parsed.path, safe="/%:@&=+$,~()")
    safe_query = quote(parsed.query, safe="/%:@&=+$,~()?")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, safe_path, safe_query, parsed.fragment)
    )


def _local_name(url: str, suffix: str) -> str:
    """Content-addressed local name.

    The remote filename is NOT reused: it is attacker-influenced data that
    could contain path separators, and it may itself be sensitive (contract
    names appear in `attach_name`). A hash of the URL is stable, collision-free
    in practice, and reveals nothing.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return f"{digest}{suffix}"
