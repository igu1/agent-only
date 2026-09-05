from agno.tools import tool
from datetime import datetime
from zoneinfo import ZoneInfo

from infra.config import get_timezone


def build_datetime_tools(*, timezone: str | None = None) -> list:
    """Clock tools, BOUND to the school's own timezone.

    Each school carries its own timezone on its AI profile, so a group with
    campuses in different regions has a different "today" per school. This used
    to be a module-level tool calling get_timezone() with NO profile, which
    always resolved to the deployment default - and the prompt tells the model
    to call it before answering anything about opening hours or availability,
    so a school an hour ahead answered from the wrong day.

    The zone is resolved once, when the agent is built (the profile is already
    loaded there), so the tool itself makes no lookup at call time.
    """
    tz = timezone or get_timezone()

    @tool
    def get_current_day() -> str:
        """Get the current day of the week (e.g., Monday, Tuesday) in the institution's local timezone."""
        return datetime.now(ZoneInfo(tz)).strftime("%A")

    return [get_current_day]
