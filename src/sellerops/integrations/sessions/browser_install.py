"""Download and stage the pinned Windows x64 Playwright browser distribution."""
import sys

# -E retains the user site and still prepends the script directory on Python 3.10.
if __name__ == '__main__' and not (sys.flags.isolated or getattr(sys.flags, 'safe_path', False)):
    sys.path.pop(0)

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import shutil
import stat
import subprocess
import time
from urllib.parse import urljoin
from zipfile import BadZipFile, ZipFile


RECEIPT = 'sellerops-install.json'
_NETWORK, _INTEGRITY, _SECURITY, _DISK, _TLS = 10, 11, 12, 13, 14


@dataclass(frozen=True)
class Artifact:
    directory: str
    executable: str
    path: str
    size: int
    sha256: str

    def urls(self, source: str) -> tuple[str, ...]:
        if source == 'npmmirror':
            return (f'https://cdn.npmmirror.com/binaries/playwright/builds/{self.path}',)
        if self.path.startswith('cft/'):
            return (f'https://storage.googleapis.com/chrome-for-testing-public/{self.path[4:]}',
                    f'https://cdn.playwright.dev/builds/{self.path}')
        suffix = f'/dbazure/download/playwright/builds/{self.path}'
        return ('https://playwright.download.prss.microsoft.com' + suffix, 'https://cdn.playwright.dev' + suffix)


ARTIFACTS = (
    Artifact('chromium-1255', 'chrome-win64/chrome.exe', 'cft/159.0.8154.6/win64/chrome-win64.zip',
             217364368, '3e2514fddb71f6727e2079369e03a6a4e105adcb28f05a6bfd875b782bddc431'),
    Artifact('ffmpeg-1013', 'ffmpeg-win64.exe', 'ffmpeg/1013/ffmpeg-win64.zip',
             1663577, '0c4ee6c694e9bffea27128ab543ae573466c5245d7d16c054e40115d7d743874'),
    Artifact('winldd-1007', 'PrintDeps.exe', 'winldd/1007/winldd-win64.zip',
             128684, '0069f0d11d4ad6df068a068c003d22fe7dbec192a47bba64b2e115e9c8ce41d8'),
)


class InstallError(RuntimeError):
    pass


def supported() -> bool:
    return sys.platform == 'win32' and platform.machine().lower() in {'amd64', 'x86_64'}


def options() -> tuple[str, int]:
    source = os.environ.get('SELLEROPS_BROWSER_SOURCE', 'auto').strip().lower()
    seconds = os.environ.get('SELLEROPS_BROWSER_TIMEOUT_SECONDS', '180').strip()
    if source not in {'auto', 'official', 'npmmirror'}:
        raise InstallError('SELLEROPS_BROWSER_SOURCE 须为 auto、official 或 npmmirror。')
    if not seconds.isascii() or not seconds.isdecimal() or len(seconds) > 3 or not 30 <= int(seconds) <= 600:
        raise InstallError('SELLEROPS_BROWSER_TIMEOUT_SECONDS 须为 30–600 的整数秒数。')
    return source, int(seconds)


def expected_receipt(version: str) -> dict:
    return {'playwright': version, 'platform': 'win-x64',
            'artifacts': {artifact.directory: artifact.sha256 for artifact in ARTIFACTS}}


def complete(runtime: Path) -> bool:
    return all((runtime / artifact.directory / artifact.executable).is_file()
               and (runtime / artifact.directory / 'INSTALLATION_COMPLETE').is_file() for artifact in ARTIFACTS)


def has_receipt(runtime: Path, version: str) -> bool:
    try:
        return json.loads((runtime / RECEIPT).read_text(encoding='utf-8')) == expected_receipt(version)
    except (OSError, UnicodeError, ValueError):
        return False


