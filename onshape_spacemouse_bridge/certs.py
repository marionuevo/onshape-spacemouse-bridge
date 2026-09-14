"""Local CA + leaf certificate for wss://127.51.68.120:8181.

3dconnexion.js hardcodes this exact IP; a public CA will never issue a
certificate for a loopback address (CA/Browser Forum baseline requirements
forbid it), so a locally-installed trust anchor is unavoidable. This is
exactly why 3Dconnexion's own Windows/macOS installer generates a CA into
the system store.

We generate a CA (the thing that gets installed into the browser's trust
store) plus a distinct leaf signed by it, rather than one self-signed
certificate acting as its own anchor. Gecko requires a genuine anchor->leaf
chain with CA:FALSE on the end-entity to trust a server certificate at all;
Chrome happens to accept a bare self-signed leaf too, but this shape works
for both, so there's no reason to special-case it. Only Chromium/Brave trust
injection is wired up below -- Gecko's cert_override.txt mechanism is a
different, per-profile file and is left for later if ever needed.

Security note: the CA private key must never leave this machine. If it were
ever shipped or synced somewhere reachable, anyone able to read it could
forge a certificate for any site for a browser that trusts this CA.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOST_IP = "127.51.68.120"
CA_NAME = "OnShape SpaceMouse Bridge Local CA"

DEFAULT_DIR = Path.home() / ".local" / "share" / "onshape-spacemouse-bridge" / "certs"

# Chromium-family browsers (Chrome, Brave, ...) share this NSS database on Linux.
NSSDB = Path.home() / ".pki" / "nssdb"

_FILES = ("ca.pem", "ca-key.pem", "leaf.pem", "leaf-key.pem", "fullchain.pem")

log = logging.getLogger("certs")


def _write_private(path: Path, data: bytes) -> None:
    """Create the file already private -- writing first and chmod'ing after
    leaves the key readable at whatever the umask allows for the window in
    between.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # in case it already existed with looser bits


def _paths(cert_dir: Path) -> dict:
    return {
        "ca_cert": cert_dir / "ca.pem",
        "ca_key": cert_dir / "ca-key.pem",
        "leaf_cert": cert_dir / "leaf.pem",
        "leaf_key": cert_dir / "leaf-key.pem",
        "fullchain": cert_dir / "fullchain.pem",
    }


def _make_ca(now: datetime.datetime, ca_days: int):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=ca_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        # RFC 5280 wants a key identifier on a CA, and it is not decorative:
        # OpenSSL 3.5 (Python 3.14) fails the chain outright with "Missing
        # Authority Key Identifier" without the matching pair below.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return ca_key, ca_cert


def _make_leaf(now: datetime.datetime, leaf_days: int, ca_key, ca_cert):
    ca_name = ca_cert.subject
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST_IP)])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=leaf_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST_IP))]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return leaf_key, leaf_cert


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def generate(cert_dir: Path = DEFAULT_DIR, ca_days: int = 3650, leaf_days: int = 825) -> dict:
    """Generate a fresh CA *and* leaf, overwriting any existing files.

    This invalidates the trust already installed in the browser: the new CA
    is a different anchor, so `trust` has to be re-run. Renewals should call
    `renew_leaf` instead -- see `ensure`.
    """
    cert_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, ca_cert = _make_ca(now, ca_days)
    leaf_key, leaf_cert = _make_leaf(now, leaf_days, ca_key, ca_cert)
    return _write_all(cert_dir, ca_key, ca_cert, leaf_key, leaf_cert)


def renew_leaf(cert_dir: Path = DEFAULT_DIR, leaf_days: int = 825) -> dict:
    """Issue a fresh leaf from the *existing* CA, leaving the anchor alone.

    This is the whole point of having a separate CA and leaf. Regenerating
    both at renewal time silently breaks the install: the CA sitting in
    ~/.pki/nssdb no longer signs the chain being served, so the browser
    starts refusing the connection -- and since the old and new CA share a
    subject name, a name-only trust check still cheerfully reports
    everything as fine.
    """
    p = _paths(cert_dir)
    ca_cert = x509.load_pem_x509_certificate(p["ca_cert"].read_bytes())
    ca_key = serialization.load_pem_private_key(p["ca_key"].read_bytes(), password=None)
    now = datetime.datetime.now(datetime.timezone.utc)
    leaf_key, leaf_cert = _make_leaf(now, leaf_days, ca_key, ca_cert)
    return _write_all(cert_dir, ca_key, ca_cert, leaf_key, leaf_cert)


