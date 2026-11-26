import os
import posixpath
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.constants import MOD_FILE_EXTENSIONS
from app.utils import format_size


class BisquitClient:
    """Calagopus / Pterodactyl (Bisquit.Host) HTTP API client."""

    def __init__(self, config_store, log_fn=None):
        self.store = config_store
        self._log = log_fn or (lambda msg, src="API", typ="info": print(f"[{src}] {msg}", flush=True))
        self.session = self._create_session()

        self._world_folder_name = None
        self._su_backup_folder = None
        self._paths_detected_at = 0.0

        # Cached server limits from GET /
        self.server_name = ""
        self.panel_backup_limit = None
        self.cpu_limit = 100.0
        self.ram_limit_bytes = 8 * (1024 ** 3)
        self.disk_limit_bytes = 0  # 0 means unknown until GET /

    @staticmethod
    def _create_session() -> requests.Session:
        s = requests.Session()
        retry = Retry(total=1, backoff_factor=0.2, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=24, pool_maxsize=24)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def invalidate_caches(self) -> None:
        self._world_folder_name = None
        self._su_backup_folder = None
        self._paths_detected_at = 0.0
        self.server_name = ""
        self.panel_backup_limit = None
        self.disk_limit_bytes = 0

    def is_configured(self) -> bool:
        return self.store.is_server_configured()

    def headers(self) -> dict:
        cfg = self.store.get_config()
        return {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def base_url(self) -> str:
        cfg = self.store.get_config()
        return f"{cfg['panel_url']}/api/client/servers/{cfg['server_id']}"

    def panel_url(self) -> str:
        return self.store.get_config()["panel_url"]

    @staticmethod
    def unwrap_item(item) -> dict:
        if not isinstance(item, dict):
            return {}
        attr = item.get("attributes")
        if isinstance(attr, dict):
            merged = item.copy()
            merged.update(attr)
            return merged
        return item

    @classmethod
    def unwrap_list(cls, payload) -> list[dict]:
        if isinstance(payload, list):
            return [cls.unwrap_item(x) for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        if isinstance(payload.get("data"), list):
            return [cls.unwrap_item(x) for x in payload["data"] if isinstance(x, dict)]
        for key in ("backups", "entries", "files", "directory_entries", "items", "assets", "servers"):
            sub = payload.get(key)
            if isinstance(sub, list):
                return [cls.unwrap_item(x) for x in sub if isinstance(x, dict)]
            if isinstance(sub, dict) and isinstance(sub.get("data"), list):
                return [cls.unwrap_item(x) for x in sub["data"] if isinstance(x, dict)]
        for val in payload.values():
            if isinstance(val, dict) and isinstance(val.get("data"), list):
                return [cls.unwrap_item(x) for x in val["data"] if isinstance(x, dict)]
        return []

    @staticmethod
    def is_file_entry(entry: dict) -> bool:
        if "directory" in entry and isinstance(entry["directory"], bool):
            return not entry["directory"]
        if "is_directory" in entry:
            return not bool(entry["is_directory"])
        if "file" in entry and isinstance(entry["file"], bool):
            return entry["file"]
        if "is_file" in entry:
            return bool(entry["is_file"])
        mime = str(entry.get("mime") or entry.get("mimetype") or "").lower()
        if "directory" in mime or "folder" in mime:
            return False
        return True

    # --- Server info, power, commands, logs ---
    def test_connection(self, panel_url: str = "", api_key: str = "", server_id: str = "") -> dict:
        """Read-only connection check against GET /api/client/servers/{id}."""
        cfg = self.store.get_config()
        p_url = (panel_url or cfg["panel_url"] or "https://mgr.bisquit.host").strip().rstrip("/")
        if "/server/" in p_url:
            p_url = p_url.split("/server/")[0]
        key = (api_key or cfg["api_key"] or "").strip()
        sid = (server_id or cfg["server_id"] or "").strip()

        if not key or not sid:
            return {"success": False, "error": "Укажите Client API Key и ID сервера."}

        try:
            r = self.session.get(
                f"{p_url}/api/client/servers/{sid}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if r.status_code == 401:
                return {"success": False, "error": "HTTP 401: Неверный или истёкший Client API Key."}
            if r.status_code == 403:
                return {"success": False, "error": "HTTP 403: Нет прав доступа к указанному серверу."}
            if r.status_code == 404:
                return {"success": False, "error": "HTTP 404: Сервер с таким ID не найден."}
            if r.status_code != 200:
                return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:140]}"}

            raw = r.json()
            s_obj = self.unwrap_item(raw.get("server", raw))
            limits = s_obj.get("limits") or {}
            feat = s_obj.get("feature_limits") or {}
            return {
                "success": True,
                "name": s_obj.get("name") or "GTNH Server",
                "cpu_limit": limits.get("cpu"),
                "memory_mb": limits.get("memory"),
                "disk_mb": limits.get("disk"),
                "backup_slots": feat.get("backups"),
                "node": s_obj.get("node_name") or "",
            }
        except requests.RequestException as e:
            return {"success": False, "error": f"Сетевая ошибка: {e}"}

    def get_server_info(self) -> dict | None:
        if not self.is_configured():
            return None
        r = self.session.get(self.base_url(), headers=self.headers(), timeout=10)
        if r.status_code != 200:
            return None
        raw = r.json()
        s_obj = self.unwrap_item(raw.get("server", raw))
        self.server_name = s_obj.get("name") or self.server_name
        limits = s_obj.get("limits") or {}
        if limits.get("memory"):
            self.ram_limit_bytes = int(limits["memory"]) * 1024 * 1024
        if limits.get("disk"):
            self.disk_limit_bytes = int(limits["disk"]) * 1024 * 1024
        if limits.get("cpu"):
            self.cpu_limit = float(limits["cpu"])
        feat = s_obj.get("feature_limits") or {}
        if "backups" in feat:
            self.panel_backup_limit = feat.get("backups")
        return s_obj

    def get_resources(self) -> dict | None:
        if not self.is_configured():
            return None
        r = self.session.get(f"{self.base_url()}/resources", headers=self.headers(), timeout=6)
        if r.status_code != 200:
            return None
        raw = r.json()
        return self.unwrap_item(raw.get("resources", raw.get("stats", raw)))

    def get_ws_credentials(self) -> tuple[str | None, str | None]:
        if not self.is_configured():
            return None, None
        try:
            r = self.session.get(f"{self.base_url()}/websocket", headers=self.headers(), timeout=6)
            if r.status_code == 200:
                j = r.json()
                data = j.get("data") if isinstance(j.get("data"), dict) else j
                token = data.get("token")
                sock_url = data.get("url") or data.get("socket")
                if token and sock_url:
                    return token, sock_url
        except Exception:
            pass
        return None, None

    def get_logs(self, lines: int = 300, timeout: int = 8) -> str | None:
        if not self.is_configured():
            return None
        r = self.session.get(
            f"{self.base_url()}/logs",
            headers=self.headers(),
            params={"lines": lines},
            timeout=timeout,
        )
        return r.text if r.status_code == 200 else None

    def send_power(self, signal: str) -> None:
        r = self.session.post(
            f"{self.base_url()}/power",
            headers=self.headers(),
            json={"action": signal},
            timeout=10,
        )
        r.raise_for_status()

    def send_command(self, command: str) -> requests.Response:
        return self.session.post(
            f"{self.base_url()}/command",
            headers=self.headers(),
            json={"command": command},
            timeout=10,
        )

    # --- File operations ---
    def read_file(self, filepath: str) -> str | None:
        if not self.is_configured():
            return None
        try:
            r = self.session.get(
                f"{self.base_url()}/files/contents",
                headers=self.headers(),
                params={"file": filepath},
                timeout=12,
            )
            return r.text if r.status_code == 200 else None
        except Exception:
            return None

    def write_file(self, filepath: str, content_str: str) -> bool:
        hdrs = self.headers()
        hdrs["Content-Type"] = "text/plain; charset=utf-8"
        r = self.session.post(
            f"{self.base_url()}/files/write",
            headers=hdrs,
            params={"file": filepath},
            data=content_str.encode("utf-8"),
            timeout=15,
        )
        r.raise_for_status()
        return True

    def list_dir(self, directory: str = "/") -> list[dict]:
        """Paginated directory listing across all pages (Calagopus per_page=100)."""
        if not self.is_configured():
            return []
        all_items = []
        page = 1
        per_page = 100
        try:
            while page <= 50:
                r = self.session.get(
                    f"{self.base_url()}/files/list",
                    headers=self.headers(),
                    params={
                        "directory": directory,
                        "page": page,
                        "per_page": per_page,
                        "sort": "name_asc",
                    },
                    timeout=12,
                )
                if r.status_code != 200:
                    break
                payload = r.json()
                chunk = self.unwrap_list(payload)
                if not chunk:
                    break
                all_items.extend(chunk)

                entries_obj = payload.get("entries") if isinstance(payload, dict) else None
                total = int(entries_obj.get("total") or 0) if isinstance(entries_obj, dict) else 0
                actual_pp = int(entries_obj.get("per_page") or per_page) if isinstance(entries_obj, dict) else per_page
                if (total and len(all_items) >= total) or (not total and len(chunk) < actual_pp):
                    break
                page += 1
        except Exception as e:
            print(f"[WARN] Ошибка листинга {directory}: {e}", flush=True)
        return all_items

    def stat_files(self, root_dir: str, files_list: list[str]) -> dict[str, dict | None]:
        """Batch file existence/metadata check via POST /files/stat."""
        result = {f: None for f in files_list}
        if not files_list or not self.is_configured():
            return result
        try:
            r = self.session.post(
                f"{self.base_url()}/files/stat",
                headers=self.headers(),
                json={"root": root_dir, "files": files_list},
                timeout=12,
            )
            if r.status_code == 200:
                entries = self.unwrap_list(r.json())
                by_name = {e.get("name"): e for e in entries if e.get("name")}
                for f in files_list:
                    bn = posixpath.basename(f.strip("/"))
                    if f in by_name:
                        result[f] = by_name[f]
                    elif bn in by_name:
                        result[f] = by_name[bn]
                return result
        except Exception:
            pass

        # Fallback via parent listing
        for f in files_list:
            full = f"{root_dir.rstrip('/')}/{f.lstrip('/')}" if root_dir != "/" else f"/{f.lstrip('/')}"
            result[f] = self.remote_entry_info(full, use_stat=False)
        return result

    def remote_entry_info(self, remote_path: str, use_stat: bool = True) -> dict | None:
        norm = "/" + str(remote_path or "").replace("\\", "/").strip("/")
        if norm == "/":
            return {"name": "/", "directory": True, "file": False}
        parent = posixpath.dirname(norm) or "/"
        name = posixpath.basename(norm)
        if use_stat:
            try:
                r = self.session.post(
                    f"{self.base_url()}/files/stat",
                    headers=self.headers(),
                    json={"root": parent, "files": [name]},
                    timeout=10,
                )
                if r.status_code == 200:
                    entries = self.unwrap_list(r.json())
                    for e in entries:
                        if e.get("name") == name:
                            return e
                    return None
            except Exception:
                pass
        for item in self.list_dir(parent):
            if item.get("name") == name:
                return item
        return None

    def create_folder(self, root_dir: str, folder_name: str) -> bool:
        try:
            r = self.session.post(
                f"{self.base_url()}/files/create-directory",
                headers=self.headers(),
                json={"root": root_dir, "name": folder_name},
                timeout=12,
            )
            return r.status_code in (200, 201, 204)
        except Exception:
            return False

    def ensure_dir_path(self, dir_path: str) -> None:
        clean = str(dir_path or "/").replace("\\", "/").strip("/")
        if not clean:
            return
        current = "/"
        for part in [p for p in clean.split("/") if p]:
            self.create_folder(current, part)
            current = f"{current.rstrip('/')}/{part}" if current != "/" else f"/{part}"

    def delete_files(self, root_dir: str, files_list: list[str], permanent: bool = True, raise_on_error: bool = True) -> bool:
        if not files_list:
            return True
        for i in range(0, len(files_list), 100):
            batch = files_list[i:i + 100]
            r = self.session.post(
                f"{self.base_url()}/files/delete",
                headers=self.headers(),
                json={"root": root_dir, "files": batch, "permanent": bool(permanent)},
                timeout=60,
            )
            if raise_on_error:
                r.raise_for_status()
        return True

    def rename_files(self, root_dir: str, moves: list[dict]) -> bool:
        if not moves:
            return True
        for i in range(0, len(moves), 100):
            batch = moves[i:i + 100]
            r = self.session.put(
                f"{self.base_url()}/files/rename",
                headers=self.headers(),
                json={"root": root_dir, "files": batch},
                timeout=60,
            )
            r.raise_for_status()
        return True

    def wait_if_async_operation(self, response, label: str = "Операция", timeout_sec: int = 600, on_progress=None) -> None:
        if response.status_code != 202:
            return
        try:
            op_id = response.json().get("identifier")
        except Exception:
            op_id = None
        if not op_id:
            return

        self._log(f"{label}: выполняется на сервере (ID {op_id[:8]}...), ожидание...", "UPDATER", "info")
        start_ts = time.time()
        deadline = start_ts + timeout_sec

        while time.time() < deadline:
            time.sleep(2.5)
            try:
                r = self.session.get(f"{self.base_url()}/files/operations", headers=self.headers(), timeout=10)
                if r.status_code != 200:
                    continue
                ops = r.json().get("operations") or []
                matched_op = None
                for op in ops:
                    if isinstance(op, str) and op == op_id:
                        matched_op = {"id": op}
                        break
                    if isinstance(op, dict):
                        oid = op.get("identifier") or op.get("uuid") or op.get("id")
                        if oid == op_id:
                            matched_op = op
                            break
                if matched_op is None:
                    if on_progress:
                        on_progress(100, f"{label}: завершено")
                    return

                if on_progress:
                    prog = matched_op.get("progress")
                    total = matched_op.get("total")
                    if isinstance(prog, (int, float)) and isinstance(total, (int, float)) and total > 0:
                        pct = min(99, int((prog / total) * 100))
                        on_progress(pct, f"{label}: {pct}%")
                    else:
                        elapsed = int(time.time() - start_ts)
                        on_progress(None, f"{label}: ожидание ({elapsed}с)...")
            except Exception:
                pass
        raise TimeoutError(f"Превышено время ожидания фоновой операции: {label} ({op_id})")

    def compress_files(
        self,
        root_dir: str,
        files_list: list[str],
        name: str,
        fmt: str = "tar_gz",
        foreground: bool = True,
        timeout: int = 600,
        on_progress=None,
    ) -> str:
        r = self.session.post(
            f"{self.base_url()}/files/compress",
            headers=self.headers(),
            json={
                "name": name,
                "format": fmt,
                "root": root_dir,
                "files": files_list,
                "foreground": foreground,
            },
            timeout=(20, timeout),
        )
        r.raise_for_status()
        self.wait_if_async_operation(r, f"Сжатие {name}", timeout_sec=timeout, on_progress=on_progress)
        try:
            body = r.json()
            entry = self.unwrap_item(body.get("entry", body))
            return entry.get("name") or name
        except Exception:
            return name

    def decompress_file(
        self,
        root_dir: str,
        filename: str,
        foreground: bool = True,
        timeout: int = 600,
        on_progress=None,
    ) -> None:
        r = self.session.post(
            f"{self.base_url()}/files/decompress",
            headers=self.headers(),
            json={
                "root": root_dir,
                "file": filename,
                "foreground": foreground,
            },
            timeout=(20, timeout),
        )
        r.raise_for_status()
        self.wait_if_async_operation(r, f"Распаковка {filename}", timeout_sec=timeout, on_progress=on_progress)

    def upload_file(
        self,
        target_dir: str,
        local_filepath: str,
        remote_filename: str | None = None,
        timeout: int = 900,
        on_progress=None,
    ) -> bool:
        """
        Streaming multipart upload with fixed Content-Length (works reliably for 500MB+ archives on Nginx/Calagopus).
        """
        fname = remote_filename or os.path.basename(local_filepath)
        r_url = self.session.get(
            f"{self.base_url()}/files/upload",
            headers=self.headers(),
            timeout=15,
        )
        r_url.raise_for_status()
        upload_url = self.unwrap_item(r_url.json()).get("url")
        if not upload_url:
            raise RuntimeError("Панель Bisquit.Host не вернула URL загрузки (/files/upload)")

        file_size = os.path.getsize(local_filepath)
        show_log_progress = file_size >= 10 * 1024 * 1024
        log_fn = self._log

        class _MultipartFileStream:
            def __init__(self, path: str, name: str):
                self._boundary = f"----GTNHUpload{int(time.time() * 1000)}{os.getpid()}"
                safe_name = str(name).replace("\\", "_").replace('"', "_")
                self._header = (
                    f"--{self._boundary}\r\n"
                    f'Content-Disposition: form-data; name="files"; filename="{safe_name}"\r\n'
                    f"Content-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8")
                self._footer = f"\r\n--{self._boundary}--\r\n".encode("utf-8")
                self._file = open(path, "rb")
                self._total_len = len(self._header) + file_size + len(self._footer)
                self._hdr_pos = 0
                self._ftr_pos = 0
                self._file_done = False
                self._sent = 0
                self._last_log_pct = 0
                self._last_cb_ts = 0.0

            @property
            def content_type(self) -> str:
                return f"multipart/form-data; boundary={self._boundary}"

            def __len__(self) -> int:
                return self._total_len

            def read(self, size: int = -1) -> bytes:
                if size is None or size < 0:
                    size = 262144
                out = bytearray()

                if self._hdr_pos < len(self._header):
                    take = self._header[self._hdr_pos:self._hdr_pos + size]
                    self._hdr_pos += len(take)
                    out.extend(take)
                    size -= len(take)

                if size > 0 and not self._file_done:
                    chunk = self._file.read(size)
                    if chunk:
                        out.extend(chunk)
                        size -= len(chunk)
                    else:
                        self._file_done = True

                if size > 0 and self._file_done and self._ftr_pos < len(self._footer):
                    take = self._footer[self._ftr_pos:self._ftr_pos + size]
                    self._ftr_pos += len(take)
                    out.extend(take)

                if out:
                    self._sent += len(out)
                    if self._total_len > 0:
                        pct = min(100, int((self._sent * 100) / self._total_len))
                        now = time.monotonic()
                        if on_progress and (now - self._last_cb_ts >= 0.25 or pct >= 100):
                            self._last_cb_ts = now
                            on_progress(pct, f"Выгрузка {fname}: {pct}% ({format_size(self._sent)} / {format_size(self._total_len)})")
                        if show_log_progress and (pct >= self._last_log_pct + 20 or (pct >= 100 and self._last_log_pct < 100)):
                            self._last_log_pct = pct
                            log_fn(
                                f"  Выгрузка {fname}: {pct}% ({format_size(self._sent)} / {format_size(self._total_len)})",
                                "SERVER-UPD",
                                "info",
                            )
                return bytes(out)

            def close(self) -> None:
                try:
                    self._file.close()
                except Exception:
                    pass

        stream = _MultipartFileStream(local_filepath, fname)
        try:
            r_up = self.session.post(
                upload_url,
                params={"directory": target_dir},
                headers={
                    "Accept": "application/json",
                    "Content-Type": stream.content_type,
                    "Content-Length": str(len(stream)),
                },
                data=stream,
                timeout=(60, timeout),
            )
            r_up.raise_for_status()
        finally:
            stream.close()
        return True

    def query_pull_url(self, archive_url: str) -> dict | None:
        """Query remote file metadata (file_name, file_size) via Calagopus POST /files/pull/query."""
        if not self.is_configured():
            return None
        try:
            r = self.session.post(
                f"{self.base_url()}/files/pull/query",
                headers=self.headers(),
                json={"url": archive_url},
                timeout=15,
            )
            if r.status_code == 200:
                return r.json().get("query_result")
        except Exception:
            pass
        return None

    def pull_file(self, target_dir: str, archive_url: str, name: str | None = None, timeout: int = 600, on_progress=None) -> bool:
        body = {
            "url": archive_url,
            "root": target_dir,
            "foreground": True,
        }
        if name:
            body["name"] = name
        r = self.session.post(
            f"{self.base_url()}/files/pull",
            headers=self.headers(),
            json=body,
            timeout=(20, timeout),
        )
        r.raise_for_status()
        self.wait_if_async_operation(
            r,
            "Скачивание архива сервером (/files/pull)",
            timeout_sec=timeout,
            on_progress=on_progress,
        )
        return True

    def list_remote_mods(self, mods_root: str = "/mods") -> dict[str, int]:
        """Scan /mods and /mods/1.7.10 on server strictly for mod archive files (ignoring resource subdirs)."""
        mods: dict[str, int] = {}
        top_entries = self.list_dir(mods_root)
        for item in top_entries:
            name = item.get("name") or ""
            if not name:
                continue
            if self.is_file_entry(item):
                if name.lower().endswith(MOD_FILE_EXTENSIONS):
                    mods[name] = int(item.get("size") or 0)
            elif name == "1.7.10":
                sub_entries = self.list_dir(f"{mods_root.rstrip('/')}/1.7.10")
                for sub in sub_entries:
                    sname = sub.get("name") or ""
                    if sname and self.is_file_entry(sub) and sname.lower().endswith(MOD_FILE_EXTENSIONS):
                        mods[f"1.7.10/{sname}"] = int(sub.get("size") or 0)
        return mods

    def detect_world_and_backup_paths(self, force: bool = False) -> tuple[str, str]:
        if not self.is_configured():
            return "World", "/backups"

        now = time.time()
        if not force and self._world_folder_name and self._su_backup_folder and (now - self._paths_detected_at < 300):
            return self._world_folder_name, self._su_backup_folder

        if not self._world_folder_name or force:
            prop_txt = self.read_file("/server.properties")
            if prop_txt:
                for line in prop_txt.splitlines():
                    if line.strip().startswith("level-name="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            self._world_folder_name = val
                            break

            if not self._world_folder_name:
                root_items = self.list_dir("/")
                names = {it.get("name", "") for it in root_items if not self.is_file_entry(it)}
                self._world_folder_name = "world" if ("world" in names and "World" not in names) else "World"

        if not self._su_backup_folder or force:
            self._su_backup_folder = "/backups"
            su_txt = self.read_file("/serverutilities/serverutilities.cfg")
            if su_txt:
                m = re.search(r"S:backup_folder_path\s*=\s*(.+)", su_txt)
                if m:
                    raw_path = m.group(1).strip()
                    clean_path = "/" + raw_path.lstrip("./").strip("/")
                    if clean_path and clean_path != "/":
                        self._su_backup_folder = clean_path

        self._paths_detected_at = now
        return self._world_folder_name, self._su_backup_folder

    def merge_directory_tree(self, src_dir: str, dst_dir: str) -> None:
        """Surgical Overlay Merge of remote directory src_dir into dst_dir."""
        src_items = self.list_dir(src_dir)
        if not src_items:
            return

        dst_items = self.list_dir(dst_dir)
        dst_files = {i.get("name") for i in dst_items if self.is_file_entry(i) and i.get("name")}
        dst_dirs = {i.get("name") for i in dst_items if not self.is_file_entry(i) and i.get("name")}

        files_to_overwrite = []
        file_moves = []
        src_rel = src_dir.lstrip("/")
        dst_rel = dst_dir.lstrip("/")

        for item in src_items:
            name = item.get("name")
            if not name or name == "session.lock":
                continue

            if self.is_file_entry(item):
                if name in dst_files:
                    files_to_overwrite.append(name)
                from_p = f"{src_rel}/{name}" if src_rel else name
                to_p = f"{dst_rel}/{name}" if dst_rel else name
                file_moves.append({"from": from_p, "to": to_p})
            else:
                if name not in dst_dirs:
                    from_p = f"{src_rel}/{name}" if src_rel else name
                    to_p = f"{dst_rel}/{name}" if dst_rel else name
                    self.rename_files("/", [{"from": from_p, "to": to_p}])
                else:
                    self.merge_directory_tree(
                        f"{src_dir.rstrip('/')}/{name}",
                        f"{dst_dir.rstrip('/')}/{name}",
                    )

        if files_to_overwrite:
            self.delete_files(dst_dir, files_to_overwrite, permanent=True)
        if file_moves:
            self.rename_files("/", file_moves)

    def get_download_url(self, root_dir: str, filename: str) -> str | None:
        r = self.session.get(
            f"{self.base_url()}/files/download",
            headers=self.headers(),
            params={
                "root": root_dir,
                "files": [filename],
                "directory": "false",
                "archive_format": "tar_gz",
            },
            timeout=10,
        )
        r.raise_for_status()
        return self.unwrap_item(r.json()).get("url")
