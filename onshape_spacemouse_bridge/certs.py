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


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _paths(cert_dir: Path) -> dict:
    return {
        "ca_cert": cert_dir / "ca.pem",
        "ca_key": cert_dir / "ca-key.pem",
        "leaf_cert": cert_dir / "leaf.pem",
        "leaf_key": cert_dir / "leaf-key.pem",
        "fullchain": cert_dir / "fullchain.pem",
    }


def generate(cert_dir: Path = DEFAULT_DIR, ca_days: int = 3650, leaf_days: int = 825) -> dict:
    """Generate a fresh CA + leaf pair, overwriting any existing files."""
    cert_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.datetime.now(datetime.timezone.utc)

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
        .sign(ca_key, hashes.SHA256())
    )

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
        .sign(ca_key, hashes.SHA256())
    )

    p = _paths(cert_dir)
    p["ca_cert"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    _write_private(p["ca_key"], ca_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    p["leaf_cert"].write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    _write_private(p["leaf_key"], leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    p["fullchain"].write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    return p


def leaf_expires_soon(cert_dir: Path = DEFAULT_DIR, within_days: int = 30) -> bool:
    leaf_path = cert_dir / "leaf.pem"
    if not leaf_path.exists():
        return True
    cert = x509.load_pem_x509_certificate(leaf_path.read_bytes())
    remaining = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
    return remaining < datetime.timedelta(days=within_days)


def ensure(cert_dir: Path = DEFAULT_DIR) -> dict:
    """Generate certs if missing or the leaf is close to expiry; otherwise
    return the existing paths untouched.
    """
    have = {f.name for f in cert_dir.glob("*")} if cert_dir.exists() else set()
    if not set(_FILES).issubset(have) or leaf_expires_soon(cert_dir):
        return generate(cert_dir)
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


def is_trusted_chromium() -> bool:
    if not NSSDB.exists():
        return False
    result = subprocess.run(["certutil", "-L", "-d", f"sql:{NSSDB}"], capture_output=True, text=True)
    return CA_NAME in result.stdout


def browsers_running() -> list[str]:
    """Best-effort check so callers can warn before injecting trust."""
    running = []
    for name in ("brave", "chrome", "chromium"):
        r = subprocess.run(["pgrep", "-fi", name], capture_output=True)
        if r.returncode == 0:
            running.append(name)
    return running
