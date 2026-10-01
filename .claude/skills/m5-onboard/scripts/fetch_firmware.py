"""Pull a UIFlow 2.0 firmware binary from M5Burner's manifest API.

The manifest endpoint returns the full catalog; we filter by device
family and flash size, then download the newest UIFlow 2.x release.
Binaries are cached under a per-user XDG cache dir (mode 0700) so
repeated runs don't re-download — and so another local user can't
pre-seed a malicious blob at the predictable cache key.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

MANIFEST_URL = "https://m5burner-api.m5stack.com/api/firmware"
BINARY_BASE = "https://m5burner.m5stack.com/firmware/"

# Allow-list for the manifest's `file` field, which gets interpolated into
# both a URL and a filesystem path. Everything we've ever seen from
# m5burner-api is 32 hex chars + ".bin", so this is plenty permissive.
# Disallowing slashes, dots-in-isolation, and URL-meaningful chars stops
# path traversal, URL smuggling, and CRLF header injection at the source.
# 256-char cap so a hostile manifest can't ship a multi-megabyte filename.
_FILE_FIELD_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


def _cache_dir() -> str:
    """Per-user firmware cache directory, mode 0700.

    Lives under XDG_CACHE_HOME (or ~/.cache as the fallback) instead of
    the system temp dir. Two reasons:

      1. /tmp on Linux is world-writable with the sticky bit. The cache
         filename is deterministically derived from a public manifest
         field, so before we owned the file, any other local user could
         have pre-seeded /tmp/uiflow2_<key>.bin with malicious bytes,
         which the cache-hit shortcut would then have flashed to the
         device. Per-user 0700 dir closes that vector.
      2. Cache survives reboots, which the system tmp dir does not — so
         repeated provisioning of multiple boards skips re-downloads.

    Created with mode 0700 if missing; tightened to 0700 on every call
    in case it pre-existed at looser perms (chmod is a no-op on
    Windows, which treats the bits as advisory).
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "m5-onboard")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _open_https(url: str, timeout: float = 30.0):
    """Open an HTTPS URL with verified TLS.

    There is no unverified fallback. We are flashing firmware to a device
    the user is about to plug into their machine; silently disabling
    cert verification on this path would let any on-path attacker swap
    in arbitrary firmware. If the system trust store is empty (common
    on macOS python.org installs), we try certifi as a second attempt
    and otherwise fail with a clear hint.

    Ladder:
      1. Default context. Works on Homebrew Python / Linux / macOS
         system Python with the OS trust store populated.
      2. certifi bundle if importable. Works if certifi was pulled in
         by any other pip install (very common).
      3. Hard fail with the Install-Certificates hint.
    """
    def _is_cert_error(exc: BaseException) -> bool:
        # urllib wraps the SSL error in URLError; inspect .reason to unwrap.
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, urllib.error.URLError) and isinstance(
            exc.reason, ssl.SSLCertVerificationError
        ):
            return True
        return False

    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except Exception as e:
        if not _is_cert_error(e):
            raise
    try:
        import certifi
    except ImportError:
        raise SystemExit(
            "TLS verification failed and certifi is not installed.\n"
            "Fix one of:\n"
            "  - macOS python.org install: run "
            "/Applications/Python\\ 3.x/Install\\ Certificates.command\n"
            "  - any platform: pip install --user certifi\n"
            "Refusing to fetch firmware over an unverified connection."
        )
    ctx = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(url, timeout=timeout, context=ctx)


# Map each supported variant to the exact (category, entry name, version
# suffix) tuple that identifies its firmware in the M5Burner manifest.
# version_suffix is matched against the `version` field of each published
# version — empty string means "any version, pick the latest stable".
#
# Schema of a manifest entry:
#   {"name": str, "category": str, "tags": [...],
#    "versions": [{"version": str, "file": "<opaque-cdn-key>.bin",
#                  "published_at": "...", "published": bool}]}
# The `file` value is an opaque object key on Aliyun OSS — NOT a content
# hash, despite the 32-hex-char shape. Integrity is verified at download
# time against the Content-MD5 header the CDN returns.
VARIANTS = {
    "basic-16mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-16MB",
    },
    "basic-4mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-4MB",
    },
    "fire": {
        "category": "core",
        "entry_name": "UIFlow2.0 Fire",
        "version_suffix": "",
    },
    "core2": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        # Core2 versions have no suffix; Tough versions end in -TOUGH.
        "version_suffix": "",
        "version_must_not": ("-TOUGH",),
    },
    "tough": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-TOUGH",
    },
    "cores3": {
        "category": "cores3",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
    },
    "cardputer": {
        "category": "cardputer",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
        # v2.4.3+ is built on ESP-IDF 5.5.x, whose I2S driver regression
        # leaves the PDM mic returning a constant -8 (espressif/esp-idf
        # #18621, m5stack/M5Cardputer#11). v2.4.2 is the newest build on
        # IDF 5.4.2. Drop the pin once an upstream fix ships.
        "pinned_version": "v2.4.2",
    },
    "cardputer-adv": {
        "category": "cardputer",
        "entry_name": "UIFlow2.0 Cardputer-Adv",
        "version_suffix": "",
    },
}


