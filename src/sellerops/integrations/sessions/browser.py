"""Explicit login windows using SellerOps' pinned Chromium, isolated from daily browsing."""
from contextlib import contextmanager
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from sellerops.core.errors import AuthenticationError
from sellerops.core.files import FileLock
from sellerops.integrations.sessions import browser_install
from sellerops.settings import paths


PLAYWRIGHT_VERSION = '1.65.2'
CHROMIUM_VERSION = '159.0.8154.6'
CHROMIUM_BUILD = '1255'
INSTALL_HELP = '请先安装项目的 browser-login 依赖，再运行 login setup。'


def profile_dir(platform: str) -> Path:
    return Path(paths.current.browser_profiles_dir) / platform


def is_site(url: str, host: str) -> bool:
    address = urlsplit(url)
    return (address.scheme == 'https' and address.hostname == host and address.port in (None, 443)
            and not address.username and not address.password)


def url_without_query(url: str) -> str:
    return url.split('?', 1)[0]


def platform_page(context, host: str):
    return next((page for page in reversed(context.pages) if is_site(page.url, host)), None) or (
        context.pages[0] if context.pages else context.new_page())


def cookie_header(context, url: str) -> str:
    return '; '.join(f"{row['name']}={row['value']}" for row in context.cookies([url]))


def _check_package() -> None:
    try:
        version = metadata.version('playwright')
    except metadata.PackageNotFoundError:
        version = None
    if version != PLAYWRIGHT_VERSION:
        raise AuthenticationError(f'专用浏览器需要 Playwright {PLAYWRIGHT_VERSION}。{INSTALL_HELP}')


@contextmanager
def _driver(runtime: Path | None = None):
    _check_package()
    from playwright.sync_api import sync_playwright

    # Only the Playwright child process needs this setting. Restore the caller's environment.
    driver = sync_playwright()
    previous = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(runtime or paths.BROWSER_RUNTIME_DIR)
    try:
        engine = driver.start()
    finally:
        if previous is None:
            os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
        else:
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = previous
    try:
        yield engine.chromium
    finally:
        engine.stop()


def _executable(browser) -> Path:
    return Path(browser.executable_path)


def _check_version(version: str) -> None:
    if version != CHROMIUM_VERSION:
        raise AuthenticationError(f'专用 Chromium 版本不匹配，需要 {CHROMIUM_VERSION}。{INSTALL_HELP}')


def _installed_version(browser) -> str | None:
    path = _executable(browser)
    if f'chromium-{CHROMIUM_BUILD}' not in path.parts:
        raise AuthenticationError(f'专用 Chromium 构建号不匹配。{INSTALL_HELP}')
    if not path.is_file():
        return None
    from playwright.sync_api import Error

    try:
        instance = browser.launch(channel='chromium', executable_path=path, chromium_sandbox=True, timeout=15000)
        try:
            return instance.version
        finally:
            instance.close()
    except Error:
        raise AuthenticationError('无法启动专用 Chromium，请检查本机浏览器运行环境。') from None


def _publish_install(stage: Path, runtime: Path) -> None:
    backup = runtime.with_name(f'.{runtime.name}-previous-{stage.parent.name}')
    had_runtime = runtime.exists()
    if had_runtime:
        runtime.replace(backup)
    try:
        stage.replace(runtime)
    except OSError:
        if had_runtime:
            try:
                backup.replace(runtime)
            except OSError:
                raise browser_install.InstallError(f'发布专用 Chromium 失败，原运行目录保留在 {backup.name}。') from None
        raise
    if had_runtime:
        shutil.rmtree(backup, ignore_errors=True)


def _setup_verified(runtime: Path, source: str, seconds: int) -> None:
    if browser_install.complete(runtime) and browser_install.has_receipt(runtime, PLAYWRIGHT_VERSION):
        with _driver(runtime) as engine:
            try:
                if _installed_version(engine) == CHROMIUM_VERSION:
                    return
            except AuthenticationError:
                pass  # Repair through the same verified staging path.
    with TemporaryDirectory(prefix='.browser-setup-', dir=runtime.parent) as directory:
        stage = Path(directory) / 'browsers'
        stage.mkdir()
        browser_install.stage_install(stage, source, seconds)
        with _driver(stage) as engine:
            expected = browser_install.ARTIFACTS[0]
            if _executable(engine) != stage / expected.directory / expected.executable:
                raise AuthenticationError(f'专用 Chromium 构建路径不匹配。{INSTALL_HELP}')
            _check_version(_installed_version(engine) or '')
        receipt = browser_install.expected_receipt(PLAYWRIGHT_VERSION)
        (stage / browser_install.RECEIPT).write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        _publish_install(stage, runtime)


def _setup_playwright(runtime: Path, source: str, seconds: int) -> None:
    if source == 'npmmirror':
        raise browser_install.InstallError('当前平台没有校验过的国内镜像产物，请使用 official。')
    with _driver(runtime) as engine:
        installed = _installed_version(engine)
        if installed == CHROMIUM_VERSION:
            return
        env = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(runtime),
               'PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT': '20000'}
        for key in ('PLAYWRIGHT_DOWNLOAD_HOST', 'PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST', 'npm_config_playwright_download_host',
                    'npm_config_playwright_chromium_download_host', 'npm_package_config_playwright_download_host',
                    'npm_package_config_playwright_chromium_download_host', 'NODE_TLS_REJECT_UNAUTHORIZED'):
            env.pop(key, None)
        command = [sys.executable, '-m', 'playwright', 'install', '--no-shell', 'chromium']
        if installed is not None:
            command.append('--force')
        subprocess.run(command, env=env, check=True, timeout=seconds, capture_output=True)
        _check_version(_installed_version(engine) or '')


