"""WorkBuddy 消费的命令结果，使用标准输出的最后一行。"""
import json
from pathlib import Path
from typing import Any

from sellerops.core.utils import ensure_utf8_stdout


def emit_summary(command: str, mode: str, status: str, shops: list[int],
                 artifacts: dict[str, str | Path], **fields: Any) -> None:
    ensure_utf8_stdout()
    print(json.dumps({"schema_version": 1, "command": command, "mode": mode, "status": status,
                      "shops": shops, "artifacts": {key: str(Path(path).resolve()) for key, path in artifacts.items()},
                      **fields}, ensure_ascii=False, allow_nan=False))
