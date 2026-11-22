"""保留旧命令回执，并供统一 JSON 模式复用其结构。"""
import json
from pathlib import Path
from typing import Any

from sellerops.core.utils import ensure_utf8_stdout


def emit_summary(command: str, mode: str, status: str, shops: list[int],
                 artifacts: dict[str, str | Path], *, print_legacy: bool = True, **fields: Any) -> dict:
    summary = {"schema_version": 1, "command": command, "mode": mode, "status": status,
               "shops": shops, "artifacts": {key: str(Path(path).resolve()) for key, path in artifacts.items()},
               **fields}
    if print_legacy:
        ensure_utf8_stdout()
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return summary
