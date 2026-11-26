import ctypes
import os
import sys
import webview

from app.bridge import MachineHullBridge
from app.constants import APP_NAME, APP_WINDOW_TITLE
from app.paths import resource_path
from app.utils import apply_window_chrome, configure_utf8_stdio, set_windows_app_id


def _show_error_dialog(title: str, message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"[FATAL] {title}: {message}", file=sys.stderr, flush=True)


def _check_webview2_installed() -> bool:
    """Quick registry check for Microsoft Edge WebView2 Runtime on Windows."""
    if os.name != "nt":
        return True
    try:
        import winreg
        guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}"),
        ]
        for hive, subkey in keys:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    pv, _ = winreg.QueryValueEx(k, "pv")
                    if pv and pv != "0.0.0.0":
                        return True
            except OSError:
                continue
        return False
    except Exception:
        return True


def main() -> None:
    configure_utf8_stdio()
    set_windows_app_id()

    debug_mode = "--debug" in sys.argv
    if not _check_webview2_installed():
        _show_error_dialog(
            APP_NAME,
            "Не обнаружен Microsoft Edge WebView2 Runtime.\n\n"
            "Установите WebView2 Runtime с официального сайта Microsoft "
            "(https://developer.microsoft.com/microsoft-edge/webview2/) и перезапустите приложение.",
        )
        return

    index_html = resource_path("ui", "index.html")
    ico_path = resource_path("assets", "icon.ico")

    if not os.path.isfile(index_html):
        _show_error_dialog(APP_NAME, f"Не найден файл интерфейса:\n{index_html}")
        return

    bridge = MachineHullBridge()
    apply_window_chrome(APP_WINDOW_TITLE, ico_path)

    webview.create_window(
        title=APP_WINDOW_TITLE,
        url=index_html,
        js_api=bridge,
        width=1280,
        height=800,
        min_size=(1060, 680),
        resizable=True,
        background_color="#16181D",
    )

    try:
        webview.start(debug=debug_mode, http_server=True)
    except Exception as e:
        _show_error_dialog(
            APP_NAME,
            f"Ошибка запуска окна WebView2:\n{e}\n\n"
            "Убедитесь, что в системе установлен Microsoft Edge WebView2 Runtime.",
        )


if __name__ == "__main__":
    main()
