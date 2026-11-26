import os
import posixpath
import re
import zipfile

from app.constants import MOD_FILE_EXTENSIONS
from app.utils import (
    HttpRangeFile,
    extract_mod_base_key,
    format_size,
    is_changelog_file,
    is_disabled_mod,
    is_file_actively_locked,
    parse_changelog_version,
)


def locate_prism_mc_dir(instance_dir: str) -> str | None:
    if not instance_dir or not os.path.isdir(instance_dir):
        return None
    for sub in (".minecraft", "minecraft"):
        cand = os.path.join(instance_dir, sub)
        if os.path.isdir(cand):
            return cand
    return None


def check_client_running_locks(mc_dir: str) -> tuple[bool, list[str]]:
    locked_items = []
    saves_dir = os.path.join(mc_dir, "saves")
    if os.path.isdir(saves_dir):
        try:
            for entry in os.listdir(saves_dir):
                slock = os.path.join(saves_dir, entry, "session.lock")
                if os.path.isfile(slock) and is_file_actively_locked(slock):
                    locked_items.append(f"saves/{entry}/session.lock")
        except Exception:
            pass

    for rel_log in ("logs/latest.log", "logs/fml-client-latest.log"):
        p = os.path.join(mc_dir, *rel_log.split("/"))
        if os.path.isfile(p) and is_file_actively_locked(p):
            locked_items.append(rel_log)

    return len(locked_items) > 0, locked_items


def scan_local_mods(mods_dir: str) -> dict[str, int]:
    """
    Scan mods/ and mods/1.7.10/ strictly for mod archive files.
    Resource/runtime folders (OpenSecurity/, ic2/, carpentersblocks/) are ignored so they never cause false custom mods.
    """
    mods: dict[str, int] = {}
    if not os.path.isdir(mods_dir):
        return mods
    try:
        for entry in os.listdir(mods_dir):
            full = os.path.join(mods_dir, entry)
            if os.path.isfile(full) and entry.lower().endswith(MOD_FILE_EXTENSIONS):
                mods[entry] = os.path.getsize(full)
            elif os.path.isdir(full) and entry == "1.7.10":
                for sub in os.listdir(full):
                    sub_full = os.path.join(full, sub)
                    if os.path.isfile(sub_full) and sub.lower().endswith(MOD_FILE_EXTENSIONS):
                        mods[f"1.7.10/{sub}"] = os.path.getsize(sub_full)
    except Exception:
        pass
    return mods