def fetch_manifest() -> list:
    with _open_https(MANIFEST_URL, timeout=30) as r:
        return json.loads(r.read().decode())


def _find_entry(manifest: list, spec: dict) -> dict:
    cat = spec["category"].lower()
    name = spec["entry_name"]
    for e in manifest:
        if (e.get("category") or "").lower() == cat and (e.get("name") or "") == name:
            return e
    seen = [
        e.get("name") for e in manifest
        if (e.get("category") or "").lower() == cat
    ]
    raise SystemExit(
        f"No manifest entry with category={cat!r} name={name!r}. "
        f"Seen in category: {seen}"
    )


def _fits_variant(ver: str, spec: dict) -> bool:
    """True if a version tag belongs to this hardware variant.

    Some manifest entries are shared between boards (Core2 and Tough,
    Basic 4MB and 16MB) and only the tag suffix tells them apart.
    """
    suffix = spec.get("version_suffix", "")
    if suffix:
        return ver.endswith(suffix)
    return not any(ver.endswith(bad) for bad in spec.get("version_must_not", ()))


def _pick_version(entry: dict, spec: dict) -> dict:
    """Pick the newest stable version matching the variant's suffix.

    Stable = version tag without rc/alpha/beta/hotfix. Falls back to
    the newest non-stable if nothing clean matches, so preview/RC
    releases are still flashable when that's all that exists.

    A variant's ``pinned_version`` wins over "newest". The
    ``M5_UIFLOW_VERSION`` env var overrides both: an exact tag (e.g.
    ``v2.5.3``) or ``latest`` to ignore the pin. An exact tag must still
    belong to the variant (a ``-TOUGH`` tag is refused for ``core2``).
    """
    wanted = os.environ.get("M5_UIFLOW_VERSION") or spec.get("pinned_version")
    if wanted and wanted != "latest":
        for v in entry.get("versions", []):
            if v.get("version") != wanted:
                continue
            if not _fits_variant(wanted, spec):
                raise SystemExit(
                    f"Version {wanted!r} in {entry.get('name')!r} is for a different "
                    f"board than this variant (suffix={spec.get('version_suffix', '')!r}, "
                    f"excluded={list(spec.get('version_must_not', ()))}). "
                    "Pick the matching --variant instead."
                )
            return v
        raise SystemExit(
            f"Version {wanted!r} not found for {entry.get('name')!r}. "
            f"Available: {[v.get('version') for v in entry.get('versions', []) if _fits_variant(v.get('version') or '', spec)]}"
        )

    suffix = spec.get("version_suffix", "")
    candidates = [
        v for v in entry.get("versions", [])
        if v.get("published") is not False
        and _fits_variant(v.get("version") or "", spec)
    ]
    if not candidates:
        raise SystemExit(
            f"No versions for {entry.get('name')!r} match suffix={suffix!r}. "
            f"Available: {[v.get('version') for v in entry.get('versions', [])]}"
        )
    stable = [
        v for v in candidates
        if not any(x in (v.get("version") or "").lower()
                   for x in ("rc", "alpha", "beta", "hotfix"))
    ]
    # Manifest order is chronological; last = newest.
    return (stable or candidates)[-1]


def pick_firmware(manifest: list, variant: str) -> tuple[dict, dict]:
    """Return (entry, version) for the chosen variant."""
    if variant not in VARIANTS:
        raise SystemExit(f"Unknown variant '{variant}'. Known: {list(VARIANTS)}")
    spec = VARIANTS[variant]
    entry = _find_entry(manifest, spec)
    version = _pick_version(entry, spec)
    return entry, version


def _md5_file(path: str) -> bytes:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


