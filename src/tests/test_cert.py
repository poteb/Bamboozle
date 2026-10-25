import datetime

import pytest

from bamboozle.discovery import cert_common_name

x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding  # noqa: E402


def _make_cert(subject_cn: str | None, issuer_cn: str = "BBL CA") -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject_attrs = [x509.NameAttribute(NameOID.COUNTRY_NAME, "CN")]
    if subject_cn is not None:
        subject_attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, subject_cn))
    issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BBL Technologies Co., Ltd"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(subject_attrs))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(Encoding.DER)


def test_returns_subject_common_name_not_issuer():
    assert cert_common_name(_make_cert("01P00C511100150")) == "01P00C511100150"


def test_returns_none_when_subject_has_no_common_name():
    assert cert_common_name(_make_cert(None)) is None


def test_returns_none_for_garbage():
    assert cert_common_name(b"") is None
    assert cert_common_name(b"\x30\x82\xff\xff\x00") is None
