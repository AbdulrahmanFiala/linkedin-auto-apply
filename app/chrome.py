from __future__ import annotations

import errno
import ctypes
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

from .config import Config

logger = logging.getLogger(__name__)


class ChromeStartupError(RuntimeError):
    pass


@dataclass(slots=True)
class ChromeHandle:
    process: subprocess.Popen | None
    launched_by_app: bool
    app_profile_lock_file: Path | None = None

    def terminate_if_needed(self, keep_open: bool) -> None:
        if self.process and self.launched_by_app and not keep_open:
            logger.info("Terminating Chrome process launched by app")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.app_profile_lock_file:
            _remove_profile_lock_if_owned(self.app_profile_lock_file)


def _can_connect(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.5)
        return sock.connect_ex((host, port)) == 0


def detect_chrome_executable(override_path: Optional[str] = None) -> str:
    if override_path:
        path = Path(override_path)
        if not path.exists():
            raise ChromeStartupError(f"CHROME_EXECUTABLE_PATH does not exist: {path}")
        return str(path)

    candidates = []
    if os.name == "nt":
        candidates.extend(
            [
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            ]
        )
    else:
        for name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
            resolved = shutil.which(name)
            if resolved:
                candidates.append(Path(resolved))

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise ChromeStartupError(
        "Could not find Google Chrome executable. Set CHROME_EXECUTABLE_PATH in .env"
    )


def wait_for_cdp(cdp_url: str, timeout_seconds: int = 30) -> None:
    version_url = cdp_url.rstrip("/") + "/json/version"
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None

    while time.time() < deadline:
        try:
            with urlopen(version_url, timeout=2) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("Browser"):
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)

    raise ChromeStartupError(f"CDP endpoint not available at {version_url}: {last_error}")


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        process_handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process_handle:
            # ERROR_INVALID_PARAMETER means the PID does not exist. Other errors
            # (such as access denied) are treated as running to avoid stealing a live lock.
            return ctypes.get_last_error() != 87
        try:
            exit_code = ctypes.c_ulong()
            if not get_exit_code(process_handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(process_handle)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # On Windows, a PID that does not exist can be reported as winerror 87.
        if getattr(exc, "winerror", None) == 87:
            return False
        raise


def _remove_profile_lock_if_owned(lock_file: Path) -> None:
    try:
        owner_pid = int(lock_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if owner_pid == os.getpid():
        lock_file.unlink(missing_ok=True)


def _create_profile_lock_file(user_data_dir: Path) -> Path:
    lock_file = user_data_dir / ".app-profile.lock"
    for attempt in range(2):
        try:
            # O_EXCL makes checking and creating the lock a single atomic operation.
            descriptor = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as lock_handle:
                lock_handle.write(str(os.getpid()))
                lock_handle.flush()
            return lock_file
        except FileExistsError:
            try:
                observed = lock_file.stat()
                owner_text = lock_file.read_text(encoding="utf-8").strip()
                owner_pid = int(owner_text)
            except (OSError, ValueError) as exc:
                raise ChromeStartupError(
                    f"Profile launch lock at {lock_file} is unreadable. Remove it only after confirming no app instance is launching Chrome for this profile."
                ) from exc

            if _pid_is_running(owner_pid):
                raise ChromeStartupError(
                    f"Profile launch lock exists at {lock_file} (PID {owner_pid} is still running). Another app instance may be launching Chrome for this profile."
                )

            logger.warning("Removing stale Chrome profile launch lock for exited PID %d: %s", owner_pid, lock_file)
            try:
                current = lock_file.stat()
                current_pid = int(lock_file.read_text(encoding="utf-8").strip())
                if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino) or current_pid != owner_pid:
                    continue
                lock_file.unlink()
            except FileNotFoundError:
                pass

    raise ChromeStartupError(f"Could not acquire Chrome profile launch lock at {lock_file}")


def ensure_chrome_running(config: Config, open_linkedin: bool = False) -> ChromeHandle:
    config.chrome_user_data_dir.mkdir(parents=True, exist_ok=True)

    if _can_connect("127.0.0.1", config.chrome_debug_port):
        logger.info("Using existing Chrome CDP endpoint at %s", config.chrome_cdp_url)
        wait_for_cdp(config.chrome_cdp_url)
        return ChromeHandle(process=None, launched_by_app=False)

    executable = detect_chrome_executable(config.chrome_executable_path)
    lock_file = _create_profile_lock_file(config.chrome_user_data_dir)
    args = [
        executable,
        f"--remote-debugging-port={config.chrome_debug_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={config.chrome_user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    if open_linkedin:
        args.append("https://www.linkedin.com")

    logger.info("Launching Chrome with user data dir: %s", config.chrome_user_data_dir)
    try:
        process = subprocess.Popen(args)
    except OSError as exc:
        _remove_profile_lock_if_owned(lock_file)
        raise ChromeStartupError(f"Failed to launch Chrome: {exc}") from exc

    try:
        wait_for_cdp(config.chrome_cdp_url)
    except ChromeStartupError as exc:
        process.terminate()
        _remove_profile_lock_if_owned(lock_file)
        raise ChromeStartupError(
            "Chrome failed to start CDP. This can happen if the profile is locked by another Chrome process. "
            "Close other Chrome instances using this profile and retry."
        ) from exc

    return ChromeHandle(process=process, launched_by_app=True, app_profile_lock_file=lock_file)
