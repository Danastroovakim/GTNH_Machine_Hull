import os
import posixpath
import shutil
import zipfile
from datetime import datetime

from app.constants import CLIENT_STRICT_PROTECTED_FILES
from app.updater.analyzer import check_client_running_locks, inspect_client_zip, locate_prism_mc_dir
from app.utils import (
    extract_mod_base_key,
    is_changelog_file,
    is_disabled_mod,
    rmtree_force,
    safe_extract_zip_entry,
)


def run_client_update(
    prism_dir: str,
    client_zip: str,
    confirmed_custom_mods: list[str],
    confirmed_custom_rps: list[str],
    protected_configs: list[str],
    log_fn,
    progress_fn,
) -> dict:
    """Zero-Loss local Prism Launcher client update with live progress reporting."""
    log_fn("=== [КЛИЕНТ] СТАРТ ОБНОВЛЕНИЯ PRISM LAUNCHER ===", "CLIENT-UPD", "warning")
    progress_fn("updater", 1, 5, 2, "Клиент 1/5: Проверка блокировок", prism_dir)

    mc_dir = locate_prism_mc_dir(prism_dir)
    if not mc_dir:
        raise RuntimeError("Не найдена папка .minecraft внутри инстанса Prism Launcher.")
    if not os.path.isfile(client_zip):
        raise RuntimeError(f"Клиентский архив не найден: {client_zip}")

    # 1. Check active locks
    log_fn("[КЛИЕНТ 1/5] Проверка отсутствия активных блокировок session.lock и логов...", "CLIENT-UPD", "info")
    is_locked, locked_items = check_client_running_locks(mc_dir)
    if is_locked:
        raise RuntimeError(
            f"Клиент Minecraft запущен (блокировка: {', '.join(locked_items)})! Закройте игру и повторите."
        )

    cz_info = inspect_client_zip(client_zip)
    root_prefix = cz_info["root_prefix"]
    mc_prefix = cz_info["mc_prefix"]

    # 2. Local safety backup
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    backup_name = f"_gtnh_client_backup_{stamp}"
    backup_dir = os.path.join(prism_dir, backup_name)
    log_fn(f"[КЛИЕНТ 2/5] Создание локального бэкапа: {backup_name}...", "CLIENT-UPD", "info")
    progress_fn("updater", 2, 5, 10, "Клиент 2/5: Создание бэкапа", f"Копирование в {backup_name}...")
    os.makedirs(backup_dir, exist_ok=True)

    mmc_pack_src = os.path.join(prism_dir, "mmc-pack.json")
    if os.path.isfile(mmc_pack_src):
        shutil.copy2(mmc_pack_src, os.path.join(backup_dir, "mmc-pack.json"))

    for root_dir_name in ("patches", "libraries"):
        src_root_sub = os.path.join(prism_dir, root_dir_name)
        if os.path.isdir(src_root_sub):
            shutil.copytree(src_root_sub, os.path.join(backup_dir, root_dir_name))

    backup_folders = ("mods", "config", "resourcepacks")
    for idx, folder_name in enumerate(backup_folders, start=1):
        src_folder = os.path.join(mc_dir, folder_name)
        if os.path.isdir(src_folder):
            progress_fn("updater", 2, 5, 10 + idx * 6, "Клиент 2/5: Создание бэкапа", f"Копирование .minecraft/{folder_name}/...")
            shutil.copytree(src_folder, os.path.join(backup_dir, folder_name))

    log_fn(f"Локальный бэкап клиента создан: {backup_name}", "CLIENT-UPD", "success")

    # 3. Update mmc-pack.json, patches/, libraries/ (never touch instance.cfg)
    log_fn("[КЛИЕНТ 3/5] Обновление mmc-pack.json, patches/ и libraries/ (instance.cfg защищён)...", "CLIENT-UPD", "info")
    progress_fn("updater", 3, 5, 32, "Клиент 3/5: Ядро инстанса", "Обновление mmc-pack.json, patches/, libraries/")

    with zipfile.ZipFile(client_zip, "r") as zf:
        all_infos = zf.infolist()
        all_zip_names = [i.filename.replace("\\", "/") for i in all_infos]

        mmc_zip_entry = f"{root_prefix}mmc-pack.json"
        if mmc_zip_entry in all_zip_names:
            safe_extract_zip_entry(zf, zf.getinfo(mmc_zip_entry), os.path.join(prism_dir, "mmc-pack.json"), prism_dir)
            log_fn("Файл mmc-pack.json обновлён; instance.cfg нетронут.", "CLIENT-UPD", "success")

        patches_pref = f"{root_prefix}patches/"
        libs_pref = f"{root_prefix}libraries/"
        has_zip_patches = any(n.startswith(patches_pref) for n in all_zip_names)
        has_zip_libs = any(n.startswith(libs_pref) for n in all_zip_names)

        patches_dir = os.path.join(prism_dir, "patches")
        libs_dir = os.path.join(prism_dir, "libraries")
        if has_zip_patches:
            rmtree_force(patches_dir)
            os.makedirs(patches_dir, exist_ok=True)
        if has_zip_libs:
            rmtree_force(libs_dir)
            os.makedirs(libs_dir, exist_ok=True)

        # 4. Extract mods/ (clean replace, including OpenSecurity sounds & 1.7.10/) + Smart Overlay Merge for config/
        log_fn("[КЛИЕНТ 4/5] Распаковка mods/ и Smart Overlay Merge для config/, ресурспаков, changelog и *.lang...", "CLIENT-UPD", "warning")
        progress_fn("updater", 4, 5, 36, "Клиент 4/5: Распаковка архива", "Очистка старых модов и распаковка новой сборки...")

        mods_dir = os.path.join(mc_dir, "mods")
        config_dir = os.path.join(mc_dir, "config")
        rp_dir = os.path.join(mc_dir, "resourcepacks")

        rmtree_force(mods_dir)
        os.makedirs(mods_dir, exist_ok=True)
        os.makedirs(config_dir, exist_ok=True)
        os.makedirs(rp_dir, exist_ok=True)

        # Clean old changelog from *.md
        for fn in os.listdir(mc_dir):
            if is_changelog_file(fn):
                try:
                    os.remove(os.path.join(mc_dir, fn))
                except Exception:
                    pass

        mods_pref = mc_prefix + "mods/"
        cfg_pref = mc_prefix + "config/"
        rp_pref = mc_prefix + "resourcepacks/"
        su_pref = mc_prefix + "serverutilities/"

        extracted_mods = 0
        extracted_mod_assets = 0
        extracted_cfg = 0
        extracted_rp = 0

        total_entries = max(1, len(all_infos))
        last_reported_pct = 36

        for idx, info in enumerate(all_infos):
            if info.is_dir():
                continue
            norm = info.filename.replace("\\", "/")

            if has_zip_patches and norm.startswith(patches_pref):
                rel = norm[len(patches_pref):]
                if rel:
                    safe_extract_zip_entry(zf, info, os.path.join(patches_dir, *rel.split("/")), patches_dir)
            elif has_zip_libs and norm.startswith(libs_pref):
                rel = norm[len(libs_pref):]
                if rel:
                    safe_extract_zip_entry(zf, info, os.path.join(libs_dir, *rel.split("/")), libs_dir)
            elif norm.startswith(mods_pref):
                rel = norm[len(mods_pref):]
                if rel:
                    safe_extract_zip_entry(zf, info, os.path.join(mods_dir, *rel.split("/")), mods_dir)
                    if "/" not in rel or rel.startswith("1.7.10/"):
                        extracted_mods += 1
                    else:
                        extracted_mod_assets += 1
            elif norm.startswith(cfg_pref):
                rel = norm[len(cfg_pref):]
                if rel:
                    safe_extract_zip_entry(zf, info, os.path.join(config_dir, *rel.split("/")), config_dir)
                    extracted_cfg += 1
            elif norm.startswith(rp_pref):
                rel = norm[len(rp_pref):]
                if rel:
                    safe_extract_zip_entry(zf, info, os.path.join(rp_dir, *rel.split("/")), rp_dir)
                    extracted_rp += 1
            elif norm.startswith(su_pref):
                rel = norm[len(su_pref):]
                if rel:
                    su_dst = os.path.join(mc_dir, "serverutilities", *rel.split("/"))
                    if not os.path.exists(su_dst):
                        safe_extract_zip_entry(zf, info, su_dst, mc_dir)
            elif norm.startswith(mc_prefix):
                rel_mc = norm[len(mc_prefix):]
                if "/" not in rel_mc:
                    low_mc = rel_mc.lower()
                    if low_mc in CLIENT_STRICT_PROTECTED_FILES:
                        continue
                    if is_changelog_file(rel_mc) or low_mc.endswith(".lang"):
                        safe_extract_zip_entry(zf, info, os.path.join(mc_dir, rel_mc), mc_dir)

            pct = 36 + int((idx / total_entries) * 50)
            if pct >= last_reported_pct + 2:
                last_reported_pct = pct
                progress_fn(
                    "updater",
                    4,
                    5,
                    pct,
                    "Клиент 4/5: Распаковка архива",
                    f"Файлы: {idx}/{total_entries} (моды: {extracted_mods}, конфиги: {extracted_cfg})",
                )

        log_fn(
            f"Извлечено: {extracted_mods} модов, {extracted_mod_assets} ассетов модов, {extracted_cfg} конфигов (Overlay), {extracted_rp} файлов ресурспаков.",
            "CLIENT-UPD",
            "info",
        )

    # 5. Restore confirmed custom mods, resourcepacks, and protected configs
    log_fn("[КЛИЕНТ 5/5] Возврат кастомных модов, ресурспаков и защищённых конфигов...", "CLIENT-UPD", "info")
    progress_fn("updater", 5, 5, 90, "Клиент 5/5: Кастомные файлы", "Возврат модов, ресурспаков и защищённых конфигов...")

    restored_mods = 0
    for rel_mod in confirmed_custom_mods:
        rel_clean = str(rel_mod).replace("\\", "/").lstrip("/")
        src_m = os.path.join(backup_dir, "mods", *rel_clean.split("/"))
        dst_m = os.path.join(mods_dir, *rel_clean.split("/"))
        if os.path.isfile(src_m):
            # If user explicitly preserves a .disabled mod, remove newly extracted enabled copy with same base key
            if is_disabled_mod(rel_clean):
                bk = extract_mod_base_key(rel_clean)
                sub_dir = os.path.dirname(dst_m)
                if os.path.isdir(sub_dir):
                    for existing in list(os.listdir(sub_dir)):
                        if not is_disabled_mod(existing) and extract_mod_base_key(existing) == bk:
                            try:
                                os.remove(os.path.join(sub_dir, existing))
                                log_fn(f"  ~ Отключённый мод {rel_clean}: удалён штатный дубликат {existing}", "CLIENT-UPD", "info")
                            except Exception:
                                pass
            os.makedirs(os.path.dirname(dst_m), exist_ok=True)
            shutil.copy2(src_m, dst_m)
            restored_mods += 1
            log_fn(f"  + Возвращён кастомный мод: {rel_clean}", "CLIENT-UPD", "success")

    restored_rps = 0
    for rp_name in confirmed_custom_rps:
        src_rp = os.path.join(backup_dir, "resourcepacks", rp_name)
        dst_rp = os.path.join(rp_dir, rp_name)
        if os.path.isdir(src_rp):
            shutil.copytree(src_rp, dst_rp, dirs_exist_ok=True)
            restored_rps += 1
            log_fn(f"  + Сохранён ресурспак (папка): {rp_name}", "CLIENT-UPD", "success")
        elif os.path.isfile(src_rp):
            shutil.copy2(src_rp, dst_rp)
            restored_rps += 1
            log_fn(f"  + Сохранён ресурспак: {rp_name}", "CLIENT-UPD", "success")

    restored_cfgs = 0
    for cfg_rel in protected_configs:
        src_c = os.path.join(backup_dir, "config", *cfg_rel.split("/"))
        dst_c = os.path.join(config_dir, *cfg_rel.split("/"))
        if os.path.isdir(src_c):
            shutil.copytree(src_c, dst_c, dirs_exist_ok=True)
            restored_cfgs += 1
            log_fn(f"  + Восстановлена папка конфига: config/{cfg_rel}", "CLIENT-UPD", "success")
        elif os.path.isfile(src_c):
            os.makedirs(os.path.dirname(dst_c), exist_ok=True)
            shutil.copy2(src_c, dst_c)
            restored_cfgs += 1
            log_fn(f"  + Восстановлен защищённый конфиг: config/{cfg_rel}", "CLIENT-UPD", "success")

    progress_fn(
        "updater",
        5,
        5,
        100,
        "Клиент обновлён",
        f"Версия {cz_info['target_version']} | Бэкап: {backup_name}",
        state="done",
    )
    log_fn(
        f"[КЛИЕНТ ГОТОВ] Версия: {cz_info['target_version']} | Кастомных модов: {restored_mods}, ресурспаков: {restored_rps}, конфигов: {restored_cfgs}. Бэкап: {backup_name}.",
        "CLIENT-UPD",
        "success",
    )
    return {
        "version": cz_info["target_version"],
        "backup_name": backup_name,
        "restored_mods": restored_mods,
        "restored_rps": restored_rps,
        "restored_cfgs": restored_cfgs,
    }
