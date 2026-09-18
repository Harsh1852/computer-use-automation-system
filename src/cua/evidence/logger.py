"""Structured run log: one JSON object per line, in `run.jsonl`.

Built with `structlog.wrap_logger` rather than `structlog.configure`, because
global configuration would make two concurrent runs write into each other's
file. The processor chain is constructed per run and exposed, which is the
seam the redactor is inserted into: redaction belongs at the sink, where a
caller cannot forget to apply it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import structlog

Processor = Callable[[Any, str, dict], Any]


class RunLog:
    """A JSONL log bound to one replay run."""

    def __init__(
        self,
        run_dir: str | Path,
        run_id: str,
        *,
        processors: Sequence[Processor] = (),
        also_stdout: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "run.jsonl"
        self.run_id = run_id
        self._stream = self.path.open("a", encoding="utf-8")
        self._also_stdout = also_stdout

        chain: list[Processor] = [
            *processors,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(sort_keys=True),
        ]
        self._chain = chain
        self._log = structlog.wrap_logger(
            structlog.PrintLogger(file=self._stream), processors=chain
        ).bind(run_id=run_id)

    @property
    def processors(self) -> list[Processor]:
        """The live chain. Phase 7 inserts the redactor at position 0."""
        return self._chain

    def event(self, event: str, **fields: Any) -> None:
        self._log.info(event, **fields)
        self._stream.flush()
        if self._also_stdout:
            print(f"  · {event} " + " ".join(f"{k}={v}" for k, v in fields.items()))

    def bind(self, **fields: Any) -> None:
        self._log = self._log.bind(**fields)

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
