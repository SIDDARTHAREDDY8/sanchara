"""Security tests for Sanchara: bearer tokens, operator key, artifacts.

Unit-testable without a running control plane. The store points at a
throwaway SQLite file (SANCHARA_DB is set before control-plane imports),
and download_and_verify() is tested against a tiny stdlib HTTP server —
no FastAPI needed.
"""
import base64
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_db_fd, _db_path = tempfile.mkstemp(prefix="sanchara-sec-test-", suffix=".db")
os.close(_db_fd)
os.environ["SANCHARA_DB"] = _db_path

CP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../control-plane")
AG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../agent")
sys.path.insert(0, os.path.abspath(CP))
sys.path.insert(0, os.path.abspath(AG))

from starlette.requests import Request  # noqa: E402

import store  # noqa: E402
import auth  # noqa: E402
import artifacts  # noqa: E402
from artifacts import generate_keypair, sign_bytes, verify_signature  # noqa: E402
from secure_update import (  # noqa: E402
    ArtifactVerificationError, download_and_verify, verify_artifact_bytes,
)

store.init_db()


# ---------- helpers ----------
def make_request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": raw})


def register_robot_with_token(robot_id):
    store.upsert_robot({"id": robot_id, "name": robot_id})
    token, token_hash = auth.issue_robot_token()
    store.set_robot_token_hash(robot_id, token_hash)
    return token


def make_tarball_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"2.0.0\n"
        info = tarfile.TarInfo(name="VERSION")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---------- token hashing ----------
def test_issue_token_round_trip():
    token, token_hash = auth.issue_robot_token()
    assert token and token_hash
    # plaintext looks random, hash is a sha256 hex digest
    assert len(token_hash) == 64
    assert auth.hash_token(token) == token_hash
    assert hashlib.sha256(token.encode()).hexdigest() == token_hash
    # the server stores only the hash — plaintext is never derivable
    assert token not in token_hash


def test_token_hash_store_round_trip():
    token = register_robot_with_token("sec-robot-1")
    stored = store.get_robot_token_hash("sec-robot-1")
    assert stored == auth.hash_token(token)
    assert store.get_robot_token_hash("no-such-robot") is None


def test_migration_2_columns_exist():
    with store.LOCK, store.conn() as c:
        robot_cols = {r["name"] for r in c.execute("PRAGMA table_info(robots)")}
        rel_cols = {r["name"] for r in c.execute("PRAGMA table_info(releases)")}
    assert "token_hash" in robot_cols
    assert "artifact_sha256" in rel_cols and "artifact_sig" in rel_cols


# ---------- require_robot ----------
def test_require_robot_unknown_robot_allowed_for_bootstrap():
    rid = auth.require_robot("sec-unknown-robot", make_request())
    assert rid == "sec-unknown-robot"


def test_require_robot_known_robot_needs_valid_token():
    token = register_robot_with_token("sec-robot-2")
    # correct bearer -> allowed
    r = make_request({"Authorization": f"Bearer {token}"})
    assert auth.require_robot("sec-robot-2", r) == "sec-robot-2"
    # wrong bearer -> 401
    from fastapi import HTTPException
    try:
        auth.require_robot("sec-robot-2", make_request({"Authorization": "Bearer wrong"}))
        raise AssertionError("expected 401")
    except HTTPException as e:
        assert e.status_code == 401
    # missing bearer -> 401
    try:
        auth.require_robot("sec-robot-2", make_request())
        raise AssertionError("expected 401")
    except HTTPException as e:
        assert e.status_code == 401


def test_require_robot_known_robot_without_token_allowed_for_bootstrap():
    store.upsert_robot({"id": "sec-robot-3"})  # legacy: no token hash
    assert store.get_robot_token_hash("sec-robot-3") is None
    assert auth.require_robot("sec-robot-3", make_request()) == "sec-robot-3"


def test_require_robot_header_whitespace_tolerated():
    # HTTP optional whitespace around the header value is stripped
    token = register_robot_with_token("sec-robot-4")
    r = make_request({"Authorization": "Bearer  " + token + " "})
    assert auth.require_robot("sec-robot-4", r) == "sec-robot-4"


def test_require_any_robot_matches_token_to_robot():
    from fastapi import HTTPException
    token = register_robot_with_token("sec-robot-5")
    r = make_request({"Authorization": f"Bearer {token}"})
    assert auth.require_any_robot(r) == "sec-robot-5"
    try:
        auth.require_any_robot(make_request({"Authorization": "Bearer bogus"}))
        raise AssertionError("expected 401")
    except HTTPException as e:
        assert e.status_code == 401
    try:
        auth.require_any_robot(make_request())
        raise AssertionError("expected 401")
    except HTTPException as e:
        assert e.status_code == 401


# ---------- require_operator ----------
def test_require_operator_key_disabled_allows_with_warning():
    from config import settings
    settings.operator_key = ""
    auth._warned_operator_disabled = False
    auth.require_operator(make_request())  # must not raise
    assert auth._warned_operator_disabled is True


def test_require_operator_key_enforced():
    from config import settings
    from fastapi import HTTPException
    settings.operator_key = "op-secret-123"
    try:
        auth.require_operator(make_request({"X-API-Key": "op-secret-123"}))
        auth.require_operator(make_request({"Authorization": "Bearer op-secret-123"}))
        for bad in [{}, {"X-API-Key": "nope"},
                    {"Authorization": "Bearer nope"}]:
            try:
                auth.require_operator(make_request(bad))
                raise AssertionError(f"expected 401 for {bad}")
            except HTTPException as e:
                assert e.status_code == 401
    finally:
        settings.operator_key = ""
        auth._warned_operator_disabled = False