def setup() -> None:
    _check_package()
    runtime = paths.BROWSER_RUNTIME_DIR
    try:
        source, seconds = browser_install.options()
        with FileLock(runtime.parent / 'browser-setup', timeout=5):
            if browser_install.supported():
                _setup_verified(runtime, source, seconds)
            else:
                _setup_playwright(runtime, source, seconds)
    except browser_install.InstallError as error:
        raise AuthenticationError(str(error)) from None
    except (OSError, subprocess.SubprocessError):
        raise AuthenticationError('专用 Chromium 安装失败，请检查网络、磁盘或专用浏览器是否仍在使用。') from None
    print(f'[已就绪] 专用 Chromium {CHROMIUM_VERSION}（构建 {CHROMIUM_BUILD}）')


def installation_status() -> dict:
    runtime = paths.BROWSER_RUNTIME_DIR
    windows = browser_install.supported()
    result = {'status': 'package_missing', 'playwright_version': None, 'expected_playwright_version': PLAYWRIGHT_VERSION,
              'chromium': {'expected_version': CHROMIUM_VERSION, 'expected_build': CHROMIUM_BUILD,
                           'manifest_version': None, 'manifest_build': None, 'installed_build': None,
                           'installed_builds': [],
                           'executable_present': None, 'install_marker_present': None,
                           'integrity_verified': False, 'runtime_version': None}}
    chromium = result['chromium']
    directory = runtime / f'chromium-{CHROMIUM_BUILD}'
    installed = sorted(path.name.removeprefix('chromium-') for path in runtime.glob('chromium-*')
                       if re.fullmatch(r'chromium-[0-9]+', path.name) and path.is_dir())
    chromium['installed_builds'] = installed
    if CHROMIUM_BUILD in installed:
        chromium['installed_build'] = CHROMIUM_BUILD
    elif len(installed) == 1:
        chromium['installed_build'] = installed[0]
    chromium['install_marker_present'] = (directory / 'INSTALLATION_COMPLETE').is_file()
    if windows:
        chromium['executable_present'] = (directory / browser_install.ARTIFACTS[0].executable).is_file()
        chromium['integrity_verified'] = (browser_install.complete(runtime)
                                          and browser_install.has_receipt(runtime, PLAYWRIGHT_VERSION))
    try:
        bundle = metadata.distribution('playwright')
    except metadata.PackageNotFoundError:
        return result
    version = bundle.version
    result['playwright_version'] = version if isinstance(version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', version) else None
    if version != PLAYWRIGHT_VERSION:
        result['status'] = 'package_mismatch'
        return result
    try:
        manifest = bundle.locate_file('playwright/driver/package/browsers.json')
        entry = next(row for row in json.loads(manifest.read_text(encoding='utf-8'))['browsers']
                     if row['name'] == 'chromium')
        version, build = entry['browserVersion'], entry['revision']
        chromium['manifest_version'] = version if isinstance(version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', version) else None
        chromium['manifest_build'] = build if isinstance(build, str) and build.isascii() and build.isdecimal() else None
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        result['status'] = 'manifest_unreadable'
        return result
    if (version, build) != (CHROMIUM_VERSION, CHROMIUM_BUILD):
        result['status'] = 'build_mismatch'
    elif installed and CHROMIUM_BUILD not in installed:
        result['status'] = 'installed_build_mismatch'
    elif not chromium['install_marker_present'] or chromium['executable_present'] is False:
        result['status'] = 'chromium_missing'
    elif not chromium['integrity_verified']:
        result['status'] = 'installed_unverified'
    else:
        result['status'] = 'ready'
    return result


@contextmanager
def open_profile(platform: str):
    _check_package()
    from playwright.sync_api import Error, TimeoutError

    with _driver() as browser:
        executable = _executable(browser)
        if not executable.is_file() or f'chromium-{CHROMIUM_BUILD}' not in executable.parts:
            raise AuthenticationError(f'专用 Chromium 缺失或构建号不匹配。{INSTALL_HELP}')
        try:
            context = browser.launch_persistent_context(profile_dir(platform), executable_path=executable,
                                                        headless=False, chromium_sandbox=True,
                                                        timeout=20000, accept_downloads=False)
        except Error:
            raise AuthenticationError('无法打开专用 Chromium。请关闭占用此资料的窗口后重试。') from None
        try:
            # A persistent context can have no Browser object. The bundled browser exposes its version here.
            page = context.pages[0] if context.pages else context.new_page()
            session = context.new_cdp_session(page)
            try:
                _check_version(session.send('Browser.getVersion')['product'].rsplit('/', 1)[-1])
            finally:
                session.detach()
            yield context
        except TimeoutError:
            raise AuthenticationError('专用浏览器加载或等待平台页面超时，原凭证保留。') from None
        except Error as exc:
            network_error = re.search(r'\bnet::ERR_[A-Z0-9_]+\b', exc.message)
            detail = f'（{network_error[0]}）' if network_error else ''
            raise AuthenticationError(f'专用浏览器登录流程中断{detail}，原凭证保留。') from None
        finally:
            try:
                context.close()
            except Error:
                pass  # A user may already have closed the window.


def wait_for_window_close(context, *, timeout_ms: int) -> None:
    from playwright.sync_api import Error, TimeoutError

    if not context.pages:
        return
    closed = False

    def mark_closed(*_) -> None:
        nonlocal closed
        closed = True

    context.on('close', mark_closed)
    try:
        context.wait_for_event('close', timeout=timeout_ms)
    except TimeoutError:
        raise AuthenticationError('登录窗口等待超时。重新连接或用 --resume 检查已保存的登录态。') from None
    except Error:
        if not closed:
            raise
