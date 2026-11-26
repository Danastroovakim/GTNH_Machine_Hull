import copy
import json
import os
import threading

from app.constants import DEFAULT_CONFIG, DEFAULT_UPDATER_PROFILE
from app.paths import CONFIG_FILE, DATA_DIR, IS_PORTABLE_MODE, UPDATER_PROFILE_FILE
from app.utils import normalize_config_relpath


def _atomic_write_json(filepath: str, data: dict) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, filepath)


class ConfigStore:
    """Thread-safe persistence for Bisquit.Host connection settings and Pack Updater profile."""

    def __init__(self, config_file: str = CONFIG_FILE, profile_file: str = UPDATER_PROFILE_FILE):
        self._lock = threading.RLock()
        self.config_file = config_file
        self.profile_file = profile_file
        self.config = self._load_config()
        self.updater_profile = self._load_updater_profile()

    def _load_config(self) -> dict:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if os.path.isfile(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                if isinstance(saved, dict):
                    for k in DEFAULT_CONFIG:
                        if k in saved and saved[k] is not None:
                            cfg[k] = saved[k]
            except Exception as e:
                print(f"[WARN] Ошибка чтения {self.config_file}: {e}", flush=True)
        if "panel.bisquit.host" in str(cfg.get("panel_url", "")):
            cfg["panel_url"] = "https://mgr.bisquit.host"
        return cfg

    def _load_updater_profile(self) -> dict:
        prof = copy.deepcopy(DEFAULT_UPDATER_PROFILE)
        if os.path.isfile(self.profile_file):
            try:
                with open(self.profile_file, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                if isinstance(saved, dict):
                    for k in DEFAULT_UPDATER_PROFILE:
                        if k in saved and saved[k] is not None:
                            prof[k] = saved[k]
            except Exception as e:
                print(f"[WARN] Ошибка чтения {self.profile_file}: {e}", flush=True)
        return prof

    def is_server_configured(self) -> bool:
        with self._lock:
            return bool(
                str(self.config.get("api_key") or "").strip()
                and str(self.config.get("server_id") or "").strip()
            )

    def get_config(self) -> dict:
        with self._lock:
            out = copy.deepcopy(self.config)
            out["is_configured"] = self.is_server_configured()
            out["data_dir"] = DATA_DIR
            out["is_portable"] = IS_PORTABLE_MODE
            return out

    def save_config(self, panel_url: str, api_key: str, server_id: str, max_backups) -> dict:
        clean_url = str(panel_url or "").strip().rstrip("/")
        if "/server/" in clean_url:
            clean_url = clean_url.split("/server/")[0]

        try:
            max_b = max(1, int(max_backups or 3))
        except (ValueError, TypeError):
            max_b = 3

        with self._lock:
            self.config = {
                "panel_url": clean_url or "https://mgr.bisquit.host",
                "api_key": str(api_key or "").strip(),
                "server_id": str(server_id or "").strip(),
                "max_panel_backups": max_b,
            }
            _atomic_write_json(self.config_file, self.config)
            return self.get_config()

    def get_updater_profile(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.updater_profile)

    def save_updater_profile(self, partial_updates: dict) -> dict:
        with self._lock:
            if isinstance(partial_updates, dict):
                for k, v in partial_updates.items():
                    if k not in DEFAULT_UPDATER_PROFILE:
                        continue
                    if k == "protected_configs" and isinstance(v, list):
                        cleaned = []
                        for item in v:
                            norm = normalize_config_relpath(item)
                            if norm and norm not in cleaned:
                                cleaned.append(norm)
                        self.updater_profile[k] = cleaned
                    else:
                        self.updater_profile[k] = v
                _atomic_write_json(self.profile_file, self.updater_profile)
            return copy.deepcopy(self.updater_profile)