# ---------- signatures ----------
def test_signature_verify_pass_fail():
    priv_b64, pub_b64 = generate_keypair()
    data = make_tarball_bytes()
    sig_b64 = sign_bytes(priv_b64, data)
    assert verify_signature(pub_b64, data, sig_b64) is True
    # tampered byte -> fail
    bad = bytearray(data)
    bad[10] ^= 0xFF
    assert verify_signature(pub_b64, bytes(bad), sig_b64) is False
    # wrong key -> fail
    _, other_pub = generate_keypair()
    assert verify_signature(other_pub, data, sig_b64) is False


def test_release_artifact_store_round_trip():
    rel = store.create_release({"version": "9.9.9"})
    sha = hashlib.sha256(b"x").hexdigest()
    store.set_release_artifact(rel["id"], sha, "c2ln")
    got_sha, got_sig = store.get_release_artifact(rel["id"])
    assert got_sha == sha and got_sig == "c2ln"
    assert store.get_release_artifact("rel_missing") == (None, None)


def test_ingest_artifact_copies_and_records():
    tmpdir = tempfile.mkdtemp(prefix="sanchara-artifacts-")
    old = artifacts.ARTIFACT_DIR
    artifacts.ARTIFACT_DIR = tmpdir
    try:
        data = make_tarball_bytes()
        src = os.path.join(tmpdir, "src.tar.gz")
        with open(src, "wb") as f:
            f.write(data)
        rel = store.create_release({"version": "9.9.8"})
        sha, size = artifacts.ingest_artifact(rel["id"], src, "c2ln")
        assert sha == hashlib.sha256(data).hexdigest()
        assert size == len(data)
        assert os.path.exists(artifacts.artifact_path(rel["id"]))
        got_sha, got_sig = store.get_release_artifact(rel["id"])
        assert got_sha == sha and got_sig == "c2ln"
    finally:
        artifacts.ARTIFACT_DIR = old


# ---------- download_and_verify over a real HTTP server ----------
class _ArtifactHandler(BaseHTTPRequestHandler):
    data = b""
    sha = ""
    sig = ""
    require_auth = True

    def do_GET(self):
        if self.require_auth and self.headers.get("Authorization") != "Bearer good-token":
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/artifacts/tampered":
            body = self.data + b"evil"
            sha, sig = self.sha, self.sig  # headers stay the originals
        elif self.path == "/artifacts/noheaders":
            body, sha, sig = self.data, "", ""
        elif self.path == "/artifacts/rel-1":
            body, sha, sig = self.data, self.sha, self.sig
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Sanchara-SHA256", sha)
        self.send_header("X-Sanchara-Signature", sig)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve_in_thread():
    srv = HTTPServer(("127.0.0.1", 0), _ArtifactHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_download_and_verify_ok():
    data = make_tarball_bytes()
    priv_b64, pub_b64 = generate_keypair()
    _ArtifactHandler.data = data
    _ArtifactHandler.sha = hashlib.sha256(data).hexdigest()
    _ArtifactHandler.sig = sign_bytes(priv_b64, data)
    _ArtifactHandler.require_auth = True
    srv = _serve_in_thread()
    try:
        dest = tempfile.mkdtemp(prefix="sanchara-dl-")
        seen = []
        path = download_and_verify(
            f"http://127.0.0.1:{srv.server_port}", "rel-1", "good-token",
            pub_b64, dest, progress_cb=lambda d, t: seen.append((d, t)))
        with open(path, "rb") as f:
            assert f.read() == data
        assert seen, "expected progress callbacks"
        assert seen[-1][0] == len(data)
    finally:
        srv.shutdown()


def test_download_and_verify_rejects_tampered_bytes():
    srv = _serve_in_thread()
    try:
        dest = tempfile.mkdtemp(prefix="sanchara-dl-")
        try:
            download_and_verify(
                f"http://127.0.0.1:{srv.server_port}", "tampered", "good-token",
                generate_keypair()[1], dest)
            raise AssertionError("expected ArtifactVerificationError")
        except ArtifactVerificationError as e:
            assert "sha256" in str(e).lower()
        # nothing unverified left on disk
        assert not os.listdir(dest), os.listdir(dest)
    finally:
        srv.shutdown()


def test_download_and_verify_rejects_missing_headers():
    srv = _serve_in_thread()
    try:
        dest = tempfile.mkdtemp(prefix="sanchara-dl-")
        try:
            download_and_verify(
                f"http://127.0.0.1:{srv.server_port}", "noheaders", "good-token",
                generate_keypair()[1], dest)
            raise AssertionError("expected ArtifactVerificationError")
        except ArtifactVerificationError:
            pass
    finally:
        srv.shutdown()


def test_download_and_verify_rejects_bad_token():
    srv = _serve_in_thread()
    try:
        dest = tempfile.mkdtemp(prefix="sanchara-dl-")
        try:
            download_and_verify(
                f"http://127.0.0.1:{srv.server_port}", "rel-1", "bad-token",
                generate_keypair()[1], dest)
            raise AssertionError("expected ArtifactVerificationError")
        except ArtifactVerificationError as e:
            assert "401" in str(e)
    finally:
        srv.shutdown()


def test_verify_artifact_bytes_unit_path():
    priv_b64, pub_b64 = generate_keypair()
    data = make_tarball_bytes()
    sig = sign_bytes(priv_b64, data)
    sha = hashlib.sha256(data).hexdigest()
    verify_artifact_bytes(data, sha, pub_b64, sig)  # no raise
    try:
        verify_artifact_bytes(data, sha, pub_b64, sign_bytes(priv_b64, b"other"))
        raise AssertionError("expected raise")
    except ArtifactVerificationError:
        pass


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
