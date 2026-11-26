import os
import re
import string

from app.utils import is_changelog_file, parse_changelog_version


def _parse_instance_cfg(cfg_path: str) -> dict:
    meta = {"name": "", "iconKey": "", "java_version": ""}
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(("#", "[")):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k == "name":
                        meta["name"] = v
                    elif k == "iconKey":
                        meta["iconKey"] = v
                    elif k == "JavaVersion":
                        meta["java_version"] = v
    except Exception:
        pass
    return meta


def _parse_prismlauncher_instance_dir(cfg_path: str) -> str | None:
    if not os.path.isfile(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("InstanceDir="):
                    val = s.split("=", 1)[1].strip().strip('"')
                    if not val:
                        return None
                    if not os.path.isabs(val):
                        val = os.path.normpath(os.path.join(os.path.dirname(cfg_path), val))
                    if os.path.isdir(val):
                        return val
    except Exception:
        pass
    return None


def _inspect_instance_folder(inst_dir: str) -> dict | None:
    cfg_path = os.path.join(inst_dir, "instance.cfg")
    if not os.path.isfile(cfg_path):
        return None

    mc_dir = None
    for sub in (".minecraft", "minecraft"):
        cand = os.path.join(inst_dir, sub)
        if os.path.isdir(cand):
            mc_dir = cand
            break
    if not mc_dir:
        return None

    meta = _parse_instance_cfg(cfg_path)
    folder_name = os.path.basename(inst_dir.rstrip("\\/"))
    display_name = meta["name"] or folder_name

    # Detect GTNH version from changelog
    version = ""
    try:
        for fn in os.listdir(mc_dir):
            if is_changelog_file(fn):
                _, to_ver = parse_changelog_version(fn)
                if to_ver:
                    version = to_ver
                    break
    except Exception:
        pass

    # Detect whether this is a GTNH instance
    haystack = f"{display_name} {folder_name} {meta['iconKey']}".lower()
    is_gtnh = bool(re.search(r"gt[\s_\-]*nh|gt[\s_\-]*new[\s_\-]*horizons", haystack))
    mods_count = 0
    mods_dir = os.path.join(mc_dir, "mods")
    if os.path.isdir(mods_dir):
        try:
            for fn in os.listdir(mods_dir):
                low = fn.lower()
                if low.endswith((".jar", ".jar.disabled")):
                    mods_count += 1
                    if "gtnewhorizonscoremod" in low or low.startswith("gregtech-5."):
                        is_gtnh = True
        except Exception:
            pass

    if not version:
        m = re.search(r"(\d+\.\d+\.\d+(?:[-_a-zA-Z0-9]+)?)", display_name)
        if m:
            version = m.group(1)

    return {
        "path": os.path.abspath(inst_dir),
        "name": display_name,
        "folder": folder_name,
        "version": version or "Не определена",
        "is_gtnh": is_gtnh,
        "mods_count": mods_count,
        "java_version": meta["java_version"],
    }


def discover_prism_instances(extra_paths: list[str] | None = None) -> list[dict]:
    """
    Auto-discover Prism Launcher and MultiMC instances across standard AppData paths,
    prismlauncher.cfg custom InstanceDir, and portable drive roots.
    """
    candidate_roots: list[str] = []

    appdata = os.environ.get("APPDATA") or ""
    localappdata = os.environ.get("LOCALAPPDATA") or ""
    userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")

    standard_cfg_files = [
        os.path.join(appdata, "PrismLauncher", "prismlauncher.cfg"),
        os.path.join(appdata, "MultiMC", "multimc.cfg"),
    ]
    standard_inst_dirs = [
        os.path.join(appdata, "PrismLauncher", "instances"),
        os.path.join(localappdata, "Programs", "PrismLauncher", "instances"),
        os.path.join(appdata, "MultiMC", "instances"),
        os.path.join(userprofile, "PrismLauncher", "instances"),
        os.path.join(userprofile, "MultiMC", "instances"),
    ]

    if os.name == "nt":
        for drive in string.ascii_uppercase:
            root = f"{drive}:\\"
            if not os.path.isdir(root):
                continue
            for rel in (
                "PrismLauncher",
                "MultiMC",
                os.path.join("Games", "PrismLauncher"),
                os.path.join("Games", "MultiMC"),
                os.path.join("Minecraft", "PrismLauncher"),
            ):
                base = os.path.join(root, rel)
                if os.path.isdir(base):
                    standard_inst_dirs.append(os.path.join(base, "instances"))
                    standard_cfg_files.append(os.path.join(base, "prismlauncher.cfg"))
                    standard_cfg_files.append(os.path.join(base, "multimc.cfg"))

    for cfg_f in standard_cfg_files:
        custom_dir = _parse_prismlauncher_instance_dir(cfg_f)
        if custom_dir:
            candidate_roots.append(custom_dir)

    candidate_roots.extend(standard_inst_dirs)

    found: dict[str, dict] = {}

    # Check explicitly passed instance folders first
    for p in (extra_paths or []):
        if p and os.path.isdir(p):
            info = _inspect_instance_folder(p)
            if info:
                found[os.path.normcase(info["path"])] = info

    seen_roots = set()
    for root_dir in candidate_roots:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        norm_root = os.path.normcase(os.path.abspath(root_dir))
        if norm_root in seen_roots:
            continue
        seen_roots.add(norm_root)
        try:
            for entry in os.listdir(root_dir):
                full = os.path.join(root_dir, entry)
                if not os.path.isdir(full):
                    continue
                info = _inspect_instance_folder(full)
                if info:
                    found[os.path.normcase(info["path"])] = info
        except Exception:
            pass

    results = list(found.values())
    results.sort(key=lambda x: (not x["is_gtnh"], -x["mods_count"], x["name"].lower()))
    return results
