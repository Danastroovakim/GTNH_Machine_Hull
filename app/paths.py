import os
import sys
import tempfile


def is_frozen() -> bool:
    """True when running inside a PyInstaller / frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def _resolve_resource_dir() -> str:
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return os.path.abspath(sys._MEIPASS)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_dir_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".gtnh_hull_probe_", dir=path)
        os.close(fd)
        os.remove(probe)
        return True
    except Exception:
        return False


def _resolve_data_dir() -> tuple[str, bool]:
    """
    Returns (data_dir, is_portable).
    In frozen mode: prefer directory next to .exe (portable mode) if writable,
    otherwise fall back to %APPDATA%\\GTNHMachineHull.
    In dev mode: project root directory.
    """
    if not is_frozen():
        return _resolve_resource_dir(), True

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if _is_dir_writable(exe_dir):
        return exe_dir, True

    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    fallback = os.path.join(appdata, "GTNHMachineHull")
    os.makedirs(fallback, exist_ok=True)
    return fallback, False


RESOURCE_DIR = _resolve_resource_dir()
DATA_DIR, IS_PORTABLE_MODE = _resolve_data_dir()

CONFIG_FILE = os.path.join(DATA_DIR, "bisquit_config.json")
UPDATER_PROFILE_FILE = os.path.join(DATA_DIR, "gtnh_updater_profile.json")


def resource_path(*parts: str) -> str:
    return os.path.join(RESOURCE_DIR, *parts)


def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)
