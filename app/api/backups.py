import os
import shutil
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.constants import (
    ARCHIVE_EXTENSIONS,
    EXTRA_MOD_DIRS,
    GTNH_WORLD_DAT_FILES,
    GTNH_WORLD_MOD_DIRS,
    MOD_FILE_EXTENSIONS,
)
from app.utils import (
    calc_dir_size,
    format_dim_label,
    format_msk_time,
    format_size,
    is_dimension_folder,
    is_file_actively_locked,
    is_personal_dimension,
    rmtree_force,
)


class BackupService:
    """Manages server mod/file backups, panel cloud snapshots, X-Ray inspection, Overlay Restore, and local client backups."""

    def __init__(self, client, telemetry, store, log_fn, progress_fn, busy_guard, ui_eval_fn):
        self.client = client
        self.telemetry = telemetry
        self.store = store
        self._log = log_fn
        self._progress = progress_fn
        self._busy = busy_guard
        self._ui_eval = ui_eval_fn

    # --- Server Mod & File Archives ---
    def get_mod_backups(self) -> list[dict]:
        if not self.client.is_configured():
            return []

        _, su_folder = self.client.detect_world_and_backup_paths()
        candidate_dirs = []
        for d in [su_folder, "/backups"]:
            if d and d not in candidate_dirs:
                candidate_dirs.append(d)

        found_backups = []
        seen_paths = set()

        def _scan_dir(dir_path: str, depth: int = 0):
            if depth > 1:
                return
            for item in self.client.list_dir(dir_path):
                name = item.get("name") or item.get("filename") or ""
                if not name:
                    continue
                full_path = f"{dir_path.rstrip('/')}/{name}" if dir_path != "/" else f"/{name}"
                if self.client.is_file_entry(item):
                    if name.lower().endswith(ARCHIVE_EXTENSIONS) and full_path not in seen_paths:
                        seen_paths.add(full_path)
                        size_bytes = int(item.get("size") or item.get("bytes") or 0)
                        dt_raw = item.get("modified") or item.get("modified_at") or item.get("created") or ""
                        is_partial = (
                            size_bytes < (900 * 1024 * 1024)
                            and not name.startswith(("panel_world_", "pre_update_safety_", "pre_restore_safety_"))
                        )
                        found_backups.append({
                            "filename": name,
                            "filepath": full_path,
                            "directory": dir_path,
                            "display_name": full_path.lstrip("/") if dir_path != "/backups" else name,
                            "pretty_date": format_msk_time(dt_raw, filename_hint=name),
                            "size": format_size(size_bytes),
                            "size_bytes": size_bytes,
                            "is_partial": is_partial,
                            "raw_date": str(dt_raw),
                        })
                elif dir_path != "/" and not name.startswith("."):
                    _scan_dir(full_path, depth + 1)

        for cdir in candidate_dirs:
            _scan_dir(cdir, 0)

        for item in self.client.list_dir("/"):
            name = item.get("name") or ""
            if self.client.is_file_entry(item) and name.lower().endswith(ARCHIVE_EXTENSIONS):
                full_path = f"/{name}"
                if full_path not in seen_paths:
                    seen_paths.add(full_path)
                    size_bytes = int(item.get("size") or item.get("bytes") or 0)
                    dt_raw = item.get("modified") or item.get("modified_at") or item.get("created") or ""
                    found_backups.append({
                        "filename": name,
                        "filepath": full_path,
                        "directory": "/",
                        "display_name": f"[КОРЕНЬ] {name}",
                        "pretty_date": format_msk_time(dt_raw, filename_hint=name),
                        "size": format_size(size_bytes),
                        "size_bytes": size_bytes,
                        "is_partial": size_bytes < (900 * 1024 * 1024),
                        "raw_date": str(dt_raw),
                    })

        found_backups.sort(key=lambda x: (x.get("raw_date") or "", x["filename"]), reverse=True)
        return found_backups

    def trigger_mod_backup(self, send_command_fn) -> dict:
        self._log("Запуск внутриигрового бэкапа ServerUtilities (/backup start)...", "BACKUP", "success")
        res = send_command_fn("backup start")

        def _poll():
            for delay in (4, 6):
                time.sleep(delay)
                self._ui_eval("window.refreshBackups && window.refreshBackups(true)")

        threading.Thread(target=_poll, daemon=True).start()
        return res

    def compress_world_archive(self) -> dict:
        if not self.client.is_configured():
            return {"success": False, "error": "Сервер не настроен"}
        if not self._busy.acquire(blocking=False):
            self._log("Другая операция уже выполняется — дождитесь завершения.", "BACKUP", "warning")
            return {"success": False, "error": "Занято другой операцией"}

        def _worker():
            try:
                self._progress("backup", 1, 3, 10, "Подготовка полного архива мира", "Поиск папок мира и модов...")
                world_name, _ = self.client.detect_world_and_backup_paths()
                self._log(f"Создание полного архива '/{world_name}' + приватов + жил (2–4 мин)...", "BACKUP", "warning")
                self.client.create_folder("/", "backups")

                root_items = self.client.list_dir("/")
                existing_names = {i.get("name") for i in root_items if i.get("name")}
                to_compress = [world_name] if world_name in existing_names else []
                for extra in EXTRA_MOD_DIRS:
                    if extra in existing_names and extra not in to_compress:
                        to_compress.append(extra)

                if not to_compress:
                    self._log(f"Папка мира '{world_name}' не найдена в корне сервера!", "BACKUP", "error")
                    self._progress("backup", 3, 3, 100, "Ошибка", f"Папка '{world_name}' не найдена", state="error")
                    return

                stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
                target_name = f"panel_world_{stamp}.tar.gz"
                self._progress("backup", 2, 3, 35, "Сжатие мира на сервере", f"Архивируется: {', '.join(to_compress)}")
                archive_name = self.client.compress_files(
                    "/",
                    to_compress,
                    target_name,
                    fmt="tar_gz",
                    foreground=True,
                    timeout=600,
                    on_progress=lambda p, msg: self._progress("backup", 2, 3, p if p is not None else 55, "Сжатие мира", msg),
                )
                self._progress("backup", 3, 3, 90, "Перемещение в /backups", archive_name)
                if archive_name:
                    self.client.rename_files("/", [{"from": archive_name, "to": f"backups/{archive_name}"}])
                    self._log(f"Полный архив мира создан: /backups/{archive_name}", "BACKUP", "success")
                self._progress("backup", 3, 3, 100, "Архив готов", f"/backups/{archive_name}", state="done")
                self._ui_eval("window.refreshBackups && window.refreshBackups(true)")
            except Exception as e:
                self._log(f"Ошибка создания полного архива мира: {e}", "BACKUP", "error")
                self._progress("backup", 3, 3, 100, "Ошибка архивации", str(e), state="error")
            finally:
                self._busy.release()

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}

    def download_mod_backup(self, filepath: str) -> dict:
        try:
            root_dir = os.path.dirname(filepath) or "/"
            filename = os.path.basename(filepath)
            dl_url = self.client.get_download_url(root_dir, filename)
            if dl_url:
                webbrowser.open(dl_url)
                self._log(f"Скачивание {filepath} открыто в браузере.", "BACKUP", "success")
                return {"success": True}
            return {"success": False}
        except Exception as e:
            self._log(f"Ошибка скачивания ({filepath}): {e}", "BACKUP", "error")
            return {"success": False}

    def delete_mod_backup(self, filepath: str) -> dict:
        try:
            directory = os.path.dirname(filepath) or "/"
            filename = os.path.basename(filepath)
            self.client.delete_files(directory, [filename], permanent=True)
            self._log(f"Архив {filepath} безвозвратно удалён с сервера.", "BACKUP", "info")
            return {"success": True}
        except Exception as e:
            self._log(f"Ошибка удаления {filepath}: {e}", "BACKUP", "error")
            return {"success": False}

    # --- X-Ray Backup Inspector ---
    def inspect_mod_backup(self, filepath: str) -> dict:
        if not self._busy.acquire(blocking=False):
            self._log("Подождите завершения текущей операции!", "INSPECT", "warning")
            return {"success": False}

        def _worker():
            inspect_dir = "_gtnh_inspect_tmp"
            rel_filepath = filepath.lstrip("/")
            filename = os.path.basename(filepath)
            moved = False
            used_sandbox = False
            try:
                self._log(f"Рентген-анализ структуры архива {filename}...", "INSPECT", "warning")
                self._progress("inspect", 1, 3, 15, "Рентген-инспектор", f"Чтение оглавления {filename}...")

                base_inspect_path = filepath
                top_items = self.client.list_dir(base_inspect_path)

                if not top_items:
                    used_sandbox = True
                    base_inspect_path = f"/{inspect_dir}"
                    self._progress("inspect", 1, 3, 30, "Распаковка в песочницу", f"{filename} не поддерживает виртуальный листинг")
                    self.client.delete_files("/", [inspect_dir], permanent=True, raise_on_error=False)
                    self.client.create_folder("/", inspect_dir)
                    self.client.rename_files("/", [{"from": rel_filepath, "to": f"{inspect_dir}/{filename}"}])
                    moved = True
                    try:
                        self.client.decompress_file(f"/{inspect_dir}", filename, foreground=True, timeout=300)
                    finally:
                        self.client.rename_files("/", [{"from": f"{inspect_dir}/{filename}", "to": rel_filepath}])
                        moved = False
                    top_items = self.client.list_dir(base_inspect_path)

                self._progress("inspect", 2, 3, 55, "Сканирование измерений и модов", filename)
                top_map = {i.get("name"): i for i in top_items if i.get("name")}
                world_name, _ = self.client.detect_world_and_backup_paths()

                world_root = base_inspect_path
                layout_type = "Корневой (файлы мира напрямую в архиве)"
                for candidate in (world_name, "World", "world"):
                    if candidate in top_map and not self.client.is_file_entry(top_map[candidate]):
                        w_sz = format_size(top_map[candidate].get("size", 0))
                        world_root = f"{base_inspect_path.rstrip('/')}/{candidate}"
                        layout_type = f"Вложенная папка /{candidate} ({w_sz})"
                        break

                su_meta_info = ""
                if ".serverutilities-backup.properties" in top_map:
                    prop_content = self.client.read_file(f"{base_inspect_path.rstrip('/')}/.serverutilities-backup.properties")
                    if prop_content:
                        meta_lines = [ln.strip() for ln in prop_content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                        su_meta_info = " | ".join(meta_lines[:4])

                su_ranks_ok = False
                if "serverutilities" in top_map:
                    su_srv = self.client.list_dir(f"{base_inspect_path.rstrip('/')}/serverutilities/server")
                    su_names = {x.get("name") for x in su_srv}
                    if "ranks.txt" in su_names or "players.txt" in su_names:
                        su_ranks_ok = True

                vp_dims_count = 0
                if "visualprospecting" in top_map:
                    vp_srv = self.client.list_dir(f"{base_inspect_path.rstrip('/')}/visualprospecting/server")
                    for vp_sub in vp_srv:
                        if not self.client.is_file_entry(vp_sub) and vp_sub.get("name"):
                            vp_files = self.client.list_dir(f"{base_inspect_path.rstrip('/')}/visualprospecting/server/{vp_sub['name']}")
                            vp_dims_count += sum(1 for f in vp_files if str(f.get("name", "")).endswith(".dat"))

                w_items = self.client.list_dir(world_root)
                w_map = {i.get("name"): i for i in w_items if i.get("name")}
                has_level_dat = "level.dat" in w_map

                ow_size = int(w_map.get("region", {}).get("size") or 0)
                ow_regions = [
                    x.get("name") for x in self.client.list_dir(f"{world_root}/region")
                    if str(x.get("name", "")).endswith(".mca")
                ]
                active_dimensions = [f"Overworld (region): {len(ow_regions)} файлов .mca ({format_size(ow_size)})"]
                empty_dimensions = []
                personal_dims_report = []

                dim_entries = [
                    (d_name, d_obj) for d_name, d_obj in sorted(w_map.items())
                    if not self.client.is_file_entry(d_obj) and is_dimension_folder(d_name)
                ]

                def _check_dim(item_tuple):
                    d_name, d_obj = item_tuple
                    d_size = int(d_obj.get("size") or 0)
                    is_pers = is_personal_dimension(d_name)
                    if d_size <= 10500 and not is_pers:
                        return (d_name, d_size, 0, is_pers)
                    regs = [
                        x.get("name") for x in self.client.list_dir(f"{world_root}/{d_name}/region")
                        if str(x.get("name", "")).endswith(".mca")
                    ]
                    return (d_name, d_size, len(regs), is_pers)

                with ThreadPoolExecutor(max_workers=8) as pool:
                    dim_results = list(pool.map(_check_dim, dim_entries))

                for d_name, d_size, mca_cnt, is_pers in dim_results:
                    pretty_lbl = format_dim_label(d_name)
                    if is_pers:
                        personal_dims_report.append(f"{d_name}: {format_size(d_size)} ({mca_cnt} .mca)")
                        active_dimensions.append(f"{pretty_lbl}: {mca_cnt} файлов .mca ({format_size(d_size)})")
                    elif mca_cnt > 0 or d_size > 15000:
                        active_dimensions.append(f"{pretty_lbl}: {mca_cnt} файлов .mca ({format_size(d_size)})")
                    else:
                        empty_dimensions.append(d_name)

                extras_found = []
                for ex in EXTRA_MOD_DIRS:
                    if ex in top_map:
                        sz = format_size(top_map[ex].get("size", 0))
                        label = f"{ex} ({sz})"
                        if ex == "serverutilities" and su_ranks_ok:
                            label = f"serverutilities [ranks.txt + players.txt] ({sz})"
                        elif ex == "visualprospecting" and vp_dims_count > 0:
                            label = f"visualprospecting [{vp_dims_count} DIM*.dat жил] ({sz})"
                        extras_found.append(label)

                for gmod in GTNH_WORLD_MOD_DIRS:
                    if gmod in w_map:
                        sz = int(w_map[gmod].get("size") or 0)
                        if sz > 0:
                            extras_found.append(f"{gmod} ({format_size(sz)})")

                dat_files_found = [
                    f"{df} ({format_size(w_map[df].get('size', 0))})"
                    for df in GTNH_WORLD_DAT_FILES if df in w_map
                ]

                report = {
                    "filename": filename,
                    "filepath": filepath,
                    "has_level_dat": has_level_dat,
                    "layout": layout_type,
                    "su_meta": su_meta_info,
                    "total_dimensions": 1 + len(dim_entries),
                    "active_dimensions": active_dimensions,
                    "empty_dimensions": empty_dimensions,
                    "personal_dims": personal_dims_report,
                    "extras": extras_found,
                    "dat_files": dat_files_found,
                    "virtual_read": not used_sandbox,
                }
                self._log(
                    f"Рентген {filename}: level.dat={'OK' if has_level_dat else 'НЕТ'}, активных измерений={len(active_dimensions)}/{1 + len(dim_entries)}",
                    "INSPECT",
                    "success" if has_level_dat else "error",
                )
                self._progress("inspect", 3, 3, 100, "Анализ завершён", filename, state="done")
                import json
                self._ui_eval(f"window.showInspectReport && window.showInspectReport({json.dumps(report, ensure_ascii=False)})")
            except Exception as e:
                self._log(f"Ошибка проверки архива: {e}", "INSPECT", "error")
                self._progress("inspect", 3, 3, 100, "Ошибка анализа", str(e), state="error")
                if moved:
                    try:
                        self.client.rename_files("/", [{"from": f"{inspect_dir}/{filename}", "to": rel_filepath}])
                    except Exception:
                        pass
            finally:
                if used_sandbox:
                    try:
                        self.client.delete_files("/", [inspect_dir], permanent=True, raise_on_error=False)
                    except Exception:
                        pass
                self._busy.release()

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}

    # --- Server World Restore (Overlay / Clean Replace) ---
    def restore_mod_backup(self, filepath: str, mode: str = "overlay", create_safety_snapshot: bool = True, send_command_fn=None) -> dict:
        if not self._busy.acquire(blocking=False):
            self._log("Другая операция уже выполняется!", "RESTORE", "warning")
            return {"success": False}

        def _worker():
            world_name, _ = self.client.detect_world_and_backup_paths()
            staging_dir = "_gtnh_restore_staging"
            rel_filepath = filepath.lstrip("/")
            filename = os.path.basename(filepath)
            moved_to_staging = False

            try:
                self._progress("restore", 1, 6, 8, "Остановка сервера", f"Подготовка к откату из {filename}")
                self._log(f"[1/6] Сохранение и остановка сервера перед откатом из {filename}...", "RESTORE", "warning")
                if self.telemetry.live_stats.get("state") == "running" and send_command_fn:
                    send_command_fn("save-all")
                    time.sleep(2)
                self.client.send_power("stop")
                for _ in range(60):
                    if self.telemetry.get_status().get("state") == "offline":
                        break
                    time.sleep(3)
                else:
                    self._log("Сервер не остановился за 3 минуты — отправка KILL...", "RESTORE", "error")
                    self.client.send_power("kill")
                    time.sleep(5)

                if create_safety_snapshot:
                    self._progress("restore", 2, 6, 22, "Страховочный снапшот", "Сжатие текущего мира + приватов + жил...")
                    self._log("[2/6] Создание страховочной точки возврата текущего мира...", "RESTORE", "info")
                    self.client.create_folder("/", "backups")
                    root_names = {i.get("name") for i in self.client.list_dir("/") if i.get("name")}
                    safety_files = [world_name] if world_name in root_names else []
                    for extra in EXTRA_MOD_DIRS:
                        if extra in root_names and extra not in safety_files:
                            safety_files.append(extra)
                    if safety_files:
                        safety_name = f"pre_restore_safety_{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.tar.gz"
                        cname = self.client.compress_files("/", safety_files, safety_name, fmt="tar_gz", foreground=True, timeout=600)
                        self.client.rename_files("/", [{"from": cname, "to": f"backups/{safety_name}"}])
                        self._log(f"Страховочный архив сохранён: /backups/{safety_name}", "RESTORE", "success")
                else:
                    self._log("[2/6] Страховочный снапшот пропущен по выбору пользователя.", "RESTORE", "info")

                self._progress("restore", 3, 6, 48, "Распаковка в песочницу", filename)
                self._log(f"[3/6] Распаковка {filename} в /{staging_dir}...", "RESTORE", "warning")
                self.client.delete_files("/", [staging_dir], permanent=True, raise_on_error=False)
                self.client.create_folder("/", staging_dir)
                self.client.rename_files("/", [{"from": rel_filepath, "to": f"{staging_dir}/{filename}"}])
                moved_to_staging = True
                try:
                    self.client.decompress_file(f"/{staging_dir}", filename, foreground=True, timeout=600)
                finally:
                    self.client.rename_files("/", [{"from": f"{staging_dir}/{filename}", "to": rel_filepath}])
                    moved_to_staging = False

                self._progress("restore", 4, 6, 65, "Проверка целостности", "Поиск level.dat и структуры мира")
                self._log("[4/6] Проверка целостности level.dat и структуры архива...", "RESTORE", "info")
                staged_items = self.client.list_dir(f"/{staging_dir}")
                staged_map = {i.get("name"): i for i in staged_items if i.get("name")}

                extracted_world_dir = None
                for candidate in (world_name, "World", "world"):
                    if candidate in staged_map and not self.client.is_file_entry(staged_map[candidate]):
                        sub = self.client.list_dir(f"/{staging_dir}/{candidate}")
                        if any(x.get("name") == "level.dat" for x in sub):
                            extracted_world_dir = f"/{staging_dir}/{candidate}"
                            break
                if not extracted_world_dir and "level.dat" in staged_map:
                    extracted_world_dir = f"/{staging_dir}"
                if not extracted_world_dir:
                    raise RuntimeError("В архиве не найден level.dat! Откат отменён, мир остался нетронутым.")

                for extra in EXTRA_MOD_DIRS:
                    if extra in staged_map and not self.client.is_file_entry(staged_map[extra]):
                        self._log(f"Синхронизация данных мода /{extra}...", "RESTORE", "info")
                        self.client.create_folder("/", extra)
                        self.client.merge_directory_tree(f"/{staging_dir}/{extra}", f"/{extra}")
                        self.client.delete_files(f"/{staging_dir}", [extra], permanent=True, raise_on_error=False)

                self._progress("restore", 5, 6, 82, "Применение файлов мира", f"Режим: {mode.upper()}")
                if mode == "overlay":
                    self._log(f"[5/6] Хирургическое наложение регионов (OVERLAY) поверх /{world_name}...", "RESTORE", "warning")
                    self.client.merge_directory_tree(extracted_world_dir, f"/{world_name}")
                else:
                    self._log(f"[5/6] Полная замена папки мира /{world_name}...", "RESTORE", "warning")
                    tmp_old = f"{world_name}_old_{int(time.time())}"
                    self.client.rename_files("/", [{"from": world_name, "to": tmp_old}])
                    try:
                        self.client.rename_files("/", [{"from": extracted_world_dir.lstrip("/"), "to": world_name}])
                    except Exception:
                        self.client.rename_files("/", [{"from": tmp_old, "to": world_name}])
                        raise
                    self.client.delete_files("/", [tmp_old], permanent=True, raise_on_error=False)

                self.client.delete_files(f"/{world_name}", ["session.lock"], permanent=True, raise_on_error=False)
                self.client.delete_files("/", [staging_dir], permanent=True, raise_on_error=False)

                self._progress("restore", 6, 6, 100, "Откат завершён", "Запуск сервера...", state="done")
                self._log("[6/6] Мир успешно восстановлен! Запускаю сервер...", "RESTORE", "success")
                self.client.send_power("start")
            except Exception as e:
                self._log(f"ОШИБКА ОТКАТА: {e}", "RESTORE", "error")
                self._progress("restore", 6, 6, 100, "Ошибка отката", str(e), state="error")
                if moved_to_staging:
                    try:
                        self.client.rename_files("/", [{"from": f"{staging_dir}/{filename}", "to": rel_filepath}])
                    except Exception:
                        pass
            finally:
                self._busy.release()
                self._ui_eval("window.onRestoreComplete && window.onRestoreComplete()")

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}

    # --- Panel Cloud Snapshots ---
    def get_panel_backups(self) -> list[dict]:
        if not self.client.is_configured():
            return []
        try:
            r = self.client.session.get(
                f"{self.client.base_url()}/backups",
                headers=self.client.headers(),
                params={"page": 1, "per_page": 50, "ungrouped": "false"},
                timeout=8,
            )
            if r.status_code != 200:
                return []

            items = self.client.unwrap_list(r.json())
            result = []
            for b in items:
                raw_bytes = b.get("bytes") or b.get("size") or 0
                completed_at = b.get("completed") or b.get("completed_at")
                is_ok = bool(b.get("is_successful", True))
                is_locked = bool(b.get("is_locked", False))

                if not completed_at and not b.get("is_successful"):
                    size_str = "В процессе..."
                    status = "pending"
                elif not is_ok:
                    size_str = "Ошибка создания"
                    status = "failed"
                else:
                    size_str = format_size(raw_bytes)
                    status = "ok"

                dt_raw = b.get("created") or b.get("created_at") or ""
                result.append({
                    "uuid": b.get("uuid", ""),
                    "name": b.get("name") or "Panel Backup",
                    "pretty_date": format_msk_time(dt_raw),
                    "size": size_str,
                    "ok": is_ok,
                    "locked": is_locked,
                    "status": status,
                    "raw_date": str(dt_raw),
                })
            result.sort(key=lambda x: x["raw_date"], reverse=True)
            return result
        except Exception:
            return []

    def create_panel_backup(self) -> dict:
        if not self.client.is_configured():
            return {"success": False}

        def _worker():
            try:
                self._log("Проверка слотов облачных бэкапов панели...", "BACKUP", "info")
                if self.client.panel_backup_limit is None:
                    self.client.get_server_info()
                backups = self.get_panel_backups()
                backups.sort(key=lambda b: b.get("raw_date") or "")

                max_b = int(self.store.get_config().get("max_panel_backups") or 3)
                if self.client.panel_backup_limit is not None and self.client.panel_backup_limit > 0:
                    max_b = min(max_b, self.client.panel_backup_limit)
                if self.client.panel_backup_limit == 0:
                    self._log("На вашем тарифе 0 слотов для облачных бэкапов панели. Используйте архивы сервера.", "BACKUP", "error")
                    return

                unlocked = [b for b in backups if not b.get("locked")]
                while len(backups) >= max_b and unlocked:
                    oldest = unlocked.pop(0)
                    backups = [b for b in backups if b["uuid"] != oldest["uuid"]]
                    self._log(f"Удаляю старый облачный бэкап: {oldest.get('name')}...", "BACKUP", "warning")
                    self.client.session.delete(
                        f"{self.client.base_url()}/backups/{oldest['uuid']}",
                        headers=self.client.headers(),
                        timeout=10,
                    )

                now_name = datetime.now().strftime("GTNH-%d.%m-%H:%M")
                cr = self.client.session.post(
                    f"{self.client.base_url()}/backups",
                    headers=self.client.headers(),
                    json={"name": now_name, "ignored_files": ["backups/*", "logs/*", "crash-reports/*"]},
                    timeout=15,
                )
                if cr.status_code >= 400:
                    self._log(f"Панель отклонила создание бэкапа (HTTP {cr.status_code}): {cr.text[:160]}", "BACKUP", "error")
                    return
                self._log(f"Облачный бэкап панели '{now_name}' запущен!", "BACKUP", "success")
                self._ui_eval("window.refreshBackups && window.refreshBackups(true)")
            except Exception as e:
                self._log(f"Ошибка создания облачного бэкапа: {e}", "BACKUP", "error")

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}

    def download_panel_backup(self, uuid: str) -> dict:
        try:
            r = self.client.session.get(
                f"{self.client.base_url()}/backups/{uuid}/download",
                headers=self.client.headers(),
                params={"archive_format": "tar_gz"},
                timeout=10,
            )
            r.raise_for_status()
            dl_url = self.client.unwrap_item(r.json()).get("url")
            if dl_url:
                webbrowser.open(dl_url)
                return {"success": True}
            return {"success": False}
        except Exception as e:
            self._log(f"Ошибка скачивания облачного бэкапа: {e}", "BACKUP", "error")
            return {"success": False}

    def delete_panel_backup(self, uuid: str) -> dict:
        try:
            r = self.client.session.delete(f"{self.client.base_url()}/backups/{uuid}", headers=self.client.headers(), timeout=10)
            if r.status_code in (200, 202, 204):
                self._log("Облачный бэкап панели удалён.", "BACKUP", "info")
                return {"success": True}
            return {"success": False}
        except Exception:
            return {"success": False}

    def toggle_lock_panel_backup(self, uuid: str) -> dict:
        try:
            cur = [b for b in self.get_panel_backups() if b["uuid"] == uuid]
            new_lock = not cur[0]["locked"] if cur else True
            r = self.client.session.patch(
                f"{self.client.base_url()}/backups/{uuid}",
                headers=self.client.headers(),
                json={"locked": new_lock},
                timeout=10,
            )
            r.raise_for_status()
            return {"success": True}
        except Exception:
            return {"success": False}

    def restore_panel_backup(self, uuid: str) -> dict:
        if not self._busy.acquire(blocking=False):
            self._log("Другая операция уже выполняется!", "RESTORE", "warning")
            return {"success": False}

        def _worker():
            try:
                self._progress("restore", 1, 3, 15, "Остановка сервера", "Подготовка к облачному откату")
                self._log("[1/3] Остановка сервера перед откатом облачного снапшота...", "RESTORE", "warning")
                self.client.send_power("stop")
                for _ in range(60):
                    if self.telemetry.get_status().get("state") == "offline":
                        break
                    time.sleep(3)

                self._progress("restore", 2, 3, 50, "Восстановление облачного снапшота", uuid[:8])
                self._log(f"[2/3] Запуск восстановления снапшота панели {uuid[:8]}...", "RESTORE", "warning")
                self.client.session.post(
                    f"{self.client.base_url()}/backups/{uuid}/restore",
                    headers=self.client.headers(),
                    json={"truncate_directory": False},
                    timeout=15,
                ).raise_for_status()
                time.sleep(8)
                for _ in range(120):
                    chk = self.client.session.get(
                        f"{self.client.base_url()}/backups/{uuid}",
                        headers=self.client.headers(),
                        timeout=10,
                    ).json()
                    info = self.client.unwrap_item(chk.get("backup", chk))
                    if info.get("completed") or info.get("completed_at"):
                        break
                    time.sleep(5)

                self._progress("restore", 3, 3, 100, "Снапшот развёрнут", "Запуск сервера...", state="done")
                self._log("[3/3] Облачный снапшот развёрнут! Запускаю сервер...", "RESTORE", "success")
                self.client.send_power("start")
            except Exception as e:
                self._log(f"Ошибка восстановления облачного снапшота: {e}", "RESTORE", "error")
                self._progress("restore", 3, 3, 100, "Ошибка отката", str(e), state="error")
            finally:
                self._busy.release()
                self._ui_eval("window.onRestoreComplete && window.onRestoreComplete()")

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}

    # --- Local Prism Client Backups (_gtnh_client_backup_*) ---
    def _resolve_prism_dir(self, prism_dir: str | None = None) -> str:
        p = (prism_dir or self.store.get_updater_profile().get("prism_instance_dir") or "").strip()
        return p

    @staticmethod
    def _locate_mc_dir(instance_dir: str) -> str | None:
        if not instance_dir or not os.path.isdir(instance_dir):
            return None
        for sub in (".minecraft", "minecraft"):
            cand = os.path.join(instance_dir, sub)
            if os.path.isdir(cand):
                return cand
        return None

    def get_client_backups(self, prism_dir: str | None = None) -> list[dict]:
        inst = self._resolve_prism_dir(prism_dir)
        if not inst or not os.path.isdir(inst):
            return []

        backups = []
        try:
            for entry in sorted(os.listdir(inst), reverse=True):
                if not entry.startswith("_gtnh_client_backup_"):
                    continue
                full = os.path.join(inst, entry)
                if not os.path.isdir(full):
                    continue
                contents = sorted(os.listdir(full))
                mods_cnt = 0
                mods_sub = os.path.join(full, "mods")
                if os.path.isdir(mods_sub):
                    mods_cnt = sum(
                        1 for f in os.listdir(mods_sub)
                        if os.path.isfile(os.path.join(mods_sub, f)) and f.lower().endswith(MOD_FILE_EXTENSIONS)
                    )
                sz = calc_dir_size(full)
                backups.append({
                    "name": entry,
                    "path": full,
                    "pretty_date": format_msk_time("", filename_hint=entry),
                    "size": format_size(sz),
                    "size_bytes": sz,
                    "mods_count": mods_cnt,
                    "contents": contents,
                    "has_patches": "patches" in contents,
                    "has_libraries": "libraries" in contents,
                })
        except Exception as e:
            self._log(f"Ошибка чтения клиентских бэкапов: {e}", "BACKUP", "warning")
        return backups

    def restore_client_backup(self, backup_name: str, prism_dir: str | None = None) -> dict:
        inst = self._resolve_prism_dir(prism_dir)
        if not inst or not os.path.isdir(inst):
            return {"success": False, "error": "Папка инстанса Prism Launcher не настроена."}
        if not backup_name or not backup_name.startswith("_gtnh_client_backup_") or "/" in backup_name or "\\" in backup_name or ".." in backup_name:
            return {"success": False, "error": "Некорректное имя клиентского бэкапа."}

        backup_path = os.path.join(inst, backup_name)
        mc_dir = self._locate_mc_dir(inst)
        if not os.path.isdir(backup_path) or not mc_dir:
            return {"success": False, "error": "Папка бэкапа или .minecraft не найдены."}

        if not self._busy.acquire(blocking=False):
            return {"success": False, "error": "Другая операция уже выполняется."}

        def _worker():
            try:
                self._progress("client_restore", 1, 4, 10, "Проверка блокировок клиента", backup_name)
                # Ensure client is closed
                locked = []
                saves_dir = os.path.join(mc_dir, "saves")
                if os.path.isdir(saves_dir):
                    for e in os.listdir(saves_dir):
                        sl = os.path.join(saves_dir, e, "session.lock")
                        if os.path.isfile(sl) and is_file_actively_locked(sl):
                            locked.append(f"saves/{e}/session.lock")
                for rel_log in ("logs/latest.log", "logs/fml-client-latest.log"):
                    lp = os.path.join(mc_dir, *rel_log.split("/"))
                    if os.path.isfile(lp) and is_file_actively_locked(lp):
                        locked.append(rel_log)
                if locked:
                    raise RuntimeError(f"Клиент Minecraft запущен ({', '.join(locked)}). Закройте игру перед откатом!")

                self._log(f"Откат клиента Prism Launcher из {backup_name}...", "CLIENT-RESTORE", "warning")

                # 2. Root files (mmc-pack.json, patches/, libraries/) — never touch instance.cfg!
                self._progress("client_restore", 2, 4, 30, "Восстановление ядра инстанса", "mmc-pack.json, patches/, libraries/")
                mmc_bak = os.path.join(backup_path, "mmc-pack.json")
                if os.path.isfile(mmc_bak):
                    shutil.copy2(mmc_bak, os.path.join(inst, "mmc-pack.json"))

                for root_sub in ("patches", "libraries"):
                    src_sub = os.path.join(backup_path, root_sub)
                    if os.path.isdir(src_sub):
                        dst_sub = os.path.join(inst, root_sub)
                        rmtree_force(dst_sub)
                        shutil.copytree(src_sub, dst_sub)

                # 3. Restore mods/ and config/
                self._progress("client_restore", 3, 4, 60, "Восстановление mods/ и config/", "Ваши saves, карты и options.txt нетронуты")
                for folder in ("mods", "config"):
                    src_f = os.path.join(backup_path, folder)
                    if os.path.isdir(src_f):
                        dst_f = os.path.join(mc_dir, folder)
                        rmtree_force(dst_f)
                        shutil.copytree(src_f, dst_f)

                # 4. Restore resourcepacks/
                self._progress("client_restore", 4, 4, 88, "Восстановление resourcepacks/", backup_name)
                src_rp = os.path.join(backup_path, "resourcepacks")
                if os.path.isdir(src_rp):
                    dst_rp = os.path.join(mc_dir, "resourcepacks")
                    rmtree_force(dst_rp)
                    shutil.copytree(src_rp, dst_rp)

                self._progress("client_restore", 4, 4, 100, "Клиент восстановлен", backup_name, state="done")
                self._log(f"Клиент успешно откачен из {backup_name}! Все сохранения (saves, journeymap, visualprospecting, instance.cfg) сохранены.", "CLIENT-RESTORE", "success")
            except Exception as e:
                self._log(f"Ошибка отката клиента: {e}", "CLIENT-RESTORE", "error")
                self._progress("client_restore", 4, 4, 100, "Ошибка отката", str(e), state="error")
            finally:
                self._busy.release()
                self._ui_eval("window.onRestoreComplete && window.onRestoreComplete()")

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True}

    def delete_client_backup(self, backup_name: str, prism_dir: str | None = None) -> dict:
        inst = self._resolve_prism_dir(prism_dir)
        if not inst or not os.path.isdir(inst):
            return {"success": False, "error": "Папка инстанса не найдена."}
        if not backup_name or not backup_name.startswith("_gtnh_client_backup_") or "/" in backup_name or "\\" in backup_name or ".." in backup_name:
            return {"success": False, "error": "Некорректное имя бэкапа."}
        full = os.path.join(inst, backup_name)
        if not os.path.isdir(full):
            return {"success": False, "error": "Бэкап не найден."}
        try:
            rmtree_force(full)
            self._log(f"Локальный бэкап клиента удалён: {backup_name}", "BACKUP", "info")
            return {"success": True}
        except Exception as e:
            self._log(f"Ошибка удаления бэкапа клиента: {e}", "BACKUP", "error")
            return {"success": False, "error": str(e)}
