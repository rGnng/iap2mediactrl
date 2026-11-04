#!/usr/bin/env python3
"""Local MFi 3.0 P-256 certificate provider for the development identity."""
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import pkcs7


class LocalMfiProvider:
    def __init__(self, cert_path, key_path):
        self.certificate = Path(cert_path).read_bytes()
        key_blob = Path(key_path).read_bytes()

        certs = pkcs7.load_der_pkcs7_certificates(self.certificate)
        if not certs:
            raise ValueError("certificate bundle contains no certificates")
        self.leaf = certs[0]
        self.public_key = self.leaf.public_key()
        if not isinstance(self.public_key, ec.EllipticCurvePublicKey):
            raise ValueError("certificate public key is not EC")
        if self.public_key.curve.name != "secp256r1":
            raise ValueError(f"unexpected curve {self.public_key.curve.name}")

        self.private_key = self._load_key(key_blob)
        self._verify_key_matches_certificate()

    @staticmethod
    def _load_key(blob):
        try:
            return serialization.load_der_private_key(blob, password=None)
        except Exception:  # noqa: BLE001
            pass
        try:
            return serialization.load_pem_private_key(blob, password=None)
        except Exception:  # noqa: BLE001
            pass
        if len(blob) == 32:
            return ec.derive_private_key(
                int.from_bytes(blob, "big"), ec.SECP256R1()
            )
        raise ValueError("unsupported private key format")

    def _verify_key_matches_certificate(self):
        digest = hashlib.sha256(b"mediactrl-key-check").digest()
        der = self.private_key.sign(
            digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))
        )
        self.public_key.verify(
            der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))
        )

    def sign_challenge(self, challenge):
        if len(challenge) != 32:
            raise ValueError(
                f"expected 32-byte MFi 3.0 challenge, got {len(challenge)}"
            )
        der = self.private_key.sign(
            challenge, ec.ECDSA(utils.Prehashed(hashes.SHA256()))
        )
        r, s = utils.decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def certificate_blob(self):
        return self.certificate
