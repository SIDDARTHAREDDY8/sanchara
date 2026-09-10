"""Release artifact store for the Sanchara control plane.

Tarballs live on the filesystem under SANCHARA_ARTIFACT_DIR (default
"<control-plane dir>/artifacts"); checksums + ed25519 signatures live in
the DB via store (migration 2). Signature verification uses the
``cryptography`` package.

config.py is owned by another worker, so the env var is read directly here
(documented default above) rather than through the settings object.
"""
import base64
import hashlib
import os
import shutil

import store

_HERE = os.path.dirname(os.path.abspath(__file__))

ARTIFACT_DIR = os.environ.get("SANCHARA_ARTIFACT_DIR",
                              os.path.join(_HERE, "artifacts"))


def artifact_path(release_id):
    """Filesystem path of a release's tarball (may not exist yet)."""
    return os.path.join(ARTIFACT_DIR, f"{release_id}.tar.gz")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_artifact(release_id, tarball_path, sig_b64=None):
    """Copy a server-local tarball into the artifact store, persist its
    sha256 + base64 ed25519 signature. Returns (sha256_hex, size_bytes)."""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    sha = sha256_file(tarball_path)
    size = os.path.getsize(tarball_path)
    dest = artifact_path(release_id)
    shutil.copyfile(tarball_path, dest)
    store.set_release_artifact(release_id, sha, sig_b64 or "")
    return sha, size


def verify_signature(pubkey_b64, data, sig_b64):
    """Verify a base64 ed25519 signature over raw bytes. Returns bool."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
    try:
        pub.verify(base64.b64decode(sig_b64), data)
        return True
    except InvalidSignature:
        return False


def sign_bytes(privkey_b64, data):
    """Helper (demo/tests): sign raw bytes, return base64 signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(privkey_b64))
    return base64.b64encode(priv.sign(data)).decode()


def generate_keypair():
    """Helper (demo/tests): return (privkey_b64, pubkey_b64) ed25519 pair."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    from cryptography.hazmat.primitives import serialization
    priv_b64 = base64.b64encode(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())).decode()
    pub_b64 = base64.b64encode(pub.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw)).decode()
    return priv_b64, pub_b64
