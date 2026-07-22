# runtime - just project root, no DB baggage anymore
import os

_project_root: str | None = None


def init_runtime(project_root: str) -> None:
    global _project_root
    _project_root = project_root


def get_project_root() -> str:
    if _project_root is None:
        return os.path.dirname(os.path.dirname(__file__))
    return _project_root
