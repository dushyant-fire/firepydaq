from __future__ import annotations

from pathlib import Path


class LegacySaveAdapter:
    """Compatibility adapter while GUI save calls are migrated to SaveManager."""

    def __init__(self, application) -> None:
        self.application = application
        self.output_prefix: Path | None = None

    def start(self, output_prefix: str | Path) -> None:
        self.output_prefix = Path(output_prefix)
        self.application._start_safe_writer()

    def stop(self):
        return self.application._finalize_safe_writer()
