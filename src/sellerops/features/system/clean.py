"""Preview or remove expired files from the three managed output directories."""
from argparse import Namespace
from dataclasses import asdict, dataclass
from os import stat_result
from pathlib import Path
import stat
from time import time_ns

from sellerops.settings import paths


@dataclass
class CleanupCounts:
    files: int = 0
    directories: int = 0
    bytes: int = 0
    skipped_reparse_points: int = 0
    errors: int = 0


def _is_reparse_point(info: stat_result) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _clean_entry(path: Path, protected: Path | None, counts: CleanupCounts, cutoff: int,
                 apply: bool, *, root: bool = False) -> bool:
    """Return whether the entry is gone or would be removed in a preview."""
    try:
        info = path.lstat()
        if _is_reparse_point(info):
            counts.skipped_reparse_points += 1
            return False
        if path == protected:
            return False
        if stat.S_ISDIR(info.st_mode):
            empty = True
            for child in path.iterdir():
                if not _clean_entry(child, protected, counts, cutoff, apply):
                    empty = False
            if root or not empty:
                return False
            if apply:
                if _is_reparse_point(path.lstat()):
                    counts.skipped_reparse_points += 1
                    return False
                path.rmdir()
            counts.directories += 1
        else:
            if root:
                raise NotADirectoryError('清理根路径必须是目录')
            if not stat.S_ISREG(info.st_mode) or info.st_mtime_ns >= cutoff:
                return False
            if apply:
                path.unlink()
            counts.files += 1
            counts.bytes += info.st_size
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        counts.errors += 1
        print(f'[错误] {path}：{error.strerror or error}')
        return False


def run(args: Namespace) -> int:
    data = paths.current
    data_dir = Path(data.data_dir)
    exports = data.exports_dir
    if exports == data_dir:
        raise ValueError('导出目录与数据目录重合，无法清理')
    scratch = paths.PROJECT_ROOT / '_scratch'
    roots = [
        ('data/logs', Path(data.log_dir), None),
        ('exports', exports, exports / 'workbench'),
    ]
    if data_dir == paths.PROJECT_ROOT / 'data':
        roots.append(('_scratch', scratch, scratch / 'README.md'))

    cutoff = time_ns() - args.days * 86_400 * 1_000_000_000
    groups = {}
    args.output = {'mode': 'apply' if args.apply else 'preview', 'days': args.days, 'groups': groups}
    action = '已删除' if args.apply else '待删除'
    space = '已释放' if args.apply else '可释放'
    for label, root, protected in roots:
        counts = CleanupCounts()
        _clean_entry(root, protected, counts, cutoff, args.apply, root=True)
        size_mb = counts.bytes / 1024**2
        groups[label] = {'path': str(root), **asdict(counts), 'size_mb': size_mb}
        print(f'{label}（{root}）：{action} {counts.files} 个文件、{counts.directories} 个目录，'
              f'{space} {size_mb:.2f} MB，跳过 {counts.skipped_reparse_points} 个重解析点')
        if counts.skipped_reparse_points:
            print(f'已跳过 {counts.skipped_reparse_points} 个符号链接/目录联接，请人工确认')

    failed = any(group['errors'] for group in groups.values())
    args.summary = '清理遇到文件处理错误' if failed else '清理完成' if args.apply else '清理预览完成'
    if failed:
        args.next_action = '核对目录权限、文件占用及已处理数量'
    print(args.summary)
    return int(failed)
