"""Sanchara authentication: per-robot bearer tokens + operator API key.

Auth model (boring on purpose — no JWT, no OAuth, no mTLS):

* Operator API: a shared secret (`SANCHARA_OPERATOR_KEY`). Sent as the
  ``X-API-Key`` header or as ``Authorization: Bearer <key>``. If the key is
  empty the API is open but a loud one-time warning is logged.
* Robot API: one random bearer token per robot, issued at registration or
  on first heartbeat. The server stores ONLY the sha256 hash
  (``robots.token_hash``); the plaintext is shown exactly once, in the
  registration / bootstrap response. Comparison uses hmac.compare_digest.

Bootstrap rule: a heartbeat for an UNKNOWN robot id (or a known robot that
has never been issued a token) with no/invalid token auto-registers it and
the response gains a one-time ``"token"`` field. A KNOWN robot that already
has a token hash but presents the wrong (or no) token gets 401.

Provisioning tradeoff: this auto-bootstrap trusts whoever claims an unused
robot id first — convenient for demos, wrong for production. Production
should use a claim flow: the operator pre-creates the robot row
(POST /api/robots, which returns a one-time token) or pre-provisions
per-robot enrollment tokens that the agent exchanges once for its bearer
token, and revokes stale robots. mTLS robot identity is the long-term
answer (see README roadmap); it is explicitly NOT implemented here.
"""
import hashlib
import hmac
import logging
import secrets

from fastapi import HTTPException, Request

import store
from config import settings

log = logging.getLogger("sanchara.auth")

_warned_operator_disabled = False


def issue_robot_token():
    """Return (token_plaintext, token_hash). Store ONLY the hash."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def require_operator(request: Request):
    """FastAPI dependency: operator routes need the shared operator key.

    Accepts the ``X-API-Key`` header or ``Authorization: Bearer <key>``.
    When SANCHARA_OPERATOR_KEY is empty the request is allowed through,
    but a loud warning is logged once per process.
    """
    global _warned_operator_disabled
    key = settings.operator_key
    if not key:
        if not _warned_operator_disabled:
            _warned_operator_disabled = True
            log.error(
                "OPERATOR AUTH DISABLED: SANCHARA_OPERATOR_KEY is empty — "
                "operator API is open to anyone who can reach it. "
                "Set SANCHARA_OPERATOR_KEY to a strong random value."
            )
        return
    presented = request.headers.get("x-api-key", "")
    if not presented:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            presented = authz[7:].strip()
    if not presented or not hmac.compare_digest(presented, key):
        raise HTTPException(status_code=401, detail="invalid operator key")


def require_robot(robot_id: str, request: Request):
    """FastAPI dependency: robot routes need the robot's bearer token.

    - Unknown robot id (or known robot with no token hash yet) -> allowed
      through so the endpoint can run the bootstrap flow and issue a token.
    - Known robot WITH a stored token hash -> ``Authorization: Bearer``
      must hash-match the stored value, else 401.

    Returns the robot id on success (None is never returned — the allow
    cases return the robot id too; the bootstrap happens in the endpoint).
    """
    stored = store.get_robot_token_hash(robot_id)
    if stored is None:
        # unknown robot, or never issued a token: endpoint bootstraps it
        return robot_id
    authz = request.headers.get("authorization", "")
    presented = ""
    if authz.lower().startswith("bearer "):
        presented = authz[7:].strip()
    if not presented or not hmac.compare_digest(hash_token(presented), stored):
        raise HTTPException(status_code=401, detail="invalid robot token")
    return robot_id


def require_any_robot(request: Request):
    """FastAPI dependency for robot routes that carry no robot id in the
    path (e.g. GET /artifacts/{release_id}): the bearer token must belong
    to SOME known robot. Returns that robot's id; 401 otherwise."""
    authz = request.headers.get("authorization", "")
    presented = ""
    if authz.lower().startswith("bearer "):
        presented = authz[7:].strip()
    if not presented:
        raise HTTPException(status_code=401, detail="missing robot token")
    robot_id = store.robot_id_for_token_hash(hash_token(presented))
    if robot_id is None:
        raise HTTPException(status_code=401, detail="invalid robot token")
    return robot_id
