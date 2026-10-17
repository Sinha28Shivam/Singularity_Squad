import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@lru_cache(maxsize=None)
def load_json(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
