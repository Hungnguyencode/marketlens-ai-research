import json
from pathlib import Path

from .models import Evidence


class EvidenceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, evidence: list[Evidence]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([e.model_dump(mode="json") for e in evidence], indent=2), encoding="utf-8")

    def load(self) -> list[Evidence]:
        if not self.path.exists():
            return []
        return [Evidence.model_validate(row) for row in json.loads(self.path.read_text(encoding="utf-8"))]
