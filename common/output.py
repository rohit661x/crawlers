"""JSONL writer: one file per crawler per UTC day under DATA_ROOT/<crawler>/."""
import json, logging, sys
from datetime import datetime, timezone
from . import config

def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return logging.getLogger(name)

class JsonlSink:
    def __init__(self, crawler: str):
        d = config.DATA_ROOT / crawler
        d.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.path = d / f"{day}.jsonl"
        self._f = open(self.path, "a", encoding="utf-8")

    def write(self, record: dict):
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self):
        self._f.close()
