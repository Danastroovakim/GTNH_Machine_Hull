import os
import posixpath
import time
from datetime import datetime

from app.constants import ARCHIVE_EXTENSIONS, SERVER_STRICT_PROTECTED_NAMES
from app.updater.analyzer import locate_prism_mc_dir
from app.utils import format_size, is_changelog_file, is_server_core_jar


def run_server_update(
    client,
    telemetry,
    send_command_fn,
    srv_src_mode: str,
    server_zip: str,
    server_url: str,
    confirmed_server_mods: list,
    protected_configs: list[str],
    create_safety: bool,
    auto_start: bool,
    prism_dir: str,
    log_fn,
    progress_fn,
) -> None:
    """
    Remote GTNH Server update via Bisquit.Host (Calagopus) API with atomic swap and automatic safety rollback.
    If any failure happens during the swap or custom file restore phase, previous /mods, /libraries, /config,
    and core JARs are automatically restored from /_gtnh_update_rollback/.
    """
    log_fn("=== [СЕРВЕР] СТАРТ ОБНОВЛЕНИЯ BISQUIT.HOST ===", "SERVER-UPD", "warning")
    if not client.is_configured():
        raise RuntimeError("Не настроены API-ключ и ID сервера Bisquit.Host.")

    world_name, _ = client.detect_world_and_backup_paths(force=True)
    staging_name = "_gtnh_update_staging"
    staging_path = f"/{staging_name}"
    rollback_name = "_gtnh_update_rollback"
    rollback_path = f"/{rollback_name}"

    strict_protected = set(SERVER_STRICT_PROTECTED_NAMES)
    strict_protected.add(world_name.lower())

    # Normalize custom server mods
    local_client_mods_dir = None
    if prism_dir:
        mc_probe = locate_prism_mc_dir(prism_dir)
        if mc_probe:
            local_client_mods_dir = os.path.join(mc_probe, "mods")

    custom_mod_specs = []
    for entry in confirmed_server_mods:
        if isinstance(entry, dict):
            rel = str(entry.get("rel_path") or entry.get("name") or "").replace("\\", "/").lstrip("/")
            lpath = (entry.get("local_path") or "").strip()
        else:
            rel = str(entry).replace("\\", "/").lstrip("/")
            lpath = ""
        if not rel:
            continue
        if not lpath and local_client_mods_dir:
            cand = os.path.join(local_client_mods_dir, *rel.split("/"))
            if os.path.isfile(cand):
                lpath = cand
        custom_mod_specs.append({"rel_path": rel, "local_path": lpath})

    # 1. Save world & stop server
    progress_fn("updater", 1, 8, 5, "Сервер 1/8: Остановка сервера", "Отправка save-all и stop...")
    log_fn("[СЕРВЕР 1/8] Сохранение мира (save-all) и остановка сервера...", "SERVER-UPD", "warning")
    cur_state = telemetry.get_status().get("state")
    if cur_state == "running":
        send_command_fn("save-all")
        time.sleep(3)
    if cur_state != "offline":
        client.send_power("stop")
        for i in range(60):
            if telemetry.get_status().get("state") == "offline":
                break
            progress_fn("updater", 1, 8, 5 + min(5, i // 6), "Сервер 1/8: Остановка сервера", f"Ожидание OFFLINE ({(i + 1) * 3}с)...")
            time.sleep(3)
        else:
            log_fn("Сервер не остановился за 3 минуты — отправка KILL...", "SERVER-UPD", "error")
            client.send_power("kill")
            time.sleep(6)
            if telemetry.get_status().get("state") != "offline":
                raise RuntimeError("Не удалось перевести сервер в статус OFFLINE! Обновление отменено.")
    log_fn("Сервер полностью остановлен (OFFLINE).", "SERVER-UPD", "success")

    root_items = client.list_dir("/")
    root_map = {i.get("name"): i for i in root_items if i.get("name")}

    # 2. Optional safety archive
    if create_safety:
        progress_fn("updater", 2, 8, 12, "Сервер 2/8: Страховочный архив", "Сжатие World + mods + config в /backups...")
        log_fn("[СЕРВЕР 2/8] Создание страховочного архива pre_update_safety_*.tar.gz в /backups...", "SERVER-UPD", "info")
        client.create_folder("/", "backups")
        safety_candidates = [
            world_name, "mods", "config", "serverutilities",
            "visualprospecting", "java9args.txt", "server.properties",
        ]
        safety_files = [f for f in safety_candidates if f in root_map]
        if safety_files:
            stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            safety_name = f"pre_update_safety_{stamp}.tar.gz"
            created_name = client.compress_files(
                "/",
                safety_files,
                safety_name,
                fmt="tar_gz",
                foreground=True,
                timeout=600,
                on_progress=lambda p, msg: progress_fn("updater", 2, 8, 12 + int((p or 50) * 0.12), "Сервер 2/8: Страховочный архив", msg),
            )
            client.rename_files("/", [{"from": created_name, "to": f"backups/{safety_name}"}])
            log_fn(f"Страховочный архив создан: /backups/{safety_name}", "SERVER-UPD", "success")
        else:
            log_fn("В корне сервера не найдено файлов для страховочного архива — пропуск.", "SERVER-UPD", "warning")
    else:
        log_fn("[СЕРВЕР 2/8] Страховочный архив отключён пользователем.", "SERVER-UPD", "warning")

    # 3. Staging upload / pull + decompress
    progress_fn("updater", 3, 8, 25, "Сервер 3/8: Загрузка архива", f"Подготовка {staging_path}...")
    log_fn(f"[СЕРВЕР 3/8] Подготовка {staging_path}, загрузка и распаковка серверного архива...", "SERVER-UPD", "warning")
    client.delete_files("/", [staging_name], permanent=True, raise_on_error=False)
    client.create_folder("/", staging_name)

    if srv_src_mode == "zip":
        if not os.path.isfile(server_zip):
            raise RuntimeError(f"Локальный серверный ZIP не найден: {server_zip}")
        zip_fname = os.path.basename(server_zip)
        zip_sz = format_size(os.path.getsize(server_zip))
        log_fn(f"Потоковая выгрузка {zip_fname} ({zip_sz}) в {staging_path} через /files/upload...", "SERVER-UPD", "info")
        client.upload_file(
            staging_path,
            server_zip,
            remote_filename=zip_fname,
            timeout=900,
            on_progress=lambda p, msg: progress_fn("updater", 3, 8, 25 + int((p or 0) * 0.30), "Сервер 3/8: Выгрузка ZIP", msg),
        )
        archive_filename = zip_fname
    else:
        log_fn(f"Скачивание архива сервером по URL через POST /files/pull в {staging_path}...", "SERVER-UPD", "info")
        client.pull_file(
            staging_path,
            server_url,
            timeout=600,
            on_progress=lambda p, msg: progress_fn("updater", 3, 8, 25 + int((p or 50) * 0.30), "Сервер 3/8: Скачивание по URL", msg),
        )
        pulled_items = client.list_dir(staging_path)
        pulled_files = [it.get("name") for it in pulled_items if client.is_file_entry(it) and it.get("name")]
        if not pulled_files:
            raise RuntimeError("После POST /files/pull в /_gtnh_update_staging не обнаружен скачанный архив!")
        archive_filename = next((f for f in pulled_files if f.lower().endswith(".zip")), pulled_files[0])
        if not archive_filename.lower().endswith(ARCHIVE_EXTENSIONS):
            new_arc = archive_filename + ".zip"
            client.rename_files(staging_path, [{"from": archive_filename, "to": new_arc}])
            archive_filename = new_arc

    progress_fn("updater", 3, 8, 56, "Сервер 3/8: Распаковка архива", f"Распаковка {archive_filename} в {staging_path}...")
    log_fn(f"Распаковка {archive_filename} в {staging_path} (foreground=True)...", "SERVER-UPD", "info")
    client.decompress_file(
        staging_path,
        archive_filename,
        foreground=True,
        timeout=600,
        on_progress=lambda p, msg: progress_fn("updater", 3, 8, 56 + int((p or 50) * 0.10), "Сервер 3/8: Распаковка архива", msg),
    )

    # Free disk quota immediately by deleting source ZIP inside staging
    client.delete_files(staging_path, [archive_filename], permanent=True, raise_on_error=False)

    # Locate unpacked root inside staging
    staged_items = client.list_dir(staging_path)
    staged_map = {i.get("name"): i for i in staged_items if i.get("name")}
    staging_root = staging_path
    if "mods" not in staged_map:
        sub_dirs = [n for n, obj in staged_map.items() if not client.is_file_entry(obj)]
        if len(sub_dirs) == 1:
            cand_root = f"{staging_path}/{sub_dirs[0]}"
            cand_items = client.list_dir(cand_root)
            cand_map = {i.get("name"): i for i in cand_items if i.get("name")}
            if "mods" in cand_map:
                staging_root = cand_root
                staged_map = cand_map

    if "mods" not in staged_map or "libraries" not in staged_map:
        client.delete_files("/", [staging_name], permanent=True, raise_on_error=False)
        raise RuntimeError("В распакованном серверном архиве не найдены mods/ и libraries/! Обновление отменено, сервер не изменён.")

    # Pre-scan live config and live mods BEFORE atomic swap
    live_cfg_names = [i.get("name") for i in client.list_dir("/config") if i.get("name")] if "config" in root_map else []
    new_cfg_low = {i.get("name", "").lower() for i in client.list_dir(f"{staging_root}/config") if i.get("name")} if "config" in staged_map else set()
    auto_custom_configs = [c for c in live_cfg_names if c.lower() not in new_cfg_low]

    current_srv_mods = client.list_remote_mods("/mods") if "mods" in root_map else {}
    protected_stat = client.stat_files("/config", protected_configs) if ("config" in root_map and protected_configs) else {}
    existing_protected = [
        (rel_cfg, client.is_file_entry(info))
        for rel_cfg, info in protected_stat.items()
        if info is not None
    ]

    # 4. Atomic Swap Preparation: move old pack files into /_gtnh_update_rollback (instant rename, zero extra disk!)
    progress_fn("updater", 4, 8, 68, "Сервер 4/8: Резерв для авто-отката", f"Перенос старой сборки в {rollback_path}...")
    log_fn(f"[СЕРВЕР 4/8] Атомарное резервирование старых mods/, libraries/, config/ и ядра в {rollback_path}...", "SERVER-UPD", "warning")

    client.delete_files("/", [rollback_name], permanent=True, raise_on_error=False)
    client.create_folder("/", rollback_name)

    new_root_jars = {
        n for n, obj in staged_map.items()
        if client.is_file_entry(obj) and n.lower().endswith(".jar")
    }
    to_swap_out = []
    for name in root_map.keys():
        low = name.lower()
        if low in strict_protected:
            continue
        if low in ("mods", "libraries", "config") or is_server_core_jar(name) or name in new_root_jars or is_changelog_file(name):
            to_swap_out.append(name)

    swapped_to_rollback: list[str] = []
    moved_into_root: list[str] = []

    try:
        if to_swap_out:
            # Rename one batch into rollback_name/
            rollback_moves = [{"from": name, "to": f"{rollback_name}/{name}"} for name in to_swap_out]
            client.rename_files("/", rollback_moves)
            swapped_to_rollback = list(to_swap_out)
            log_fn(f"Старая сборка зарезервирована в {rollback_path} ({len(swapped_to_rollback)} элементов).", "SERVER-UPD", "info")

        # 5. Move new pack files from staging into /
        progress_fn("updater", 5, 8, 76, "Сервер 5/8: Установка новой сборки", "Перенос mods/, libraries/, config/ и ядра в /...")
        log_fn("[СЕРВЕР 5/8] Перенос новых mods/, libraries/, config/, JAR-файлов ядра и changelog в /...", "SERVER-UPD", "warning")
        staging_rel = staging_root.lstrip("/")
        moves = []

        for folder in ("mods", "libraries", "config"):
            if folder in staged_map:
                moves.append({"from": f"{staging_rel}/{folder}", "to": folder})

        for name, obj in staged_map.items():
            if not client.is_file_entry(obj):
                continue
            low = name.lower()
            if low in strict_protected:
                if low == "java9args.txt":
                    if "java9args.txt" in root_map:
                        if "java9args.txt.new" in root_map:
                            client.delete_files("/", ["java9args.txt.new"], permanent=True, raise_on_error=False)
                        moves.append({"from": f"{staging_rel}/{name}", "to": "java9args.txt.new"})
                        log_fn("Новый java9args.txt сохранён рядом как /java9args.txt.new (рабочий защищён).", "SERVER-UPD", "success")
                    else:
                        moves.append({"from": f"{staging_rel}/{name}", "to": "java9args.txt"})
                continue

            if low.endswith(".jar") or is_changelog_file(name):
                moves.append({"from": f"{staging_rel}/{name}", "to": name})

        if moves:
            client.rename_files("/", moves)
            moved_into_root = [m["to"] for m in moves]
            log_fn(f"Перенесено основных элементов новой сборки: {len(moves)} шт.", "SERVER-UPD", "success")

        # 6. Restore custom server mods & protected/custom configs
        progress_fn("updater", 6, 8, 84, "Сервер 6/8: Кастомные моды и конфиги", "Возврат пользовательских файлов...")
        log_fn("[СЕРВЕР 6/8] Восстановление/выгрузка кастомных модов в /mods и защищённых конфигов...", "SERVER-UPD", "info")

        for spec in custom_mod_specs:
            rel = spec["rel_path"]
            lpath = spec["local_path"]
            rel_parent = posixpath.dirname(rel)
            fname = posixpath.basename(rel)
            target_dir = f"/mods/{rel_parent}" if rel_parent else "/mods"

            if rel_parent:
                client.ensure_dir_path(target_dir)

            if client.remote_entry_info(f"/mods/{rel}") is not None:
                client.delete_files(target_dir, [fname], permanent=True, raise_on_error=False)

            uploaded_ok = False
            if lpath and os.path.isfile(lpath):
                try:
                    client.upload_file(target_dir, lpath, remote_filename=fname, timeout=300)
                    uploaded_ok = True
                    log_fn(f"  + Выгружен кастомный мод: /mods/{rel}", "SERVER-UPD", "success")
                except Exception as up_err:
                    log_fn(f"  ! Ошибка /files/upload для {rel} ({up_err}), беру резерв из {rollback_path}...", "SERVER-UPD", "warning")

            if not uploaded_ok and rel in current_srv_mods:
                client.rename_files("/", [{
                    "from": f"{rollback_name}/mods/{rel}",
                    "to": f"mods/{rel}",
                }])
                log_fn(f"  + Восстановлен кастомный мод сервера: /mods/{rel}", "SERVER-UPD", "success")

        # Restore explicit protected configs from rollback
        for rel_cfg, is_file in existing_protected:
            rel_parent = posixpath.dirname(rel_cfg)
            fname = posixpath.basename(rel_cfg)
            target_parent = f"/config/{rel_parent}" if rel_parent else "/config"
            if rel_parent:
                client.ensure_dir_path(target_parent)

            existing = client.remote_entry_info(f"/config/{rel_cfg}")
            if is_file:
                if existing is not None:
                    client.delete_files(target_parent, [fname], permanent=True, raise_on_error=False)
                client.rename_files("/", [{
                    "from": f"{rollback_name}/config/{rel_cfg}",
                    "to": f"config/{rel_cfg}",
                }])
            else:
                if existing is not None:
                    client.merge_directory_tree(
                        f"{rollback_path}/config/{rel_cfg}",
                        f"/config/{rel_cfg}",
                    )
                else:
                    client.rename_files("/", [{
                        "from": f"{rollback_name}/config/{rel_cfg}",
                        "to": f"config/{rel_cfg}",
                    }])
            log_fn(f"  + Восстановлен защищённый конфиг сервера: /config/{rel_cfg}", "SERVER-UPD", "success")

        # Auto-restore custom server mod configs not present in new pack
        if auto_custom_configs:
            protected_top = {rc.split("/", 1)[0].lower() for rc, _ in existing_protected}
            custom_cfg_moves = [
                {"from": f"{rollback_name}/config/{cname}", "to": f"config/{cname}"}
                for cname in auto_custom_configs
                if cname.lower() not in protected_top
            ]
            if custom_cfg_moves:
                client.rename_files("/", custom_cfg_moves)
                log_fn(
                    f"  + Авто-сохранены конфиги кастомных серверных модов ({len(custom_cfg_moves)} шт.): {', '.join(auto_custom_configs[:8])}",
                    "SERVER-UPD",
                    "success",
                )

    except Exception as swap_err:
        # AUTOMATIC SAFETY ROLLBACK
        log_fn(f"СБОЙ НА ЭТАПЕ ЗАМЕНЫ ФАЙЛОВ ({swap_err}) — ЗАПУСК АВТОМАТИЧЕСКОГО ОТКАТА!", "SERVER-UPD", "error")
        progress_fn("updater", 6, 8, 85, "Автоматический откат сервера", "Восстановление исходной сборки из резерва...", state="error")
        rollback_ok = True
        try:
            # Remove partially moved new entries
            cleanup_new = [x for x in moved_into_root if x != "java9args.txt.new"]
            if cleanup_new:
                client.delete_files("/", cleanup_new, permanent=True, raise_on_error=False)

            # Move back everything from /_gtnh_update_rollback to /
            if swapped_to_rollback:
                rb_entries = {i.get("name") for i in client.list_dir(rollback_path) if i.get("name")}
                restore_moves = [
                    {"from": f"{rollback_name}/{name}", "to": name}
                    for name in swapped_to_rollback
                    if name in rb_entries
                ]
                if restore_moves:
                    client.rename_files("/", restore_moves)

            client.delete_files("/", [rollback_name, staging_name], permanent=True, raise_on_error=False)
            log_fn("АВТО-ОТКАТ УСПЕШЕН: исходные mods/, libraries/, config/ и ядро возвращены в рабочее состояние!", "SERVER-UPD", "warning")
        except Exception as rb_err:
            rollback_ok = False
            log_fn(
                f"ВНИМАНИЕ! Авто-откат завершился с ошибкой ({rb_err}). Исходные файлы находятся в {rollback_path} (и в /backups/pre_update_safety_*). Перенесите папки вручную из {rollback_path} в /.",
                "SERVER-UPD",
                "error",
            )
        suffix = " Сервер автоматически откачен в исходное состояние." if rollback_ok else f" Проверьте папку {rollback_path}!"
        raise RuntimeError(f"{swap_err}.{suffix}") from swap_err

    # 7. Final permanent cleanup of staging & rollback folders
    progress_fn("updater", 7, 8, 94, "Сервер 7/8: Очистка временных папок", f"Удаление {staging_path} и {rollback_path}...")
    log_fn(f"[СЕРВЕР 7/8] Безвозвратная очистка {staging_path} и {rollback_path} (permanent=True)...", "SERVER-UPD", "info")
    client.delete_files("/", [staging_name, rollback_name], permanent=True, raise_on_error=False)

    # 8. Optional auto-start
    log_fn(
        "[СЕРВЕР 8/8] Защищённые данные (World, serverutilities, visualprospecting, server.properties, whitelist/ops, java9args.txt) полностью сохранены!",
        "SERVER-UPD",
        "success",
    )
    if auto_start:
        progress_fn("updater", 8, 8, 100, "Сервер обновлён", "Запуск обновлённого сервера GTNH...", state="done")
        log_fn("Автозапуск обновлённого сервера GTNH...", "SERVER-UPD", "success")
        client.send_power("start")
    else:
        progress_fn("updater", 8, 8, 100, "Сервер обновлён", "Готов к запуску (автозапуск отключён)", state="done")
        log_fn("Автозапуск отключён. При необходимости проверьте /java9args.txt.new и нажмите START.", "SERVER-UPD", "info")
