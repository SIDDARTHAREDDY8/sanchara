"""Environment-based configuration for the Sanchara control plane.

Every setting has a sane default so the control plane boots with no
environment at all; operators override via env vars.

    SANCHARA_PORT         - HTTP port the API listens on (default "8000").
    SANCHARA_DB           - path to the SQLite database file
                            (default "<control-plane dir>/sanchara.db").
                            The legacy misspelled SANCHRA_DB is still honored
                            as a fallback so old deployments keep working.
    SANCHARA_OPERATOR_KEY - shared secret for the operator API (default ""
                            = auth disabled for now). Enforcement is a later
                            worker's job; see README roadmap.
    SANCHARA_ENGINE_TICK_S - seconds between rollout-engine ticks
                            (default "2").
    SANCHARA_LOG_LEVEL    - logging level name, e.g. DEBUG / INFO / WARNING
                            (default "INFO").

Consumed as a single ``settings`` object; never import ``os.environ`` for
these values elsewhere.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


class Settings:
    def __init__(self):
        self.port = int(os.environ.get("SANCHARA_PORT", "8000"))
        self.db_path = os.environ.get(
            "SANCHARA_DB",
            os.environ.get("SANCHRA_DB", os.path.join(_HERE, "sanchara.db")),
        )
        self.operator_key = os.environ.get("SANCHARA_OPERATOR_KEY", "")
        self.engine_tick_s = float(os.environ.get("SANCHARA_ENGINE_TICK_S", "2"))
        self.log_level = os.environ.get("SANCHARA_LOG_LEVEL", "INFO").upper()

    def redacted(self):
        """Config dict safe for logs: secrets are never printed."""
        return {
            "port": self.port,
            "db_path": self.db_path,
            "operator_key": "***" if self.operator_key else "(disabled)",
            "engine_tick_s": self.engine_tick_s,
            "log_level": self.log_level,
        }


settings = Settings()
