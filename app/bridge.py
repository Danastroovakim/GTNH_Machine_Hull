import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
import webview

from app.api.backups import BackupService
from app.api.bisquit_client import BisquitClient
from app.api.configs import ConfigStudioService
from app.api.ws_console import ConsoleTelemetryService
from app.config_store import ConfigStore
from app.constants import APP_NAME, APP_VERSION
from app.paths import DATA_DIR, IS_PORTABLE_MODE
from app.updater.analyzer import analyze_pack_update
from app.updater.client_updater import run_client_update
from app.updater.prism_finder import discover_prism_instances
from app.updater.server_updater import run_server_update
from app.utils import format_size, is_personal_dimension, normalize_config_relpath


class MachineHullBridge:
    """Facade exposed to PyWebView JS (`window.pywebview.api`)."""

    def __init__(self, store: ConfigStore | None = None):
        self.store = store or ConfigStore()
        self._ui_ready = False
        self._busy_lock = threading.Lock()
        self._ui_log_queue: deque = deque(maxlen=2500)
        self._ui_log_lock = threading.Lock()

        self.client = BisquitClient(self.store, log_fn=self.push_log)
        self.telemetry = ConsoleTelemetryService(self.client, self._queue_batch_for_ui, self.push_log)
        self.backups = BackupService(
            self.client,
            self.telemetry,
            self.store,
            log_fn=self.push_log,
            progress_fn=self.emit_progress,
            busy_guard=self._busy_lock,
            ui_eval_fn=self._eval_js,
        )
        self.configs = ConfigStudioService(self.client, log_fn=self.push_log)

        print(f"[SYSTEM] {APP_NAME} v{APP_VERSION} готов. DATA_DIR={DATA_DIR}", flush=True)
        threading.Thread(target=self._ui_flusher_loop, daemon=True).start()

    # --- JS push helpers ---
    def _eval_js(self, code: str) -> None:
        if not self._ui_ready or not webview.windows:
            return
        try:
            webview.windows[0].evaluate_js(code)
        except Exception:
            pass

    def push_log(self, message: str, source: str = "SYSTEM", msg_type: str = "info") -> None:
        now = datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] [{source}] {message}"
        try:
            print(line, flush=True)
        except Exception:
            try:
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                print(line.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)
            except Exception:
                pass
        self._queue_batch_for_ui([{"time": now, "msg": line, "source": source, "type": msg_type}])

    def _queue_batch_for_ui(self, batch: list[dict]) -> None:
        if not batch:
            return
        with self._ui_log_lock:
            self._ui_log_queue.extend(batch)

    def _ui_flusher_loop(self) -> None:
        while True:
            time.sleep(0.22)
            if not self._ui_ready or not webview.windows:
                continue
            batch = []
            with self._ui_log_lock:
                while self._ui_log_queue and len(batch) < 350:
                    batch.append(self._ui_log_queue.popleft())
            if batch:
                payload = json.dumps(batch, ensure_ascii=False)
                self._eval_js(f"window.addLogBatch && window.addLogBatch({payload})")

    def emit_progress(
        self,
        task: str,
        step: int,
        total_steps: int,
        pct: int | None,
        label: str,
        detail: str = "",
        state: str = "running",
    ) -> None:
        payload = json.dumps({
            "task": task,
            "step": step,
            "total_steps": total_steps,
            "pct": pct,
            "label": label,
            "detail": detail,
            "state": state,
        }, ensure_ascii=False)
        self._eval_js(f"window.onTaskProgress && window.onTaskProgress({payload})")

    # --- Lifecycle & App Meta ---
    def notify_ui_ready(self) -> dict:
        if not self._ui_ready:
            self._ui_ready = True
            self.telemetry.start()
            if self.client.is_configured():
                threading.Thread(target=self.run_diagnostics, daemon=True).start()
            else:
                self.push_log(
                    "Режим без подключения к серверу: доступен локальный Pack Updater (Клиент) и откат клиентских бэкапов. Для управления сервером укажите API-ключ Bisquit.Host.",
                    "SYSTEM",
                    "info",
                )
        return self.get_app_meta()

    def get_app_meta(self) -> dict:
        return {
            "name": APP_NAME,
            "version": APP_VERSION,
            "data_dir": DATA_DIR,
            "is_portable": IS_PORTABLE_MODE,
            "is_configured": self.client.is_configured(),
        }

    def open_data_dir(self) -> dict:
        try:
            if os.name == "nt":
                os.startfile(DATA_DIR)
            else:
                subprocess.Popen(["xdg-open", DATA_DIR])
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- Connection & Settings ---
    def get_config(self) -> dict:
        return self.store.get_config()

    def test_connection(self, panel_url: str = "", api_key: str = "", server_id: str = "") -> dict:
        return self.client.test_connection(panel_url, api_key, server_id)

    def save_config(self, panel_url: str, api_key: str, server_id: str, max_backups) -> dict:
        cfg = self.store.save_config(panel_url, api_key, server_id, max_backups)
        self.client.invalidate_caches()
        self.telemetry.reset_connection()
        if cfg["is_configured"]:
            self.push_log("Подключение сохранено. Инициализация связи с Bisquit.Host...", "SYSTEM", "success")
            threading.Thread(target=self.run_diagnostics, daemon=True).start()
        else:
            self.push_log("Серверное подключение очищено (локальный режим клиента).", "SYSTEM", "info")
        return {"success": True, "config": cfg}

    # --- Telemetry, Console & Power ---
    def get_status(self) -> dict:
        return self.telemetry.get_status()

    def set_log_source(self, mode: str) -> dict:
        self.telemetry.set_log_source(mode)
        return {"success": True}

    def send_power(self, signal: str) -> dict:
        if not self.client.is_configured():
            self.push_log("Сначала укажите API-ключ и ID сервера в «Подключение и Настройки».", "SYSTEM", "error")
            return {"success": False}
        try:
            self.push_log(f"Отправка сигнала питания: {signal.upper()}...", "SYSTEM", "warning")
            self.client.send_power(signal)
            return {"success": True}
        except Exception as e:
            self.push_log(f"Ошибка управления питанием ({signal}): {e}", "SYSTEM", "error")
            return {"success": False, "error": str(e)}

    def send_command(self, command: str) -> dict:
        cmd = str(command or "").strip()
        if not cmd:
            return {"success": False}
        if not self.client.is_configured():
            self.push_log("Сервер не подключён.", "SYSTEM", "error")
            return {"success": False}
        self.push_log(f"$ {cmd}", "CONSOLE", "warning")
        try:
            r = self.client.send_command(cmd)
            if r.status_code in (200, 202, 204):
                return {"success": True}
            self.push_log(f"Сервер отклонил команду (HTTP {r.status_code}). Сервер должен быть запущен.", "SYSTEM", "error")
            return {"success": False}
        except Exception as e:
            self.push_log(f"Ошибка отправки команды: {e}", "SYSTEM", "error")
            return {"success": False, "error": str(e)}

    def run_diagnostics(self) -> dict:
        if not self.client.is_configured():
            self.push_log("Диагностика сервера пропущена: не заданы API-ключ и ID сервера.", "DIAG", "warning")
            return {"success": False}

        self.push_log(f"=== ДИАГНОСТИКА {APP_NAME.upper()} v{APP_VERSION} ===", "DIAG", "warning")
        try:
            s_obj = self.client.get_server_info()
            if s_obj:
                s_name = s_obj.get("name", "GTNH Server")
                self.push_log(
                    f"Сервер: '{s_name}' | RAM: {format_size(self.client.ram_limit_bytes)} | Disk: {format_size(self.client.disk_limit_bytes)} | Облачных слотов: {self.client.panel_backup_limit}",
                    "DIAG",
                    "success",
                )
            else:
                self.push_log("Не удалось получить данные сервера (проверьте API-ключ и ID).", "DIAG", "error")
                return {"success": False}

            world_name, su_folder = self.client.detect_world_and_backup_paths(force=True)
            world_items = self.client.list_dir(f"/{world_name}")
            std_dims = [
                i.get("name") for i in world_items
                if not self.client.is_file_entry(i) and str(i.get("name", "")).startswith("DIM")
            ]
            pers_dims = [
                f"{i.get('name')} ({format_size(i.get('size', 0))})"
                for i in world_items
                if not self.client.is_file_entry(i) and is_personal_dimension(str(i.get("name", "")))
            ]
            pers_str = f" | Личные измерения: {', '.join(pers_dims)}" if pers_dims else ""
            self.push_log(f"Мир: /{world_name} | Измерений DIM*: {len(std_dims)}{pers_str}", "DIAG", "info")

            su_cfg = self.configs.get_all_configs().get("su", {})
            if su_cfg.get("found"):
                mode_desc = "ЧАСТИЧНЫЙ (только приват)" if su_cfg["only_claimed"] else "ПОЛНЫЙ МИР"
                self.push_log(
                    f"ServerUtilities: Режим={mode_desc}, Целые регионы={su_cfg['entire_regions']}, Таймер={su_cfg['backup_timer']}",
                    "DIAG",
                    "success",
                )

            mod_b = self.backups.get_mod_backups()
            partial_cnt = sum(1 for b in mod_b if b.get("is_partial"))
            self.push_log(
                f"Архивы в {su_folder}: {len(mod_b)} шт. (Полных: {len(mod_b) - partial_cnt}, Частичных: {partial_cnt})",
                "DIAG",
                "success",
            )
            return {"success": True}
        except Exception as e:
            self.push_log(f"Ошибка диагностики: {e}", "DIAG", "error")
            return {"success": False, "error": str(e)}

    # --- Backups & Restore ---
    def get_mod_backups(self) -> list[dict]:
        return self.backups.get_mod_backups()

    def trigger_mod_backup(self) -> dict:
        return self.backups.trigger_mod_backup(self.send_command)

    def compress_world_archive(self) -> dict:
        return self.backups.compress_world_archive()

    def download_mod_backup(self, filepath: str) -> dict:
        return self.backups.download_mod_backup(filepath)

    def delete_mod_backup(self, filepath: str) -> dict:
        return self.backups.delete_mod_backup(filepath)

    def inspect_mod_backup(self, filepath: str) -> dict:
        return self.backups.inspect_mod_backup(filepath)

    def restore_mod_backup(self, filepath: str, mode: str = "overlay", create_safety_snapshot: bool = True) -> dict:
        return self.backups.restore_mod_backup(filepath, mode, create_safety_snapshot, send_command_fn=self.send_command)

    def get_panel_backups(self) -> list[dict]:
        return self.backups.get_panel_backups()

    def create_panel_backup(self) -> dict:
        return self.backups.create_panel_backup()

    def download_panel_backup(self, uuid: str) -> dict:
        return self.backups.download_panel_backup(uuid)

    def delete_panel_backup(self, uuid: str) -> dict:
        return self.backups.delete_panel_backup(uuid)

    def toggle_lock_panel_backup(self, uuid: str) -> dict:
        return self.backups.toggle_lock_panel_backup(uuid)

    def restore_panel_backup(self, uuid: str) -> dict:
        return self.backups.restore_panel_backup(uuid)

    def get_client_backups(self, prism_dir: str = "") -> list[dict]:
        return self.backups.get_client_backups(prism_dir or None)

    def restore_client_backup(self, backup_name: str, prism_dir: str = "") -> dict:
        return self.backups.restore_client_backup(backup_name, prism_dir or None)

    def delete_client_backup(self, backup_name: str, prism_dir: str = "") -> dict:
        return self.backups.delete_client_backup(backup_name, prism_dir or None)

    # --- Config Studio ---
    def get_all_configs(self) -> dict:
        return self.configs.get_all_configs()

    def save_all_configs(self, payload: dict) -> dict:
        return self.configs.save_all_configs(payload)

    # --- Pack Updater & Prism Discovery ---
    def get_updater_profile(self) -> dict:
        return self.store.get_updater_profile()

    def save_updater_profile(self, partial_updates: dict) -> dict:
        prof = self.store.save_updater_profile(partial_updates)
        return {"success": True, "profile": prof}

    def discover_prism_instances(self) -> dict:
        cur = self.store.get_updater_profile().get("prism_instance_dir") or ""
        instances = discover_prism_instances(extra_paths=[cur] if cur else None)
        return {"success": True, "instances": instances}

    @staticmethod
    def _dialog_consts():
        folder_dlg = getattr(webview, "FOLDER_DIALOG", None)
        open_dlg = getattr(webview, "OPEN_DIALOG", None)
        if folder_dlg is None and hasattr(webview, "FileDialog"):
            folder_dlg = webview.FileDialog.FOLDER
        if open_dlg is None and hasattr(webview, "FileDialog"):
            open_dlg = webview.FileDialog.OPEN
        return folder_dlg, open_dlg

    def browse_prism_dir(self) -> dict:
        if not webview.windows:
            return {"success": False, "path": ""}
        folder_dlg, _ = self._dialog_consts()
        cur = self.store.get_updater_profile().get("prism_instance_dir") or ""
        init_dir = cur if (cur and os.path.isdir(cur)) else ""
        res = webview.windows[0].create_file_dialog(folder_dlg, directory=init_dir)
        if res and len(res) > 0:
            chosen = res[0]
            self.store.save_updater_profile({"prism_instance_dir": chosen})
            return {"success": True, "path": chosen}
        return {"success": False, "path": ""}

    def browse_client_zip(self) -> dict:
        if not webview.windows:
            return {"success": False, "path": ""}
        _, open_dlg = self._dialog_consts()
        cur = self.store.get_updater_profile().get("last_client_zip") or ""
        init_dir = os.path.dirname(cur) if (cur and os.path.isdir(os.path.dirname(cur))) else ""
        res = webview.windows[0].create_file_dialog(
            open_dlg, directory=init_dir, allow_multiple=False,
            file_types=("ZIP Archives (*.zip)", "All files (*.*)"),
        )
        if res and len(res) > 0:
            chosen = res[0]
            self.store.save_updater_profile({"last_client_zip": chosen})
            return {"success": True, "path": chosen}
        return {"success": False, "path": ""}

    def browse_server_zip(self) -> dict:
        if not webview.windows:
            return {"success": False, "path": ""}
        _, open_dlg = self._dialog_consts()
        cur = self.store.get_updater_profile().get("last_server_zip") or ""
        init_dir = os.path.dirname(cur) if (cur and os.path.isdir(os.path.dirname(cur))) else ""
        res = webview.windows[0].create_file_dialog(
            open_dlg, directory=init_dir, allow_multiple=False,
            file_types=("ZIP Archives (*.zip)", "All files (*.*)"),
        )
        if res and len(res) > 0:
            chosen = res[0]
            self.store.save_updater_profile({"last_server_zip": chosen})
            return {"success": True, "path": chosen}
        return {"success": False, "path": ""}

    def browse_extra_server_mods(self) -> dict:
        if not webview.windows:
            return {"success": False, "files": []}
        _, open_dlg = self._dialog_consts()
        res = webview.windows[0].create_file_dialog(
            open_dlg, allow_multiple=True,
            file_types=("Minecraft Mods (*.jar;*.zip)", "All files (*.*)"),
        )
        if res:
            out = []
            for p in res:
                if os.path.isfile(p):
                    out.append({"path": p, "name": os.path.basename(p), "size_str": format_size(os.path.getsize(p))})
            return {"success": True, "files": out}
        return {"success": False, "files": []}

    def analyze_pack_update(self, params: dict) -> dict:
        return analyze_pack_update(params, self.store, self.client, self.push_log)

    def execute_pack_update(self, payload: dict) -> dict:
        if not self._busy_lock.acquire(blocking=False):
            self.push_log("Другая операция (обновление, откат или инспекция) уже выполняется!", "UPDATER", "warning")
            return {"success": False, "error": "Другая операция уже выполняется"}

        mode = payload.get("mode", "client")
        prism_dir = (payload.get("prism_instance_dir") or "").strip()
        client_zip = (payload.get("client_zip_path") or "").strip()
        srv_src_mode = payload.get("server_source_mode", "zip")
        server_zip = (payload.get("server_zip_path") or "").strip()
        server_url = (payload.get("server_zip_url") or "").strip()

        confirmed_client_mods = payload.get("confirmed_client_mods") or []
        confirmed_resourcepacks = payload.get("confirmed_resourcepacks") or []
        confirmed_server_mods = payload.get("confirmed_server_mods") or []
        protected_configs = [
            normalize_config_relpath(p) for p in (payload.get("protected_configs") or [])
            if normalize_config_relpath(p)
        ]
        create_server_safety = bool(payload.get("create_server_safety", True))
        auto_start_server = bool(payload.get("auto_start_server", True))

        updates = {
            "update_mode": mode,
            "prism_instance_dir": prism_dir,
            "last_client_zip": client_zip,
            "server_source_mode": srv_src_mode,
            "last_server_zip": server_zip,
            "last_server_url": server_url,
            "protected_configs": protected_configs,
        }
        if mode in ("both", "client"):
            updates["custom_client_mods"] = list(dict.fromkeys(confirmed_client_mods))
            updates["custom_resourcepacks"] = list(dict.fromkeys(confirmed_resourcepacks))
        if mode in ("both", "server"):
            srv_mod_names = [
                (m.get("rel_path") if isinstance(m, dict) else str(m))
                for m in confirmed_server_mods
            ]
            updates["custom_server_mods"] = list(dict.fromkeys([x for x in srv_mod_names if x]))
        self.store.save_updater_profile(updates)

        self.push_log("Профиль сохранён в gtnh_updater_profile.json. Запуск GTNH Pack Updater...", "UPDATER", "warning")

        def _worker():
            self._eval_js("window.setUpdaterBusy && window.setUpdaterBusy(true)")
            overall_ok = True
            err_msg = ""
            try:
                if mode in ("both", "client"):
                    run_client_update(
                        prism_dir=prism_dir,
                        client_zip=client_zip,
                        confirmed_custom_mods=confirmed_client_mods,
                        confirmed_custom_rps=confirmed_resourcepacks,
                        protected_configs=protected_configs,
                        log_fn=self.push_log,
                        progress_fn=self.emit_progress,
                    )

                if mode in ("both", "server"):
                    run_server_update(
                        client=self.client,
                        telemetry=self.telemetry,
                        send_command_fn=self.send_command,
                        srv_src_mode=srv_src_mode,
                        server_zip=server_zip,
                        server_url=server_url,
                        confirmed_server_mods=confirmed_server_mods,
                        protected_configs=protected_configs,
                        create_safety=create_server_safety,
                        auto_start=auto_start_server,
                        prism_dir=prism_dir,
                        log_fn=self.push_log,
                        progress_fn=self.emit_progress,
                    )

                self.push_log("=== GTNH PACK UPDATER: ОБНОВЛЕНИЕ УСПЕШНО ЗАВЕРШЕНО! ===", "UPDATER", "success")
            except Exception as e:
                overall_ok = False
                err_msg = str(e)
                self.push_log(f"ОШИБКА ОБНОВЛЕНИЯ: {e}", "UPDATER", "error")
                self.emit_progress("updater", 1, 1, 100, "Ошибка обновления", err_msg, state="error")
            finally:
                self._busy_lock.release()
                payload_done = json.dumps({"ok": overall_ok, "error": err_msg}, ensure_ascii=False)
                self._eval_js(f"window.onPackUpdateComplete && window.onPackUpdateComplete({payload_done})")

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}
