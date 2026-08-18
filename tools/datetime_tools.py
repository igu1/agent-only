from agno.tools import tool
from datetime import datetime
from zoneinfo import ZoneInfo

from infra.config import get_timezone


@tool
def get_current_day() -> str:
    """Get the current day of the week (e.g., Monday, Tuesday) in the institution's local timezone."""
    return datetime.now(ZoneInfo(get_timezone())).strftime("%A")
