"""
Самоподписанный сертификат для HTTPS в локальной сети.
Нужен потому, что браузер телефона разрешает камеру только на https://.
Создаётся автоматически в папке certs/ и пересоздаётся, если сменился IP компьютера.
"""
import datetime
import ipaddress
import shutil
import subprocess
from pathlib import Path

CERT_DIR = Path(__file__).parent / "certs"


def ensure_cert(ip):
    """Вернёт (cert_path, key_path) или None, если создать сертификат не удалось."""
    CERT_DIR.mkdir(exist_ok=True)
    cert, key, meta = CERT_DIR / "sma.crt", CERT_DIR / "sma.key", CERT_DIR / "ip.txt"
    if cert.exists() and key.exists() and meta.exists() and meta.read_text().strip() == ip:
        return str(cert), str(key)
    try:
        _make_with_cryptography(ip, cert, key)
    except Exception:
        try:
            _make_with_openssl(ip, cert, key)
        except Exception:
            return None
    meta.write_text(ip)
    return str(cert), str(key)


def _make_with_cryptography(ip, cert, key):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SMA Core")])
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip != "127.0.0.1":
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=825))
         .add_extension(x509.SubjectAlternativeName(sans), critical=False)
         .sign(k, hashes.SHA256()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))


def _make_with_openssl(ip, cert, key):
    if not shutil.which("openssl"):
        raise RuntimeError("openssl не найден")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=SMA Core",
         "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{ip}"],
        check=True, capture_output=True)
