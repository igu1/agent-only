from agno.tools import tool
from datetime import datetime
from zoneinfo import ZoneInfo


@tool
def get_current_day() -> str:
    """Get the current day of the week (e.g., Monday, Tuesday) in Asia/Kolkata timezone."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A")