# config - env vars only, no DB config needed anymore
import os


def get_env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return value


def get_timezone(profile: dict | None = None) -> str:
    """Tenant timezone: profile override > AGENT_TIMEZONE env > Asia/Riyadh."""
    tz = (profile or {}).get('timezone') if isinstance(profile, dict) else None
    return tz or get_env('AGENT_TIMEZONE') or 'Asia/Riyadh'
