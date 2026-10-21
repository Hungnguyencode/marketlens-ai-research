from collections.abc import Callable


class ToolRegistry:
    def __init__(self, tools: dict[str, Callable]):
        self._tools = dict(tools)
        self.calls = 0

    @property
    def available_tools(self):
        return tuple(self._tools)

    def call(self, name: str, *, budget: int, **kwargs):
        if name not in self._tools:
            raise ValueError(f"Tool is not allowlisted: {name}")
        if self.calls >= budget:
            raise RuntimeError("Tool-call budget exhausted")
        self.calls += 1
        return self._tools[name](**kwargs)
