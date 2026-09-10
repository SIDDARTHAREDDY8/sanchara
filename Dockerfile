# Sanchara images. Two lean targets, no build tools in the final layers:
#   target control-plane - FastAPI + SQLite rollout engine + dashboard
#   target agent         - simulated-ROS robot agent (requests only)
#
# Build:  docker build --target control-plane -t sanchara-control-plane .
#         docker build --target agent -t sanchara-agent .

# ---- shared base: non-root runtime user ----
FROM python:3.12-slim AS base
RUN useradd --create-home --uid 10001 sanchara
WORKDIR /app

# ---- control plane ----
FROM base AS control-plane
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY control-plane/ ./control-plane/
COPY dashboard/ ./dashboard/
USER sanchara
WORKDIR /app/control-plane
ENV SANCHARA_PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec python -m uvicorn app:app --host 0.0.0.0 --port ${SANCHARA_PORT:-8000}"]

# ---- robot agent ----
FROM base AS agent
# cryptography is required: agent.py imports secure_update (ed25519 artifact
# verification) at module level even when --artifact-pubkey is not used.
RUN pip install --no-cache-dir "requests>=2.31" "cryptography>=42"
COPY agent/ ./agent/
USER sanchara
WORKDIR /app/agent
ENTRYPOINT ["python", "agent.py"]
