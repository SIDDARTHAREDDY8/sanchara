"""Verified artifact download for the Sanchara robot agent.

download_and_verify() fetches a release tarball from the control plane's
GET /artifacts/{release_id} endpoint, streams it to disk, and verifies it
BEFORE it is ever used:

  1. sha256 of the downloaded bytes must match the X-Sanchara-SHA256
     response header,
  2. the ed25519 signature in the X-Sanchara-Signature header must verify
     against the operator's public key.

Any mismatch raises ArtifactVerificationError and the temp file is removed
— nothing unverified ever lands in the destination directory.
"""
import base64
import hashlib
import os
import tempfile

import requests

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class ArtifactVerificationError(Exception):
    """Raised when an artifact fails checksum or signature verification,
    or the server response is not usable."""


def verify_artifact_bytes(data, sha256_hex, pubkey_b64, sig_b64):
    """Verify raw artifact bytes against an expected sha256 hex digest and
    a base64 ed25519 signature. Raises ArtifactVerificationError."""
    if not sha256_hex:
        raise ArtifactVerificationError("server did not provide a sha256 checksum")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256_hex.lower():
        raise ArtifactVerificationError(
            f"sha256 mismatch: expected {sha256_hex}, got {actual}")
    if not pubkey_b64:
        raise ArtifactVerificationError("no artifact public key configured")
    if not sig_b64:
        raise ArtifactVerificationError("server did not provide a signature")
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
    try:
        pub.verify(base64.b64decode(sig_b64), data)
    except (InvalidSignature, ValueError) as e:
        raise ArtifactVerificationError(f"ed25519 signature invalid: {e}")


def download_and_verify(server_url, release_id, bearer_token, pubkey_b64,
                        dest_dir, timeout=60, progress_cb=None):
    """Download + verify a release tarball. Returns the final Path on success.

    Raises ArtifactVerificationError on any verification failure and
    requests.HTTPError / requests errors on transport failure.
    progress_cb(downloaded_bytes, total_bytes) is called as chunks arrive.
    """
    os.makedirs(dest_dir, exist_ok=True)
    url = server_url.rstrip("/") + f"/artifacts/{release_id}"
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    try:
        with requests.get(url, headers=headers, stream=True,
                          timeout=timeout) as r:
            r.raise_for_status()
            sha_expected = r.headers.get("X-Sanchara-SHA256", "")
            sig_b64 = r.headers.get("X-Sanchara-Signature", "")
            total = int(r.headers.get("Content-Length", "0") or 0)
            fd, tmp = tempfile.mkstemp(suffix=".part", dir=dest_dir)
            downloaded = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
                with open(tmp, "rb") as f:
                    data = f.read()
                verify_artifact_bytes(data, sha_expected, pubkey_b64, sig_b64)
                final = os.path.join(dest_dir, f"{release_id}.tar.gz")
                os.replace(tmp, final)
                return final
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    except ArtifactVerificationError:
        raise
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code in (401, 403):
            raise ArtifactVerificationError(
                f"artifact download rejected by server (HTTP {code})")
        raise
