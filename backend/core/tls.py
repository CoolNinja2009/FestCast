"""Self-signed HTTPS certificate for the local phone camera page.

Phone browsers only allow camera access on https pages. FestCast makes its own certificate (kept in data/tls/) for
this PC's LAN IP, its .local name and localhost; phones show a warning once and the operator taps "proceed".
The certificate is remade when the LAN IP or name changes, or when it nears expiry.

iPhones refuse a certificate outright (no "proceed" button) unless it lists serverAuth in ExtendedKeyUsage and is
valid for at most 825 days (Apple, "Requirements for trusted certificates in iOS 13"), even a self-signed one.
We stay under 398 days, the stricter limit, to be safe."""
from __future__ import annotations

import datetime as dt
import ipaddress
import logging
from pathlib import Path

log = logging.getLogger("festcast.tls")


def ensure_cert(data_dir: Path, ips: list[str], names: list[str]) -> tuple[str, str] | None:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        log.warning("python package 'cryptography' missing: local camera page (https) disabled. "
                    "Run .venv\\Scripts\\python -m pip install -r requirements.txt")
        return None
    d = data_dir / "tls"
    d.mkdir(parents=True, exist_ok=True)
    crt, key = d / "festcast.crt", d / "festcast.key"
    ips = sorted({i for i in [*ips, "127.0.0.1"] if i})
    names = sorted({n for n in [*names, "localhost"] if n})
    want = {*ips, *names}
    now = dt.datetime.now(dt.timezone.utc)
    if crt.exists() and key.exists():
        try:
            cert = x509.load_pem_x509_certificate(crt.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have = {str(v) for v in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            ios_ok = (ExtendedKeyUsageOID.SERVER_AUTH in eku
                      and cert.not_valid_after_utc - cert.not_valid_before_utc <= dt.timedelta(days=398))
            if ios_ok and want <= have and cert.not_valid_after_utc - now > dt.timedelta(days=30):
                return str(crt), str(key)
        except Exception:  # noqa: BLE001
            pass
    k = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FestCast camera page")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(k.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=396))
            .add_extension(x509.SubjectAlternativeName([*(x509.IPAddress(ipaddress.ip_address(i)) for i in ips),
                                                        *(x509.DNSName(n) for n in names)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(k, hashes.SHA256()))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("made a new self-signed certificate for %s", ", ".join(sorted(want)))
    return str(crt), str(key)
