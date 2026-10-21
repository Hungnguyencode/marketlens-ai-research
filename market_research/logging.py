import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4


class ExecutionLogger:
    def __init__(self, path: str | Path, run_id: str):
        self.path, self.run_id = Path(path), run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def step(self, name: str, module: str, status: str, summary: str = "", error: str | None = None, duration: float = 0):
        ended = datetime.now(timezone.utc)
        from datetime import timedelta
        row = {"run_id": self.run_id, "step_name": name, "module_name": module,
               "started_at": (ended - timedelta(seconds=duration)).isoformat(), "ended_at": ended.isoformat(),
               "status": status, "duration_seconds": round(duration, 4), "summary": summary[:300], "error": error}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def timed(self, name, module, operation):
        start = perf_counter()
        try:
            result = operation()
            self.step(name, module, "success", "completed", duration=perf_counter() - start)
            return result
        except Exception as exc:
            self.step(name, module, "error", "operation failed", str(exc), perf_counter() - start)
            raise
