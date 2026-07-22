from typing import Any


class ChannelRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, channel_type: str, adapter: Any) -> None:
        self._adapters[str(channel_type)] = adapter

    def get(self, channel_type: str) -> Any:
        return self._adapters[str(channel_type)]

    def has(self, channel_type: str) -> bool:
        return str(channel_type) in self._adapters


registry = ChannelRegistry()
