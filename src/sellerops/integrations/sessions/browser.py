"""Explicit login windows using SellerOps' pinned Chromium, isolated from daily browsing."""
from contextlib import contextmanager
from importlib import metadata
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from sellerops.core.errors import AuthenticationError
from sellerops.core.files import FileLock
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
def _driver():
    _check_package()
    from playwright.sync_api import sync_playwright

    # Only the Playwright child process needs this setting. Restore the caller's environment.
    driver = sync_playwright()
    previous = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(paths.BROWSER_RUNTIME_DIR)
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


def setup() -> None:
    _check_package()
    with FileLock(paths.BROWSER_RUNTIME_DIR.parent / 'browser-setup', timeout=5):
        with _driver() as browser:
            installed_version = _installed_version(browser)
            if installed_version != CHROMIUM_VERSION:
                env = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(paths.BROWSER_RUNTIME_DIR)}
                # Project installation must use the same driver and official artifact source.
                for key in ('PLAYWRIGHT_DOWNLOAD_HOST', 'PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST'):
                    env.pop(key, None)
                command = [sys.executable, '-m', 'playwright', 'install', '--no-shell', 'chromium']
                if installed_version is not None:
                    command.append('--force')
                try:
                    subprocess.run(command, env=env, check=True, timeout=600)
                except (OSError, subprocess.SubprocessError):
                    raise AuthenticationError('专用 Chromium 安装失败，请检查网络后重新运行 login setup。') from None
                _check_version(_installed_version(browser) or '')
    print(f'[已就绪] 专用 Chromium {CHROMIUM_VERSION}（构建 {CHROMIUM_BUILD}）')


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