def download(entry: dict, version: dict, dest_dir: str | None = None) -> str:
    if dest_dir is None:
        dest_dir = _cache_dir()
    file_field = version.get("file")
    if not file_field:
        raise SystemExit(f"Manifest version has no `file` field: {version}")
    # Validate before the value flows into a URL or filesystem path. A
    # hostile or buggy manifest cannot make us reach an arbitrary URL,
    # write outside the cache dir, or inject CRLF into the request line.
    if not _FILE_FIELD_RE.match(file_field):
        raise SystemExit(
            f"Manifest `file` field {file_field!r} is not in the allowed "
            f"shape {_FILE_FIELD_RE.pattern}; refusing to use it in a URL "
            "or filesystem path."
        )
    # The `file` field may or may not include a .bin suffix depending
    # on when the entry was added; normalize both sides.
    url = BINARY_BASE + file_field + ("" if file_field.endswith(".bin") else ".bin")
    base = file_field[:-4] if file_field.endswith(".bin") else file_field
    dest = os.path.join(dest_dir, f"uiflow2_{base}.bin")
    # Belt-and-suspenders containment check: if the regex above were ever
    # loosened, this still catches anything that would write outside
    # dest_dir. realpath collapses any "." / ".." / symlink games.
    real_dest = os.path.realpath(dest)
    real_root = os.path.realpath(dest_dir) + os.sep
    if not real_dest.startswith(real_root):
        raise SystemExit(
            f"Refusing to write outside cache dir: dest={real_dest!r} "
            f"is not under {real_root!r}."
        )
    sidecar = dest + ".md5"

    # Cache-hit path: re-hash the cached binary and compare to the
    # sidecar we wrote at download time. The sidecar lives in a 0700
    # cache dir, so only this uid could have placed it there — an
    # attacker dropping a binary without a matching sidecar falls
    # straight through to the cache-miss path, which then runs the
    # live Content-MD5 check against the CDN. Any error here (missing
    # sidecar, malformed hex, hash mismatch) is treated as a cache
    # miss; we never raise from the hit path.
    if os.path.exists(dest) and os.path.exists(sidecar):
        try:
            with open(sidecar, "r") as f:
                expected_hex = f.read().strip()
            expected = bytes.fromhex(expected_hex)
            if len(expected) == 16 and _md5_file(dest) == expected:
                return dest
        except (OSError, ValueError):
            pass

    # Cache-miss path. Aliyun OSS sets Content-MD5 (base64'd MD5 of
    # the stored object) on every blob response. We stream-hash the
    # body and compare so that a storage-layer corruption or
    # manifest/binary drift is caught before we hand the bytes to
    # esptool.
    #
    # This is integrity-only. MD5 is broken for collision attacks, so
    # it is NOT a substitute for TLS — it complements the verified-TLS
    # connection enforced by _open_https(). A CDN that can rewrite
    # both bytes and headers in tandem is not stopped by this check;
    # pinned constants would be needed for that, and M5Stack does not
    # publish signed releases to pin against.
    tmp = dest + ".part"
    sidecar_tmp = sidecar + ".part"
    h = hashlib.md5()
    try:
        with _open_https(url, timeout=120) as r:
            expected_b64 = r.headers.get("Content-MD5")
            if not expected_b64:
                raise SystemExit(
                    f"CDN response for {url} did not include a Content-MD5 "
                    "header; refusing to install unverifiable firmware."
                )
            try:
                expected = base64.b64decode(expected_b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise SystemExit(
                    f"Malformed Content-MD5 header {expected_b64!r}: {e}"
                )
            if len(expected) != 16:
                raise SystemExit(
                    f"Content-MD5 wrong length ({len(expected)} bytes, "
                    f"want 16) for {url}"
                )
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
        if h.digest() != expected:
            raise SystemExit(
                f"MD5 mismatch on firmware download from {url}: "
                f"expected {expected.hex()}, got {h.hexdigest()}. "
                "Aborting; partial file removed."
            )
        # Atomic rename: the binary appears at its cache key only after
        # verification passes, and the sidecar appears only after the
        # binary is in place. A crash anywhere in this sequence leaves
        # a recoverable state (no half-verified blob, no orphan
        # sidecar pointing at a missing file).
        os.replace(tmp, dest)
        with open(sidecar_tmp, "w") as f:
            f.write(h.hexdigest() + "\n")
        os.replace(sidecar_tmp, sidecar)
    except BaseException:
        for path in (tmp, sidecar_tmp):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch UIFlow 2.0 firmware.")
    ap.add_argument(
        "--variant",
        required=True,
        choices=sorted(VARIANTS),
        help="Which device variant to fetch firmware for.",
    )
    ap.add_argument(
        "--dest",
        default=None,
        help=(
            "Cache directory. Default: $XDG_CACHE_HOME/m5-onboard/ "
            "(or ~/.cache/m5-onboard/), created at mode 0700 if missing. "
            "Override only if you know you need a different location — "
            "we don't tighten permissions on a path you name explicitly."
        ),
    )
    args = ap.parse_args()

    manifest = fetch_manifest()
    entry, version = pick_firmware(manifest, args.variant)
    path = download(entry, version, args.dest)
    sys.stderr.write(
        f"Picked: {entry.get('name', '?')} "
        f"version={version.get('version', '?')} "
        f"({version.get('published_at', '?')})\n"
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