def inspect_client_zip(zip_path: str) -> dict:
    """Inspect a Prism Launcher / MultiMC GTNH client ZIP archive."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        names = [i.filename.replace("\\", "/") for i in infos]

        root_prefix = ""
        mc_prefix = None

        for n in names:
            low = n.lower()
            if low == "mmc-pack.json":
                root_prefix = ""
                break
            if low.endswith("/mmc-pack.json"):
                root_prefix = n[:-len("mmc-pack.json")]
                break

        for cand_mc in (f"{root_prefix}.minecraft/", f"{root_prefix}minecraft/"):
            if any(n.startswith(cand_mc) for n in names):
                mc_prefix = cand_mc
                break

        if mc_prefix is None:
            for n in names:
                m = re.match(r"^(.*?\.?minecraft/)mods/", n, flags=re.IGNORECASE)
                if m:
                    mc_prefix = m.group(1)
                    root_prefix = mc_prefix.rsplit("/", 2)[0] + "/" if "/" in mc_prefix.rstrip("/") else ""
                    break

        if mc_prefix is None:
            raise RuntimeError("В клиентском ZIP не найдена папка .minecraft/ с модами GTNH!")

        mods: dict[str, int] = {}
        mod_asset_count = 0
        resourcepacks: set[str] = set()
        changelog_file = ""
        changelog_preview = ""
        has_mmc_pack = f"{root_prefix}mmc-pack.json" in names
        has_patches = any(n.startswith(f"{root_prefix}patches/") for n in names)
        has_libraries = any(n.startswith(f"{root_prefix}libraries/") for n in names)
        has_config = False

        mods_pref = mc_prefix + "mods/"
        cfg_pref = mc_prefix + "config/"
        rp_pref = mc_prefix + "resourcepacks/"

        for info in infos:
            norm = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            if norm.startswith(mods_pref):
                rel = norm[len(mods_pref):]
                parts = rel.split("/")
                if (len(parts) == 1 or (len(parts) == 2 and parts[0] == "1.7.10")) and rel.lower().endswith(MOD_FILE_EXTENSIONS):
                    mods[rel] = info.file_size
                elif rel:
                    mod_asset_count += 1
            elif norm.startswith(cfg_pref):
                has_config = True
            elif norm.startswith(rp_pref):
                rel_rp = norm[len(rp_pref):]
                top_rp = rel_rp.split("/", 1)[0]
                if top_rp:
                    resourcepacks.add(top_rp)
            elif norm.startswith(mc_prefix):
                rel_mc = norm[len(mc_prefix):]
                if "/" not in rel_mc and is_changelog_file(rel_mc):
                    changelog_file = rel_mc
                    try:
                        raw_cl = zf.read(info).decode("utf-8", errors="replace")
                        cl_lines = [ln for ln in raw_cl.splitlines() if ln.strip()][:28]
                        changelog_preview = "\n".join(cl_lines)
                    except Exception:
                        pass

        if not mods:
            raise RuntimeError("В клиентском ZIP не обнаружены файлы модов внутри .minecraft/mods/!")

        from_ver, to_ver = parse_changelog_version(changelog_file)
        if not to_ver and root_prefix:
            m_ver = re.search(r"(\d+\.\d+\.\d+(?:[-_a-zA-Z0-9\.]*)?)", root_prefix)
            if m_ver:
                to_ver = m_ver.group(1)

        return {
            "root_prefix": root_prefix,
            "mc_prefix": mc_prefix,
            "has_mmc_pack": has_mmc_pack,
            "has_patches": has_patches,
            "has_libraries": has_libraries,
            "has_config": has_config,
            "mods": mods,
            "mod_asset_count": mod_asset_count,
            "resourcepacks": sorted(resourcepacks),
            "changelog_file": changelog_file,
            "changelog_preview": changelog_preview,
            "from_version": from_ver or "",
            "target_version": to_ver or "Не определена",
        }


def inspect_server_zip_obj(zf: zipfile.ZipFile) -> dict:
    """Inspect a GTNH Server ZIP archive from an open ZipFile handle (local or HttpRangeFile)."""
    infos = zf.infolist()
    names = [i.filename.replace("\\", "/") for i in infos]

    prefix = ""
    if any(n.startswith("mods/") for n in names):
        prefix = ""
    else:
        for n in names:
            m = re.match(r"^([^/]+/)mods/", n)
            if m:
                prefix = m.group(1)
                break

    mods_pref = prefix + "mods/"
    libs_pref = prefix + "libraries/"
    cfg_pref = prefix + "config/"

    mods: dict[str, int] = {}
    has_libs = False
    has_config = False
    has_java9args = False
    core_jars = []
    changelog_file = ""
    changelog_preview = ""

    for info in infos:
        norm = info.filename.replace("\\", "/")
        if info.is_dir():
            continue
        if norm.startswith(mods_pref):
            rel = norm[len(mods_pref):]
            parts = rel.split("/")
            if (len(parts) == 1 or (len(parts) == 2 and parts[0] == "1.7.10")) and rel.lower().endswith(MOD_FILE_EXTENSIONS):
                mods[rel] = info.file_size
        elif norm.startswith(libs_pref):
            has_libs = True
        elif norm.startswith(cfg_pref):
            has_config = True
        elif norm.startswith(prefix):
            rel_root = norm[len(prefix):]
            if "/" not in rel_root:
                low_r = rel_root.lower()
                if low_r == "java9args.txt":
                    has_java9args = True
                elif low_r.endswith(".jar"):
                    core_jars.append(rel_root)
                elif is_changelog_file(rel_root):
                    changelog_file = rel_root
                    try:
                        raw_cl = zf.read(info).decode("utf-8", errors="replace")
                        cl_lines = [ln for ln in raw_cl.splitlines() if ln.strip()][:28]
                        changelog_preview = "\n".join(cl_lines)
                    except Exception:
                        pass

    if not mods:
        raise RuntimeError("В серверном ZIP не обнаружена папка mods/ с файлами модов!")

    from_ver, to_ver = parse_changelog_version(changelog_file)
    return {
        "prefix": prefix,
        "mods": mods,
        "has_libs": has_libs,
        "has_config": has_config,
        "has_java9args": has_java9args,
        "core_jars": sorted(core_jars),
        "changelog_file": changelog_file,
        "changelog_preview": changelog_preview,
        "from_version": from_ver or "",
        "target_version": to_ver or "Не определена",
    }


def diff_mod_lists(old_mods_map: dict[str, int], new_mods_map: dict[str, int], profile_custom_list: list[str]) -> dict:
    """
    Smart diff between installed mods and target pack mods:
    - Exact match -> unchanged
    - Disabled mods (*.disabled) -> surfaced explicitly in custom_mods with is_disabled=True so they are never silently re-enabled
    - In user profile -> custom_mods (auto-checked, with conflict_with if target pack also contains same base key)
    - Same base key as target pack mod -> updated_mods
    - Otherwise -> custom_mods (unchecked candidate for user confirmation)
    """
    new_exact_low = {k.lower(): k for k in new_mods_map.keys()}
    new_by_base: dict[str, list[str]] = {}
    for k in new_mods_map.keys():
        b = extract_mod_base_key(k)
        new_by_base.setdefault(b, []).append(k)

    prof_exact_low = {str(x).replace("\\", "/").lower() for x in (profile_custom_list or [])}
    prof_bases = {extract_mod_base_key(x) for x in (profile_custom_list or []) if x}

    custom_mods = []
    updated_mods = []
    unchanged_count = 0
    matched_new_keys = set()

    for old_rel in sorted(old_mods_map.keys(), key=lambda s: s.lower()):
        old_size = old_mods_map[old_rel]
        low_rel = old_rel.lower()
        base_key = extract_mod_base_key(old_rel)
        disabled = is_disabled_mod(old_rel)

        if low_rel in new_exact_low:
            unchanged_count += 1
            matched_new_keys.add(new_exact_low[low_rel])
            continue

        in_profile = (low_rel in prof_exact_low) or (base_key in prof_bases)
        new_matches = new_by_base.get(base_key, [])

        if disabled:
            conflict = new_matches[0] if new_matches else ""
            if new_matches:
                matched_new_keys.update(new_matches)
            custom_mods.append({
                "rel_path": old_rel,
                "name": posixpath.basename(old_rel),
                "base_key": base_key,
                "size_str": format_size(old_size),
                "in_profile": in_profile,
                "is_disabled": True,
                "checked": in_profile,
                "conflict_with": conflict,
            })
        elif in_profile:
            conflict = new_matches[0] if new_matches else ""
            custom_mods.append({
                "rel_path": old_rel,
                "name": posixpath.basename(old_rel),
                "base_key": base_key,
                "size_str": format_size(old_size),
                "in_profile": True,
                "is_disabled": False,
                "checked": True,
                "conflict_with": conflict,
            })
        elif new_matches:
            matched_new_keys.update(new_matches)
            updated_mods.append({
                "old": old_rel,
                "new": new_matches[0],
            })
        else:
            custom_mods.append({
                "rel_path": old_rel,
                "name": posixpath.basename(old_rel),
                "base_key": base_key,
                "size_str": format_size(old_size),
                "in_profile": False,
                "is_disabled": False,
                "checked": False,
                "conflict_with": "",
            })

    new_added = sorted(set(new_mods_map.keys()) - matched_new_keys, key=lambda s: s.lower())
    return {
        "custom_mods": custom_mods,
        "updated_mods": updated_mods,
        "new_added_mods": new_added,
        "unchanged_count": unchanged_count,
        "new_added_count": len(new_added),
        "old_total": len(old_mods_map),
        "new_total": len(new_mods_map),
    }


def analyze_pack_update(params: dict, store, client, log_fn) -> dict:
    """Step 1 (Analyze & Confirm): read-only inspection of local client and/or Bisquit.Host server."""
    mode = params.get("mode", "client")
    prism_dir = (params.get("prism_instance_dir") or "").strip()
    client_zip = (params.get("client_zip_path") or "").strip()
    srv_src_mode = params.get("server_source_mode", "zip")
    server_zip = (params.get("server_zip_path") or "").strip()
    server_url = (params.get("server_zip_url") or "").strip()

    profile = store.save_updater_profile({
        "update_mode": mode,
        "prism_instance_dir": prism_dir,
        "last_client_zip": client_zip,
        "server_source_mode": srv_src_mode,
        "last_server_zip": server_zip,
        "last_server_url": server_url,
    })

    protected_configs = profile.get("protected_configs", [])
    report = {
        "success": True,
        "mode": mode,
        "errors": [],
        "warnings": [],
        "client": None,
        "server": None,
        "protected_configs": protected_configs,
    }

    local_client_mods_dir = None
    client_zip_info_cache = None

    # --- CLIENT ANALYSIS (works 100% offline, NO API key needed) ---
    if mode in ("both", "client"):
        c_errors = []
        mc_dir = locate_prism_mc_dir(prism_dir)
        if not prism_dir or not os.path.isdir(prism_dir):
            c_errors.append("Не указана или не найдена папка инстанса Prism Launcher.")
        elif not mc_dir:
            c_errors.append("В указанной папке Prism Launcher не найдена подпапка .minecraft/ (или minecraft/).")

        if not client_zip or not os.path.isfile(client_zip):
            c_errors.append("Не указан или не найден локальный клиентский архив (.zip).")

        if c_errors:
            report["errors"].extend(c_errors)
        else:
            try:
                local_client_mods_dir = os.path.join(mc_dir, "mods")
                is_locked, locked_list = check_client_running_locks(mc_dir)
                if is_locked:
                    report["errors"].append(
                        f"Обнаружена активная блокировка клиента ({', '.join(locked_list)}). Закройте Minecraft перед обновлением!"
                    )

                cur_cl_file = ""
                cur_ver = "Не определена"
                for fn in os.listdir(mc_dir):
                    if is_changelog_file(fn):
                        cur_cl_file = fn
                        _, parsed_to = parse_changelog_version(fn)
                        if parsed_to:
                            cur_ver = parsed_to
                        break

                cz_info = inspect_client_zip(client_zip)
                client_zip_info_cache = cz_info
                old_client_mods = scan_local_mods(local_client_mods_dir)
                mod_diff = diff_mod_lists(
                    old_client_mods,
                    cz_info["mods"],
                    profile.get("custom_client_mods", []),
                )

                # Resourcepacks
                rp_dir = os.path.join(mc_dir, "resourcepacks")
                archive_rps_low = {x.lower() for x in cz_info["resourcepacks"]}
                prof_rps_low = {str(x).lower() for x in profile.get("custom_resourcepacks", [])}
                custom_rps = []
                if os.path.isdir(rp_dir):
                    for entry in sorted(os.listdir(rp_dir), key=lambda s: s.lower()):
                        if entry.lower() in archive_rps_low:
                            continue
                        full_rp = os.path.join(rp_dir, entry)
                        is_dir = os.path.isdir(full_rp)
                        sz_str = "Папка" if is_dir else format_size(os.path.getsize(full_rp))
                        in_prof = entry.lower() in prof_rps_low
                        custom_rps.append({
                            "name": entry,
                            "is_dir": is_dir,
                            "size_str": sz_str,
                            "in_profile": in_prof,
                            "checked": in_prof,
                        })

                cfg_dir = os.path.join(mc_dir, "config")
                prot_status = [
                    {"path": rel_c, "exists": os.path.exists(os.path.join(cfg_dir, *rel_c.split("/")))}
                    for rel_c in protected_configs
                ]

                report["client"] = {
                    "instance_dir": prism_dir,
                    "mc_dir": mc_dir,
                    "is_locked": is_locked,
                    "locked_items": locked_list,
                    "current_version": cur_ver,
                    "current_changelog": cur_cl_file,
                    "target_version": cz_info["target_version"],
                    "target_changelog": cz_info["changelog_file"],
                    "changelog_preview": cz_info["changelog_preview"],
                    "has_mmc_pack": cz_info["has_mmc_pack"],
                    "has_patches": cz_info["has_patches"],
                    "has_libraries": cz_info["has_libraries"],
                    "mod_asset_count": cz_info["mod_asset_count"],
                    "archive_rp_count": len(cz_info["resourcepacks"]),
                    "mod_diff": mod_diff,
                    "custom_resourcepacks": custom_rps,
                    "protected_status": prot_status,
                }
                log_fn(
                    f"Скан клиента: {cur_ver} -> {cz_info['target_version']} | Обновляется: {len(mod_diff['updated_mods'])}, новых: {mod_diff['new_added_count']}, кастомных: {len(mod_diff['custom_mods'])}, ресурспаков: {len(custom_rps)}",
                    "UPDATER",
                    "success",
                )
            except Exception as e:
                report["errors"].append(f"Ошибка анализа клиента: {e}")

    # --- SERVER ANALYSIS (Bisquit.Host API) ---
    if mode in ("both", "server"):
        s_errors = []
        if not client.is_configured():
            s_errors.append("API-ключ или ID сервера Bisquit.Host не настроены (откройте раздел «Подключение и Настройки»).")
        if srv_src_mode == "zip":
            if not server_zip or not os.path.isfile(server_zip):
                s_errors.append("Не указан или не найден локальный серверный архив (.zip).")
        else:
            if not server_url or not server_url.lower().startswith(("http://", "https://")):
                s_errors.append("Укажите корректную прямую HTTP/HTTPS ссылку на серверный ZIP-архив.")

        if s_errors:
            report["errors"].extend(s_errors)
        else:
            try:
                root_entries = client.list_dir("/")
                cur_srv_cl = ""
                cur_srv_ver = "Не определена"
                has_live_java9 = False
                for item in root_entries:
                    nm = item.get("name") or ""
                    if nm.lower() == "java9args.txt":
                        has_live_java9 = True
                    if client.is_file_entry(item) and is_changelog_file(nm):
                        cur_srv_cl = nm
                        _, parsed_to = parse_changelog_version(nm)
                        if parsed_to:
                            cur_srv_ver = parsed_to

                old_server_mods = client.list_remote_mods("/mods")
                if not old_server_mods:
                    report["warnings"].append("На сервере в /mods не найдено модов (возможно, сервер пуст или недоступен).")

                sz_info = None
                url_preview_warning = ""
                pull_meta = None
                if srv_src_mode == "zip":
                    with zipfile.ZipFile(server_zip, "r") as zf:
                        sz_info = inspect_server_zip_obj(zf)
                else:
                    try:
                        with HttpRangeFile(client.session, server_url, timeout=15) as rf:
                            with zipfile.ZipFile(rf, "r") as zf:
                                sz_info = inspect_server_zip_obj(zf)
                    except Exception as re_err:
                        pull_meta = client.query_pull_url(server_url)
                        meta_hint = ""
                        if pull_meta and pull_meta.get("file_name"):
                            meta_hint = f" (файл: {pull_meta['file_name']}, {format_size(pull_meta.get('file_size'))})"
                        url_preview_warning = (
                            f"Быстрый предпросмотр ZIP по URL недоступен ({re_err}){meta_hint}. "
                            "Вы можете вручную отметить кастомные серверные моды ниже; архив скачается сервером через /files/pull и проверится до замены."
                        )
                        report["warnings"].append(url_preview_warning)

                if sz_info is not None:
                    srv_diff = diff_mod_lists(
                        old_server_mods,
                        sz_info["mods"],
                        profile.get("custom_server_mods", []),
                    )
                    target_srv_ver = sz_info["target_version"]
                    target_srv_cl = sz_info["changelog_file"]
                    srv_cl_preview = sz_info["changelog_preview"]
                    has_new_java9 = sz_info["has_java9args"]
                    core_jars = sz_info["core_jars"]
                    range_fallback = False
                else:
                    # Improved Range fallback: never hide newly added custom server mods!
                    range_fallback = True
                    prof_srv_low = {str(x).replace("\\", "/").lower() for x in profile.get("custom_server_mods", [])}
                    prof_srv_bases = {extract_mod_base_key(x) for x in profile.get("custom_server_mods", []) if x}
                    client_zip_bases = (
                        {extract_mod_base_key(k) for k in client_zip_info_cache["mods"].keys()}
                        if client_zip_info_cache else set()
                    )

                    fallback_custom = []
                    for old_rel in sorted(old_server_mods.keys(), key=lambda s: s.lower()):
                        bk = extract_mod_base_key(old_rel)
                        in_prof = (old_rel.lower() in prof_srv_low) or (bk in prof_srv_bases)
                        not_in_client_zip = bool(client_zip_bases and bk not in client_zip_bases)
                        if in_prof or not_in_client_zip or not client_zip_bases:
                            fallback_custom.append({
                                "rel_path": old_rel,
                                "name": posixpath.basename(old_rel),
                                "base_key": bk,
                                "size_str": format_size(old_server_mods[old_rel]),
                                "in_profile": in_prof,
                                "is_disabled": is_disabled_mod(old_rel),
                                "checked": in_prof,
                                "conflict_with": "",
                                "unverified": not in_prof,
                            })
                    # Sort profile & likely-custom first
                    fallback_custom.sort(key=lambda m: (not m["in_profile"], m["rel_path"].lower()))

                    srv_diff = {
                        "custom_mods": fallback_custom,
                        "updated_mods": [],
                        "new_added_mods": [],
                        "unchanged_count": 0,
                        "new_added_count": 0,
                        "old_total": len(old_server_mods),
                        "new_total": 0,
                    }
                    target_srv_ver = "Определится после /files/pull"
                    target_srv_cl = (pull_meta or {}).get("file_name") or ""
                    srv_cl_preview = ""
                    has_new_java9 = True
                    core_jars = []

                if not local_client_mods_dir and prism_dir:
                    mc_probe = locate_prism_mc_dir(prism_dir)
                    if mc_probe:
                        local_client_mods_dir = os.path.join(mc_probe, "mods")

                for cm in srv_diff["custom_mods"]:
                    cm["source"] = "server"
                    cm["local_path"] = ""
                    if local_client_mods_dir:
                        cand_local = os.path.join(local_client_mods_dir, *cm["rel_path"].split("/"))
                        if os.path.isfile(cand_local):
                            cm["local_path"] = cand_local

                # Batch stat protected configs on server in a single API call
                stat_map = client.stat_files("/config", protected_configs)
                srv_prot_status = [
                    {"path": rel_c, "exists": stat_map.get(rel_c) is not None}
                    for rel_c in protected_configs
                ]

                report["server"] = {
                    "source_mode": srv_src_mode,
                    "range_fallback": range_fallback,
                    "current_version": cur_srv_ver,
                    "current_changelog": cur_srv_cl,
                    "target_version": target_srv_ver,
                    "target_changelog": target_srv_cl,
                    "changelog_preview": srv_cl_preview,
                    "has_live_java9": has_live_java9,
                    "has_new_java9": has_new_java9,
                    "core_jars": core_jars,
                    "url_preview_warning": url_preview_warning,
                    "mod_diff": srv_diff,
                    "protected_status": srv_prot_status,
                }
                log_fn(
                    f"Скан сервера: {cur_srv_ver} -> {target_srv_ver} | Модов: {len(old_server_mods)}, обновляется: {len(srv_diff['updated_mods'])}, кастомных: {len(srv_diff['custom_mods'])}",
                    "UPDATER",
                    "success",
                )
            except Exception as e:
                report["errors"].append(f"Ошибка анализа сервера: {e}")

    if report["errors"]:
        report["success"] = False
        for err in report["errors"]:
            log_fn(f"Проверка обновления: {err}", "UPDATER", "error")

    return report