def _write_all(cert_dir: Path, ca_key, ca_cert, leaf_key, leaf_cert) -> dict:
    cert_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = _paths(cert_dir)
    p["ca_cert"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    _write_private(p["ca_key"], _pem(ca_key))
    p["leaf_cert"].write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    _write_private(p["leaf_key"], _pem(leaf_key))
    p["fullchain"].write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    return p


def _expires_soon(path: Path, within_days: int) -> bool:
    if not path.exists():
        return True
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except ValueError:
        return True
    remaining = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
    return remaining < datetime.timedelta(days=within_days)


def leaf_expires_soon(cert_dir: Path = DEFAULT_DIR, within_days: int = 30) -> bool:
    return _expires_soon(cert_dir / "leaf.pem", within_days)


def ca_expires_soon(cert_dir: Path = DEFAULT_DIR, within_days: int = 30) -> bool:
    return _expires_soon(cert_dir / "ca.pem", within_days)


def ensure(cert_dir: Path = DEFAULT_DIR) -> dict:
    """Make sure a usable CA + leaf exist, renewing as narrowly as possible.

    Renewing the leaf must NOT touch the CA. `serve` calls this on every
    start, so regenerating both once the leaf neared expiry -- which is what
    this used to do -- would, roughly two years in, silently replace the
    anchor sitting in the browser's trust store with one that no longer
    signs the served chain. The bridge would simply stop working, with a
    certificate error and a trust check that still said everything was fine.
    """
    have = {f.name for f in cert_dir.glob("*")} if cert_dir.exists() else set()
    if not set(_FILES).issubset(have):
        return generate(cert_dir)
    if ca_expires_soon(cert_dir):
        log.warning(
            "the local CA is expiring; regenerating it and the leaf -- "
            "re-run 'trust' and restart the browser afterwards"
        )
        return generate(cert_dir)
    if leaf_expires_soon(cert_dir):
        log.info("leaf certificate expiring; re-issuing it from the existing CA")
        return renew_leaf(cert_dir)
    return _paths(cert_dir)


# --- Chromium/Brave trust injection ----------------------------------------


def trust_chromium(cert_dir: Path = DEFAULT_DIR) -> None:
    """Install the CA into ~/.pki/nssdb. Browsers must be fully closed
    first -- NSS reads this store at startup, and a still-running browser
    process (Brave included) will not pick up the change.
    """
    ca_path = cert_dir / "ca.pem"
    if not ca_path.exists():
        raise FileNotFoundError(f"no CA at {ca_path}; run 'gen-certs' first")

    if not NSSDB.exists():
        NSSDB.mkdir(parents=True, exist_ok=True)
        subprocess.run(["certutil", "-N", "--empty-password", "-d", f"sql:{NSSDB}"], check=True)

    # Drop any earlier entry under the same name first -- certutil -A does
    # not overwrite, it would add a duplicate.
    subprocess.run(
        ["certutil", "-D", "-d", f"sql:{NSSDB}", "-n", CA_NAME],
        capture_output=True,
    )
    subprocess.run(
        ["certutil", "-A", "-d", f"sql:{NSSDB}", "-n", CA_NAME, "-t", "C,,", "-i", str(ca_path)],
        check=True,
    )


def installed_ca_der() -> Optional[bytes]:
    """The DER of the CA currently in the NSS store under our name, if any."""
    if not NSSDB.exists():
        return None
    r = subprocess.run(
        ["certutil", "-L", "-d", f"sql:{NSSDB}", "-n", CA_NAME, "-r"],
        capture_output=True,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def is_trusted_chromium(cert_dir: Path = DEFAULT_DIR) -> bool:
    """Whether the CA *on disk* is the one installed in the browser store.

    Checking only that something with the right nickname exists is not
    enough: a regenerated CA keeps the same subject name, so a name-only
    check reports success while the browser rejects every connection. When
    there is no local CA to compare against, fall back to the name check.
    """
    installed = installed_ca_der()
    if installed is None:
        return False
    ca_path = cert_dir / "ca.pem"
    if not ca_path.exists():
        return True
    try:
        local = x509.load_pem_x509_certificate(ca_path.read_bytes())
    except ValueError:
        return True
    return installed == local.public_bytes(serialization.Encoding.DER)


def browsers_running() -> list[str]:
    """Best-effort check so callers can warn before injecting trust."""
    running = []
    # Match process names exactly rather than -f (full command line): -f
    # matches anything that merely mentions the word, so a checkout path
    # containing "chrome", a --chrome flag, or chromedriver all counted as a
    # running browser and blocked `trust` for no reason.
    for name, procs in (
        ("brave", ("brave", "brave-browser")),
        ("chrome", ("chrome", "google-chrome")),
        ("chromium", ("chromium", "chromium-browser")),
    ):
        if any(subprocess.run(["pgrep", "-x", pr], capture_output=True).returncode == 0
               for pr in procs):
            running.append(name)
    return running
