"""
Build script for standalone Windows x64 executable (GTNH_Machine_Hull.exe).

Usage:
  python build_exe.py             # Release onefile build (--noconsole)
  python build_exe.py --debug     # Console enabled for debugging
  python build_exe.py --onedir    # Folder distribution instead of single-file
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE_NAME = "GTNH_Machine_Hull"
DIST_DIR = os.path.join(ROOT, "dist")
BUILD_DIR = os.path.join(ROOT, "build")


def _collect_local_secrets() -> list[str]:
    """Read local bisquit_config.json strictly to verify none of its secrets leak into dist/ or .exe."""
    secrets = []
    cfg_path = os.path.join(ROOT, "bisquit_config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k in ("api_key", "server_id"):
                val = str(data.get(k) or "").strip()
                if len(val) >= 6:
                    secrets.append(val)
        except Exception:
            pass
    return secrets


def verify_no_secrets_leaked(exe_path: str) -> None:
    """Ensure neither dist/ nor the built executable contains any local config files or credentials."""
    forbidden_names = {"bisquit_config.json", "gtnh_updater_profile.json"}
    for root, _, files in os.walk(DIST_DIR):
        for fn in files:
            if fn.lower() in forbidden_names:
                raise RuntimeError(f"SECURITY FAIL: forbidden personal file found in dist/: {os.path.join(root, fn)}")

    secrets = _collect_local_secrets()
    if secrets and os.path.isfile(exe_path):
        with open(exe_path, "rb") as fh:
            blob = fh.read()
        for sec in secrets:
            if sec.encode("utf-8") in blob or sec.encode("utf-16-le") in blob:
                raise RuntimeError("SECURITY FAIL: local secret detected inside built executable!")
    print(f"[SECURITY OK] Verified zero personal configs in dist/ and zero leaked secrets in {os.path.basename(exe_path)}.")


def build(debug: bool = False, onedir: bool = False) -> str:
    ico_path = os.path.join(ROOT, "assets", "icon.ico")
    ui_dir = os.path.join(ROOT, "ui")
    assets_dir = os.path.join(ROOT, "assets")
    entry_script = os.path.join(ROOT, "main.py")

    for required in (ico_path, ui_dir, assets_dir, entry_script):
        if not os.path.exists(required):
            raise FileNotFoundError(f"Required build input missing: {required}")

    # Clean previous dist/build
    for d in (DIST_DIR, BUILD_DIR):
        if os.path.isdir(d):
            shutil.rmtree(d)

    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir" if onedir else "--onefile",
        "--console" if debug else "--noconsole",
        "--name", EXE_NAME,
        "--icon", ico_path,
        "--add-data", f"{ui_dir}{sep}ui",
        "--add-data", f"{assets_dir}{sep}assets",
        "--collect-all", "webview",
        "--collect-all", "clr_loader",
        "--hidden-import", "clr",
        "--hidden-import", "pythonnet",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "cefpython3",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "PySide2",
        "--exclude-module", "PySide6",
        "--exclude-module", "qtpy",
        "--exclude-module", "wx",
        "--exclude-module", "PIL",
        "--exclude-module", "numpy",
        entry_script,
    ]

    print("[BUILD] Running PyInstaller:", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, cwd=ROOT)
    if res.returncode != 0:
        raise RuntimeError(f"PyInstaller failed with exit code {res.returncode}")

    exe_path = (
        os.path.join(DIST_DIR, EXE_NAME, f"{EXE_NAME}.exe")
        if onedir
        else os.path.join(DIST_DIR, f"{EXE_NAME}.exe")
    )
    if not os.path.isfile(exe_path):
        raise FileNotFoundError(f"Expected executable not found: {exe_path}")

    verify_no_secrets_leaked(exe_path)

    # Remove intermediate spec file if desired or keep clean
    spec_file = os.path.join(ROOT, f"{EXE_NAME}.spec")
    if os.path.isfile(spec_file):
        os.remove(spec_file)

    size_mb = os.path.getsize(exe_path) / (1024 * 1024)
    print(f"[BUILD OK] {exe_path} ({size_mb:.1f} MB)", flush=True)
    return exe_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build standalone GTNH_Machine_Hull.exe")
    parser.add_argument("--debug", action="store_true", help="Keep console window visible for debugging")
    parser.add_argument("--onedir", action="store_true", help="Build directory bundle instead of single .exe")
    args = parser.parse_args()
    build(debug=args.debug, onedir=args.onedir)


if __name__ == "__main__":
    main()