def _download(artifact: Artifact, source: str, destination: Path) -> int:
    import requests

    allowed = artifact.urls(source)
    url = allowed[0]
    digest = hashlib.sha256()
    size = 0
    try:
        with requests.Session() as session:
            session.trust_env = False  # No ambient netrc, proxy credentials or CA overrides.
            for _ in range(len(allowed)):
                with session.get(url, stream=True, timeout=(10, 20), allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            return _SECURITY
                        target = urljoin(url, location)
                        if target not in allowed or target == url:
                            return _SECURITY
                        url = target
                        continue
                    if response.status_code != 200:
                        return _NETWORK
                    with destination.open('xb') as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            size += len(chunk)
                            if size > artifact.size:
                                return _INTEGRITY
                            digest.update(chunk)
                            output.write(chunk)
                    return 0 if size == artifact.size and digest.hexdigest() == artifact.sha256 else _INTEGRITY
    except requests.exceptions.SSLError:
        return _TLS
    except requests.RequestException:
        return _NETWORK
    except OSError:
        return _DISK
    return _SECURITY


def _download_archive(artifact: Artifact, stage: Path, budgets: dict[str, float]) -> Path:
    archive = stage / (artifact.directory + '.zip')
    env = os.environ.copy()
    # The site module still reads PYTHONUSERBASE with -E.
    env.pop('PYTHONUSERBASE', None)
    for source, remaining in budgets.items():
        if remaining <= 0:
            continue
        print(f'[浏览器下载] {artifact.directory} | {source}', flush=True)
        start = time.monotonic()
        try:
            command = [sys.executable, '-E']
            if sys.flags.no_user_site:
                command.append('-s')
            command.extend([str(Path(__file__).resolve()), artifact.directory, source, str(archive)])
            code = subprocess.run(command, timeout=remaining, capture_output=True, env=env).returncode
        except subprocess.TimeoutExpired:
            code = _NETWORK
            budgets[source] = 0
        except OSError:
            code = _DISK
        elapsed = time.monotonic() - start
        budgets[source] = max(0, min(budgets[source], remaining - elapsed))
        if code == 0:
            return archive
        archive.unlink(missing_ok=True)
        if code == _NETWORK:
            reason = '已到下载时限' if not budgets[source] else '连接或传输失败'
            print(f'[浏览器下载警告] {artifact.directory} | {source} | {reason}', flush=True)
            continue
        reason = {_INTEGRITY: '归档长度或 SHA-256 校验失败', _SECURITY: '重定向校验失败', _TLS: 'TLS 校验失败',
                  _DISK: '无法写入下载暂存目录'}.get(code, '下载进程异常')
        raise InstallError(f'{artifact.directory}（{source}）{reason}。')
    raise InstallError(f'{artifact.directory} 下载失败，所选来源不可用或超过下载时限。')


def _extract(archive: Path, directory: Path, artifact: Artifact) -> None:
    seen = set()
    try:
        with ZipFile(archive) as package:
            for member in package.infolist():
                parts = member.filename.removesuffix('/').split('/')
                mode = stat.S_IFMT(member.external_attr >> 16)
                key = tuple(part.casefold() for part in parts)
                if (key in seen or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or any(part in {'', '.', '..'} or part.endswith(('.', ' '))
                               or any(ch in '\\:<>"|?*' or ord(ch) < 32 for ch in part)
                               or PureWindowsPath(part).is_reserved() for part in parts)):
                    raise InstallError(f'{artifact.directory} 归档包含不安全或重复的路径。')
                seen.add(key)
                target = directory.joinpath(*parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as content, target.open('xb') as output:
                        shutil.copyfileobj(content, output)
        if not (directory / artifact.executable).is_file():
            raise InstallError(f'{artifact.directory} 缺少预期可执行文件。')
        (directory / 'INSTALLATION_COMPLETE').touch(exist_ok=False)
    except (BadZipFile, OSError, RuntimeError, NotImplementedError) as error:
        if isinstance(error, InstallError):
            raise
        raise InstallError(f'{artifact.directory} 无法解包到暂存目录。') from None


def stage_install(stage: Path, source: str, seconds: int) -> None:
    sources = ('official', 'npmmirror') if source == 'auto' else (source,)
    budgets = dict.fromkeys(sources, float(seconds))
    for artifact in ARTIFACTS:
        archive = _download_archive(artifact, stage, budgets)
        _extract(archive, stage / artifact.directory, artifact)
        archive.unlink()


if __name__ == '__main__':
    name, source, destination = sys.argv[1:]
    artifact = next(item for item in ARTIFACTS if item.directory == name)
    sys.exit(_download(artifact, source, Path(destination)))
