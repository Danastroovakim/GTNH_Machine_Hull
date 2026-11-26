import ctypes
import io
import os
import posixpath
import re
import shutil
import stat
import sys
import threading
import time
from datetime import datetime

from app.constants import (
    APP_USER_MODEL_ID,
    CHANGELOG_RE,
    GTNH_DIM_NAMES,
    MSK_TZ,
)


def configure_utf8_stdio() -> None:
    """Prevent UnicodeEncodeError on Windows cp1251 consoles."""
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def set_windows_app_id(app_id: str = APP_USER_MODEL_ID) -> None:
    """Ensure Windows taskbar uses our custom app icon instead of python.exe."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def apply_window_chrome(window_title: str, ico_path: str = "") -> None:
    """Apply dark titlebar (Win10/11) and custom Win32 window/taskbar icon."""
    def _worker():
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040

        user32.LoadImageW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.FindWindowW.restype = ctypes.c_void_p

        for _ in range(45):
            time.sleep(0.12)
            try:
                hwnd = user32.FindWindowW(None, window_title)
                if not hwnd:
                    continue

                # Dark mode caption bar (Win10 19045+ & Win11) + force non-client frame repaint
                val = ctypes.c_int(1)
                dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
                dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), ctypes.sizeof(val))

                # Subtle dark titlebar color on Win11 (#16181D -> COLORREF 0x001D1816)
                caption_color = ctypes.c_int(0x001D1816)
                dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))

                # Titlebar + taskbar icons
                if ico_path and os.path.isfile(ico_path):
                    h_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                    h_big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 48, 48, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                    if h_small:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
                    if h_big:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)

                # Force Windows 10/11 to immediately redraw the dark non-client titlebar
                SWP_NOMOVE = 0x0002
                SWP_NOSIZE = 0x0001
                SWP_NOZORDER = 0x0004
                SWP_FRAMECHANGED = 0x0020
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
                break
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def format_size(size_bytes) -> str:
    try:
        size_bytes = int(size_bytes or 0)
    except (ValueError, TypeError):
        return "0 B"
    if size_bytes <= 0:
        return "0 B"
    gb = size_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = size_bytes / (1024 ** 2)
    if mb >= 0.1:
        return f"{mb:.1f} MB"
    kb = size_bytes / 1024
    if kb >= 0.1:
        return f"{kb:.1f} KB"
    return f"{size_bytes} B"


def format_msk_time(iso_str, filename_hint: str = "") -> str:
    if filename_hint:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[-_](\d{2})-(\d{2})-(\d{2})", filename_hint)
        if m:
            y, mo, d, h, mi, s = m.groups()
            return f"{d}.{mo}.{y} {h}:{mi}:{s}"
    if not iso_str:
        return "Неизвестно"
    try:
        clean = str(iso_str).strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is not None:
            dt = dt.astimezone(MSK_TZ)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(iso_str)[:19].replace("T", " ")


def is_dimension_folder(name: str) -> bool:
    if not name:
        return False
    return name.startswith("DIM") or name.startswith("PERSONAL_DIM")


def is_personal_dimension(name: str) -> bool:
    if not name:
        return False
    return name.startswith("PERSONAL_DIM") or name in ("DIM180", "DIM181")


def format_dim_label(d_name: str) -> str:
    if is_personal_dimension(d_name):
        return f"{d_name} (Личное измерение PersonalSpace)"
    if d_name in GTNH_DIM_NAMES:
        return f"{d_name} — {GTNH_DIM_NAMES[d_name]}"
    return d_name


def is_disabled_mod(filename: str) -> bool:
    return str(filename or "").lower().endswith(".disabled")


def extract_mod_base_key(filename: str) -> str:
    """
    Normalize GTNH / 1.7.10 mod jar name into a stable base key.
    Examples:
      gregtech-5.09.51.102.jar -> gregtech
      appliedenergistics2-rv3-beta-438-GTNH.jar -> appliedenergistics2
      [1.7.10]bspkrsCore-universal-6.16.jar -> bspkrscore
    """
    base_name = posixpath.basename(str(filename).replace("\\", "/"))
    stem = re.sub(r"\.(jar|zip|litemod)(\.disabled)?$", "", base_name, flags=re.IGNORECASE)
    stem = re.sub(r"^[\[\(]?(?:mc)?1\.7\.10[\]\)]?[\s\-_\+]*", "", stem, flags=re.IGNORECASE)

    m = re.search(
        r"[\-_\s\+]+(?:mc\d|rv\d|v\d|r\d|\d+(?:\.\d+|[\-_a-zA-Z]|$))",
        stem,
        flags=re.IGNORECASE,
    )
    raw_base = stem[:m.start()] if (m and m.start() >= 2) else stem
    raw_base = re.sub(
        r"(?:[\-_\s\+]+(?:universal|forge|gtnh|mod|jar|client|server|all))+$",
        "",
        raw_base,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"[^a-z0-9]+", "", raw_base.lower())
    return normalized or stem.lower()


def is_changelog_file(filename: str) -> bool:
    bn = posixpath.basename(str(filename).replace("\\", "/")).lower()
    return bn.startswith("changelog from ") and bn.endswith(".md")


def parse_changelog_version(filename: str) -> tuple[str | None, str | None]:
    bn = posixpath.basename(str(filename).replace("\\", "/"))
    m = CHANGELOG_RE.match(bn)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def is_server_core_jar(filename: str) -> bool:
    bn = posixpath.basename(str(filename).replace("\\", "/")).lower()
    if not bn.endswith(".jar"):
        return False
    return (
        bn.startswith("forge-")
        or bn.startswith("lwjgl3ify-forgepatches")
        or bn.startswith("minecraft_server.")
    )


def normalize_config_relpath(path_str: str) -> str:
    p = str(path_str or "").strip().replace("\\", "/")
    p = p.lstrip("/")
    if p.lower().startswith("config/"):
        p = p[7:]
    p = p.strip("/")
    if not p or ".." in p.split("/"):
        return ""
    return p


def is_file_actively_locked(filepath: str) -> bool:
    """Return True if another process holds an exclusive/write lock on filepath."""
    if not filepath or not os.path.exists(filepath):
        return False
    if os.name == "nt":
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
            k32.CreateFileW.restype = ctypes.c_void_p
            handle = k32.CreateFileW(
                filepath, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
            )
            if handle == INVALID_HANDLE_VALUE or handle is None:
                err = ctypes.get_last_error()
                return err in (32, 33)
            k32.CloseHandle(ctypes.c_void_p(handle))
            return False
        except Exception:
            return False
    try:
        import fcntl
        with open(filepath, "r+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except (IOError, OSError):
        return True


def rmtree_force(path: str) -> None:
    """Remove directory tree even when Windows read-only bits are set."""
    if not os.path.exists(path):
        return

    def _on_exc(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except Exception:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_exc)
    else:
        shutil.rmtree(path, onerror=_on_exc)


def safe_extract_zip_entry(zf, info, dest_path: str, allowed_root: str) -> None:
    """Extract a single ZipInfo safely with ZipSlip protection."""
    abs_root = os.path.abspath(allowed_root)
    abs_dest = os.path.abspath(dest_path)
    if os.path.commonpath([abs_root, abs_dest]) != abs_root:
        raise RuntimeError(f"Обнаружена небезопасная траектория пути в архиве (ZipSlip): {info.filename}")
    os.makedirs(os.path.dirname(abs_dest), exist_ok=True)
    with zf.open(info, "r") as src, open(abs_dest, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


def calc_dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


class HttpRangeFile(io.IOBase):
    """Seekable read-only stream over HTTP Range requests to inspect remote ZIP central directories."""

    def __init__(self, session, url: str, timeout: int = 15):
        self._session = session
        self._url = url
        self._timeout = timeout
        self._pos = 0
        self._size = self._probe_size()

    def _probe_size(self) -> int:
        r = self._session.get(
            self._url,
            headers={"Range": "bytes=0-0"},
            timeout=self._timeout,
            allow_redirects=True,
            stream=True,
        )
        try:
            if r.status_code == 206:
                cr = r.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)$", cr)
                if m:
                    return int(m.group(1))
            raise RuntimeError(f"Хост не поддерживает HTTP Range-запросы (HTTP {r.status_code})")
        finally:
            r.close()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = max(0, offset)
        elif whence == io.SEEK_CUR:
            self._pos = max(0, self._pos + offset)
        elif whence == io.SEEK_END:
            self._pos = max(0, self._size + offset)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self._pos >= self._size:
            return b""
        if size is None or size < 0:
            end = min(self._size - 1, self._pos + (4 * 1024 * 1024) - 1)
        else:
            if size == 0:
                return b""
            end = min(self._size - 1, self._pos + size - 1)
        if end < self._pos:
            return b""
        r = self._session.get(
            self._url,
            headers={"Range": f"bytes={self._pos}-{end}"},
            timeout=self._timeout,
            allow_redirects=True,
        )
        if r.status_code not in (200, 206):
            raise IOError(f"Ошибка HTTP Range чтения: HTTP {r.status_code}")
        data = r.content
        if r.status_code == 200:
            data = data[self._pos:end + 1]
        self._pos += len(data)
        return data
