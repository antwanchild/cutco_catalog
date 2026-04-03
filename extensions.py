from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
# memory:// is intentional — no Redis available; per-worker counters are
# acceptable for admin login brute-force protection on a self-hosted instance.
limiter = Limiter(key_func=get_remote_address, default_limits=[],
                  storage_uri="memory://")
