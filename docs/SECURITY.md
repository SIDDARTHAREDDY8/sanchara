# Sanchara security model

## What we protect against

| Threat | Mitigation |
|---|---|
| Rogue client pushes a bad release | Operator API key required for all management endpoints |
| Attacker impersonates a robot to poison fleet state | Per-robot bearer tokens; wrong token → 401 |
| Attacker substitutes a malicious update tarball | ed25519 signature + SHA-256 verified by the agent before apply |
| Token database leak | Server stores only SHA-256 hashes (`hmac.compare_digest`); plaintext shown once at issuance |
| Stale/compromised robot acts on old orders | Tasks are only valid while the deployment is `running`/`rolling_back`; heartbeats older than `heartbeat_timeout_s` disqualify the robot |

## Auth model

- **Operator key** (`SANCHARA_OPERATOR_KEY`): required on every `/api/*`
  management endpoint (`X-API-Key` or `Authorization: Bearer`). Empty key =
  open dev mode with a loud per-request log warning. Never run production
  with an empty key.
- **Robot tokens**: `secrets.token_urlsafe(32)` issued once at `POST
  /api/robots` or on first-heartbeat auto-bootstrap. Robots send
  `Authorization: Bearer <token>` on heartbeat, task poll, status updates,
  and artifact downloads.
- **Bootstrap tradeoff (known, documented):** an unknown robot id that
  heartbeats with no token is auto-registered and issued a token. This makes
  onboarding frictionless but means anyone who can reach the server can claim
  a robot id. For production fleets, replace this with a provisioning claim
  flow (e.g. factory-installed token, or operator-approved registration
  queue). See `control-plane/auth.py`.

## Artifact integrity

Releases carry real tarballs, served at `GET /artifacts/{release_id}`:

1. Operator builds the tarball and signs it offline with the ed25519
   **private** key.
2. `POST /api/releases` with `artifact_path` + `artifact_sig_b64` ingests it;
   the server records SHA-256 and the signature.
3. The agent (`agent/secure_update.py`) streams the tarball to a temp file,
   verifies SHA-256 against the `X-Sanchara-SHA256` header **and** the ed25519
   signature against `X-Sanchara-Signature`, and only then hands it to the
   update step. Any mismatch raises `ArtifactVerificationError`; nothing
   unverified touches disk.
4. Agents are provisioned with the ed25519 **public** key
   (`--artifact-pubkey`). Key rotation = redeploy agents with the new pubkey
   (document the rotation window: sign with the new key only after all agents
   carry the new pubkey).

Without `--artifact-pubkey`, the agent skips download verification (legacy
sim path). Production agents must always be started with it.

## What this MVP does NOT yet do (be honest about it)

- **No TLS.** Traffic is plain HTTP. Terminate TLS at a reverse proxy
  (Caddy/nginx) or run inside a private network (Tailscale/VPN).
- **No mTLS robot identity.** Bearer tokens authenticate robots; a stolen
  token can be replayed until rotated. Rotate via re-registration.
- **No artifact encryption.** Tarballs are signed, not encrypted — don't ship
  secrets inside them.
- **No rate limiting / audit of auth failures.** Failed attempts are logged;
  put the server behind a proxy with rate limiting for internet exposure.
- **Single-node SQLite.** The DB file is the crown jewel — back it up
  (`sanchara.db` on a volume), restrict file permissions, and see the
  Postgres note in `docs/DEPLOYMENT.md`.

## Operational checklist

- [ ] `SANCHARA_OPERATOR_KEY` set to a long random value
- [ ] Agents started with `--artifact-pubkey` and per-robot `--token`
- [ ] TLS in front of the control plane
- [ ] DB on a persistent volume with backups
- [ ] Dashboard (`/`) restricted or put behind the same auth as the API
