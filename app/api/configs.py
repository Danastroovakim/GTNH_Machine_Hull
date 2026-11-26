import re


class ConfigStudioService:
    """Reads and writes serverutilities.cfg, server.properties, and java9args.txt on Bisquit.Host."""

    def __init__(self, client, log_fn):
        self.client = client
        self._log = log_fn

    def get_all_configs(self) -> dict:
        res = {
            "configured": self.client.is_configured(),
            "su": {"found": False},
            "props": {"found": False},
            "jvm": {"found": False, "content": ""},
        }
        if not self.client.is_configured():
            return res

        su_txt = self.client.read_file("/serverutilities/serverutilities.cfg")
        if su_txt:
            def _ex(pat: str, default: str) -> str:
                m = re.search(pat, su_txt)
                return m.group(1).strip() if m else default

            res["su"] = {
                "found": True,
                "enable_backups": _ex(r"B:enable_backups\s*=\s*(true|false)", "true").lower() == "true",
                "backup_timer": _ex(r"S:backup_timer\s*=\s*([^\r\n]+)", "1h"),
                "backups_to_keep": _ex(r"I:backups_to_keep\s*=\s*(\d+)", "3"),
                "only_claimed": _ex(r"B:only_backup_claimed_chunks\s*=\s*(true|false)", "false").lower() == "true",
                "entire_regions": _ex(r"B:backup_entire_regions_with_claims\s*=\s*(true|false)", "true").lower() == "true",
                "need_online_players": _ex(r"B:need_online_players\s*=\s*(true|false)", "true").lower() == "true",
                "chunk_loading": _ex(r"B:chunk_loading\s*=\s*(true|false)", "true").lower() == "true",
            }

        prop_txt = self.client.read_file("/server.properties")
        if prop_txt:
            def _exp(key: str, default: str) -> str:
                m = re.search(rf"^{re.escape(key)}=(.*)$", prop_txt, flags=re.MULTILINE)
                return m.group(1).strip() if m else default

            res["props"] = {
                "found": True,
                "view_distance": _exp("view-distance", "8"),
                "pause_empty": _exp("pause-when-empty-seconds", "0"),
                "max_players": _exp("max-players", "20"),
                "difficulty": _exp("difficulty", "3"),
                "whitelist": _exp("white-list", "true").lower() == "true",
                "pvp": _exp("pvp", "true").lower() == "true",
            }

        jvm_txt = self.client.read_file("/java9args.txt")
        if jvm_txt is not None:
            res["jvm"] = {"found": True, "content": jvm_txt}

        return res

    def save_all_configs(self, payload: dict) -> dict:
        if not self.client.is_configured():
            return {"success": False, "error": "Сервер не подключён."}
        try:
            if "su" in payload:
                su = payload["su"]
                txt = self.client.read_file("/serverutilities/serverutilities.cfg")
                if txt:
                    repl = {
                        r"(B:enable_backups\s*=\s*)(true|false)": str(bool(su.get("enable_backups"))).lower(),
                        r"(S:backup_timer\s*=\s*)([^\r\n]+)": str(su.get("backup_timer", "1h")).strip(),
                        r"(I:backups_to_keep\s*=\s*)(\d+)": str(int(su.get("backups_to_keep", 3))),
                        r"(B:only_backup_claimed_chunks\s*=\s*)(true|false)": str(bool(su.get("only_claimed"))).lower(),
                        r"(B:backup_entire_regions_with_claims\s*=\s*)(true|false)": str(bool(su.get("entire_regions"))).lower(),
                        r"(B:need_online_players\s*=\s*)(true|false)": str(bool(su.get("need_online_players"))).lower(),
                        r"(B:chunk_loading\s*=\s*)(true|false)": str(bool(su.get("chunk_loading"))).lower(),
                    }
                    for pat, val in repl.items():
                        txt = re.sub(pat, lambda m, v=val: f"{m.group(1)}{v}", txt)
                    self.client.write_file("/serverutilities/serverutilities.cfg", txt)

            if "props" in payload:
                pr = payload["props"]
                ptxt = self.client.read_file("/server.properties")
                if ptxt:
                    prepl = {
                        "view-distance": str(int(pr.get("view_distance", 8))),
                        "pause-when-empty-seconds": str(int(pr.get("pause_empty", 0))),
                        "max-players": str(int(pr.get("max_players", 20))),
                        "difficulty": str(int(pr.get("difficulty", 3))),
                        "white-list": str(bool(pr.get("whitelist"))).lower(),
                        "pvp": str(bool(pr.get("pvp"))).lower(),
                    }
                    for k, v in prepl.items():
                        ptxt = re.sub(rf"^({re.escape(k)}=).*$", rf"\g<1>{v}", ptxt, flags=re.MULTILINE)
                    self.client.write_file("/server.properties", ptxt)

            if "jvm_content" in payload and str(payload["jvm_content"]).strip():
                self.client.write_file("/java9args.txt", str(payload["jvm_content"]))

            self._log("Конфигурационные файлы сервера успешно сохранены!", "CONFIG", "success")
            return {"success": True}
        except Exception as e:
            self._log(f"Ошибка сохранения конфигов: {e}", "CONFIG", "error")
            return {"success": False, "error": str(e)}
