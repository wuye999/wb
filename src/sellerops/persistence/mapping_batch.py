"""在一个数据目录内准备映射文件。清单落盘后，中断的替换可继续。"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator

from sellerops.core.files import FileLock, atomic_dump_json, safe_load_json
from sellerops.settings import paths


SHOP_TABLE = re.compile(r'shop_\d+(?:_[^<>:"/\\|?*]+)?\.xlsx')


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MappingBatch:
    def __init__(self, data: paths.DataPaths):
        self.root = Path(data.data_dir)
        state = Path(data.state_dir)
        self.work = state / ".mapping-work"
        self.manifest = state / ".mapping-pending.json"
        self.targets: list[str] = []
        self.input_data: Any = None
        self.fixed = {Path(name) for name in (data.mapping_xlsx, data.vc_known_json,
                                              data.vc_override_json, data.vc_excluded_json)}
        self.shops = Path(data.shops_dir)
        for path in (self.work, self.manifest):
            if path.resolve() != path:
                raise ValueError(f"映射恢复路径指向其他位置：{path}")

    def _target(self, relative: str) -> Path:
        target = self.root / relative
        shop_table = target.parent == self.shops and SHOP_TABLE.fullmatch(target.name)
        if (target.resolve() != target or Path(relative).is_absolute()
                or target not in self.fixed and not shop_table):
            raise ValueError(f"无效映射保存路径：{relative}")
        return target

    def _part(self, target: Path) -> Path:
        path = self.work / target.name
        if path.resolve() != path:
            raise ValueError(f"映射临时文件指向其他位置：{path}")
        return path

    def stage(self, destination: str) -> Path:
        relative = Path(destination).relative_to(self.root).as_posix()
        target = self._target(relative)
        if relative in self.targets:
            raise ValueError(f"重复的映射输出：{target}")
        part = self._part(target)
        self.targets.append(relative)
        return part

    def discard_preparation(self) -> None:
        """只删除本模块生成的映射文件；有清单时必须保留现场。"""
        if self.manifest.exists() or not self.work.exists():
            return
        parts = list(self.work.iterdir())
        fixed_names = {path.name for path in self.fixed}
        for part in parts:
            if (part.name not in fixed_names and not SHOP_TABLE.fullmatch(part.name)
                    or not part.is_file() or part.resolve() != part):
                raise ValueError(f"映射临时目录中有未识别文件：{part}")
        for part in parts:
            part.unlink()
        self.work.rmdir()

    def commit(self, request: dict, input_hash: str | None) -> None:
        files = {}
        for name in self.targets:
            part = self._part(self._target(name))
            with part.open("rb+") as stream:
                os.fsync(stream.fileno())
            files[name] = _digest(part)
        if files:
            record = {"request": request, "input_hash": input_hash, "files": files}
            atomic_dump_json(self.manifest, record, use_lock=False)
            self.finish(record)

    def finish(self, record: dict) -> None:
        """先核对全部内容，再继续替换。已移动的文件按正式文件校验。"""
        if (not isinstance(record, dict) or not isinstance(record.get("request"), dict)
                or record["request"].get("command") not in {"shop-mappings", "merge", "rename", "import-mapping"}
                or "input_hash" not in record
                or record["input_hash"] is not None and not re.fullmatch("[0-9a-f]{64}", str(record["input_hash"]))
                or not isinstance(record.get("files"), dict) or not record["files"]):
            raise ValueError(f"映射保存清单损坏：{self.manifest}")
        remaining = []
        for name, digest in record["files"].items():
            target = self._target(name)
            part = self._part(target)
            source = part if part.exists() else target
            if not source.is_file() or _digest(source) != digest:
                raise ValueError(f"映射文件无法核对，已保留恢复清单：{source}")
            if source == part:
                remaining.append((part, target))
        if self.work.exists() and set(self.work.iterdir()) != {part for part, _ in remaining}:
            raise ValueError(f"恢复清单与临时文件不一致：{self.work}")
        for part, target in remaining:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(part, target)
            except PermissionError as exc:
                raise PermissionError(f"请关闭《{target.name}》，重新运行刚才的命令，程序会继续保存。") from exc
            except OSError as exc:
                raise OSError(f"无法保存 {target}，已保留恢复清单：{exc}") from exc
        if self.work.exists():
            self.work.rmdir()
        self.manifest.unlink()


@contextmanager
def mapping_batch(command: str, *, input_file: str | None = None,
                  **options: Any) -> Iterator[MappingBatch | None]:
    """同一请求续完后返回 None；新请求在旧批次完成后准备。"""
    batch = MappingBatch(paths.current)
    source = Path(input_file).resolve() if input_file else None
    request = {"command": command, "input": str(source) if source else None, **options}
    with FileLock(batch.manifest):
        prior = None
        if batch.manifest.exists():
            prior = safe_load_json(batch.manifest)
            batch.finish(prior)
            print("上次映射保存已完成。")
        contents = None
        if source is not None:
            try:
                contents = source.read_bytes()
            except FileNotFoundError:
                if not prior or prior["request"] != request:
                    raise
        input_hash = sha256(contents).hexdigest() if contents is not None else None
        if prior and prior["request"] == request and (contents is None or input_hash == prior["input_hash"]):
            yield None
            return
        batch.discard_preparation()
        if contents is not None:
            try:
                batch.input_data = json.loads(contents.decode("utf-8-sig"))
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"输入文件格式错误：{source}") from exc
        batch.work.mkdir(parents=True)
        try:
            yield batch
            batch.commit(request, input_hash)
        finally:
            batch.discard_preparation()
