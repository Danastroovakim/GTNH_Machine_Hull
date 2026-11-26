import base64
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlparse

from app.constants import ANSI_ESCAPE_RE


class RawWebSocketClient:
    """Built-in RFC-6455 SSL WebSocket client without external dependencies."""

    def __init__(self, ws_url: str, origin: str, on_message, on_state_change):
        self.ws_url = ws_url
        self.origin = origin
        self.on_message = on_message
        self.on_state_change = on_state_change
        self.sock = None
        self.running = False
        self._send_lock = threading.Lock()

    def connect_and_loop(self) -> None:
        parsed = urlparse(self.ws_url)
        use_ssl = parsed.scheme == "wss"
        host = parsed.hostname
        port = parsed.port or (443 if use_ssl else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        raw_sock = socket.create_connection((host, port), timeout=10)
        if use_ssl:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            self.sock = raw_sock

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = f"{host}:{port}" if port not in (80, 443) else host
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: {self.origin}\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("utf-8"))

        header_buf = b""
        while b"\r\n\r\n" not in header_buf:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("WebSocket: соединение закрыто при рукопожатии")
            header_buf += chunk

        first_line = header_buf.split(b"\r\n", 1)[0].decode("utf-8", errors="ignore")
        if "101" not in first_line:
            raise ConnectionError(f"WebSocket отклонил подключение: {first_line}")

        self.sock.settimeout(25.0)
        self.running = True
        self.on_state_change(True)

        try:
            while self.running:
                try:
                    opcode, payload = self._recv_frame()
                except socket.timeout:
                    self._send_frame(b"ping", opcode=9)
                    continue

                if opcode is None or opcode == 8:
                    break
                elif opcode == 9:
                    self._send_frame(payload, opcode=10)
                elif opcode in (1, 2):
                    text = payload.decode("utf-8", errors="replace")
                    self.on_message(text)
        finally:
            self.running = False
            self.on_state_change(False)
            self.close()

    def _recv_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _recv_frame(self) -> tuple[int | None, bytes | None]:
        hdr = self._recv_exact(2)
        if not hdr:
            return None, None
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F

        if length == 126:
            ext = self._recv_exact(2)
            if not ext:
                return None, None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(8)
            if not ext:
                return None, None
            length = struct.unpack("!Q", ext)[0]

        mask_key = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length > 0 else b""
        if payload is None:
            return None, None

        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _send_frame(self, payload_bytes: bytes, opcode: int = 1) -> None:
        with self._send_lock:
            if not self.sock:
                return
            frame = bytearray()
            frame.append(0x80 | (opcode & 0x0F))
            length = len(payload_bytes)
            if length < 126:
                frame.append(0x80 | length)
            elif length < 65536:
                frame.append(0x80 | 126)
                frame.extend(struct.pack("!H", length))
            else:
                frame.append(0x80 | 127)
                frame.extend(struct.pack("!Q", length))

            mask_key = os.urandom(4)
            frame.extend(mask_key)
            masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload_bytes))
            frame.extend(masked_payload)
            self.sock.sendall(frame)

    def send_json(self, obj: dict) -> None:
        if self.running:
            data = json.dumps(obj).encode("utf-8")
            self._send_frame(data, opcode=1)

    def close(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


class ConsoleTelemetryService:
    """Manages WebSocket console stream, log poller fallback, and live server telemetry."""

    def __init__(self, client, emit_logs_fn, push_log_fn):
        self.client = client
        self._emit_logs = emit_logs_fn
        self._push_log = push_log_fn

        self._active = False
        self._ws_client = None
        self._ws_connected = False
        self._log_source_mode = "auto"
        self._last_log_tail = ""
        self._last_log_signature = None
        self._initial_history_loaded = False

        self.live_stats = {
            "state": "offline",
            "ram_bytes": 0,
            "ram_limit_bytes": 8 * (1024 ** 3),
            "cpu_pct": 0.0,
            "disk_bytes": 0,
            "uptime": 0,
        }
        self._last_lag_info = "0ms (0t)"
        self._last_tps_info = "20.0 TPS (0.0ms)"
        self._online_players: set[str] = set()

        threading.Thread(target=self._ws_manager_loop, daemon=True).start()
        threading.Thread(target=self._log_poller_loop, daemon=True).start()

    def start(self) -> None:
        self._active = True
        if self.client.is_configured():
            threading.Thread(target=self.preload_recent_logs, daemon=True).start()

    def reset_connection(self) -> None:
        self._last_log_tail = ""
        self._last_log_signature = None
        self._initial_history_loaded = False
        self._online_players.clear()
        if self._ws_client:
            self._ws_client.close()
        self._ws_connected = False
        if self._active and self.client.is_configured():
            threading.Thread(target=self.preload_recent_logs, daemon=True).start()

    def set_log_source(self, mode: str) -> None:
        self._log_source_mode = mode
        self._last_log_tail = ""
        self._last_log_signature = None
        if mode != "auto" and self._ws_client:
            self._ws_client.close()
        self._push_log(f"Источник консоли переключён на: {mode.upper()}", "SYSTEM", "info")

    def classify_line(self, line: str) -> str:
        low = line.lower()

        if "overall" in low and "mean tick time:" in low and "mean tps:" in low:
            m = re.search(r"mean tick time:\s*([\d\.]+)\s*ms.*mean tps:\s*([\d\.]+)", low)
            if m:
                mspt, tps = float(m.group(1)), float(m.group(2))
                self._last_tps_info = f"{tps:.1f} TPS ({mspt:.1f}ms)"
            return "success"

        join_m = re.search(r"\]:\s*([a-zA-Z0-9_]{2,16})\s+joined the game", line)
        if join_m:
            self._online_players.add(join_m.group(1))
            return "success"

        left_m = re.search(r"\]:\s*([a-zA-Z0-9_]{2,16})\s+left the game", line)
        if left_m:
            self._online_players.discard(left_m.group(1))
            return "warning"

        if "can't keep up!" in low:
            if "running " in low and " behind" in low:
                try:
                    part = line.split("Running ")[1]
                    self._last_lag_info = part.replace(" behind, skipping ", " / ").strip()[:28]
                except Exception:
                    pass
            return "lag"

        if "[server utilities]" in low and ("backup done" in low or "created " in low):
            return "success"

        if "/error]" in low or "exception" in low or "stderr]" in low or "crash report" in low:
            return "error"
        if "/warn]" in low or "warning" in low:
            return "warning"
        if "[server utilities]" in low or "[fml]" in low or "[personalspace]" in low:
            return "mod"
        if "joined the game" in low or "done (" in low:
            return "success"
        return "info"

    def preload_recent_logs(self) -> None:
        if not self.client.is_configured() or self._initial_history_loaded:
            return
        try:
            text = self.client.get_logs(lines=450, timeout=8)
            if text:
                self._initial_history_loaded = True
                self._last_log_tail = text[-40000:]
                batch = []
                for raw_line in text.splitlines():
                    clean = ANSI_ESCAPE_RE.sub("", raw_line).strip()
                    if not clean:
                        continue
                    batch.append({
                        "time": "HIST",
                        "msg": clean,
                        "source": "SERVER",
                        "type": self.classify_line(clean),
                    })
                self._emit_logs(batch)
        except Exception:
            pass

    def _ws_manager_loop(self) -> None:
        while True:
            if self._active and self.client.is_configured() and self._log_source_mode == "auto":
                token, sock_url = self.client.get_ws_credentials()
                if token and sock_url:
                    def on_state(connected: bool):
                        self._ws_connected = connected
                        if connected and self._ws_client:
                            self._ws_client.send_json({"event": "auth", "args": [token]})

                    def on_msg(raw_text: str):
                        try:
                            pkt = json.loads(raw_text)
                            event = pkt.get("event", "")
                            args = pkt.get("args") or []

                            if event == "auth success":
                                if not self._initial_history_loaded:
                                    self._ws_client.send_json({"event": "send logs", "args": [None]})
                                self._ws_client.send_json({"event": "send stats", "args": [None]})

                            elif event in ("console output", "install output", "daemon message"):
                                batch = []
                                for arg in args:
                                    for raw_line in str(arg).splitlines():
                                        clean = ANSI_ESCAPE_RE.sub("", raw_line).strip()
                                        if not clean:
                                            continue
                                        batch.append({
                                            "time": "WS",
                                            "msg": clean,
                                            "source": "SERVER",
                                            "type": self.classify_line(clean),
                                        })
                                self._emit_logs(batch)

                            elif event == "stats" and args:
                                st = json.loads(args[0]) if isinstance(args[0], str) else args[0]
                                if isinstance(st, dict):
                                    self.live_stats["cpu_pct"] = float(st.get("cpu_absolute") or 0.0)
                                    self.live_stats["ram_bytes"] = int(st.get("memory_bytes") or 0)
                                    if st.get("memory_limit_bytes"):
                                        self.live_stats["ram_limit_bytes"] = int(st["memory_limit_bytes"])
                                    self.live_stats["disk_bytes"] = int(st.get("disk_bytes") or 0)
                                    if st.get("uptime") is not None:
                                        self.live_stats["uptime"] = int(st.get("uptime") or 0)
                                    if st.get("state"):
                                        self.live_stats["state"] = str(st["state"]).lower()

                            elif event == "status" and args:
                                self.live_stats["state"] = str(args[0]).lower()

                            elif event == "token expiring":
                                new_tok, _ = self.client.get_ws_credentials()
                                if new_tok and self._ws_client:
                                    self._ws_client.send_json({"event": "auth", "args": [new_tok]})
                        except Exception:
                            pass

                    try:
                        self._ws_client = RawWebSocketClient(
                            sock_url,
                            self.client.panel_url(),
                            on_msg,
                            on_state,
                        )
                        self._ws_client.connect_and_loop()
                    except Exception:
                        self._ws_connected = False
            else:
                if self._ws_client and self._ws_connected:
                    self._ws_client.close()
                self._ws_connected = False

            time.sleep(5)

    def _log_poller_loop(self) -> None:
        while True:
            if self._active and self.client.is_configured() and (self._log_source_mode != "auto" or not self._ws_connected):
                try:
                    text = None
                    if self._log_source_mode in ("auto", "latest"):
                        text = self.client.get_logs(lines=300, timeout=6)

                    if text is None:
                        target_file = "/logs/fml-server-latest.log" if self._log_source_mode == "fml" else "/logs/latest.log"
                        target_name = os.path.basename(target_file)
                        entries = self.client.list_dir("/logs")
                        should_download = False
                        for item in entries:
                            if item.get("name") == target_name:
                                sig = (item.get("size", 0), item.get("modified") or "")
                                if sig != self._last_log_signature:
                                    self._last_log_signature = sig
                                    should_download = True
                                break
                        if should_download:
                            text = self.client.read_file(target_file)

                    if text:
                        tail = text[-45000:] if len(text) > 45000 else text
                        if tail != self._last_log_tail:
                            if not self._last_log_tail:
                                new_lines = tail.splitlines()[-250:]
                            else:
                                anchor = self._last_log_tail[-400:]
                                idx = tail.find(anchor)
                                if idx != -1:
                                    new_lines = tail[idx + len(anchor):].splitlines()
                                else:
                                    new_lines = tail.splitlines()[-60:]
                            self._last_log_tail = tail
                            batch = []
                            for ln in new_lines:
                                clean = ANSI_ESCAPE_RE.sub("", ln).strip()
                                if clean:
                                    batch.append({
                                        "time": "POLL",
                                        "msg": clean,
                                        "source": "SERVER",
                                        "type": self.classify_line(clean),
                                    })
                            self._emit_logs(batch)
                except Exception:
                    pass
            time.sleep(2.5)

    def get_status(self) -> dict:
        if not self.client.is_configured():
            return {
                "configured": False,
                "state": "unconfigured",
                "server_name": "",
                "ram": "0.0 GiB",
                "ram_limit": "—",
                "ram_pct": 0,
                "cpu": "0.0%",
                "cpu_limit": 100,
                "cpu_pct": 0,
                "disk": "0.0 GiB",
                "disk_limit": "—",
                "disk_pct": 0,
                "lag": self._last_lag_info,
                "tps": self._last_tps_info,
                "players": 0,
                "player_list": [],
                "ws": False,
                "uptime": 0,
            }

        # Populate server limits once if disk limit is still unknown
        if self.client.disk_limit_bytes == 0 and not self.client.server_name:
            try:
                self.client.get_server_info()
            except Exception:
                pass

        if not self._ws_connected:
            try:
                res = self.client.get_resources()
                if res:
                    state = res.get("state") or res.get("current_state") or self.live_stats["state"]
                    ram_bytes = res.get("memory_bytes") or 0
                    ram_limit = res.get("memory_limit_bytes") or self.client.ram_limit_bytes
                    disk_bytes = res.get("disk_bytes") or 0
                    cpu_pct = res.get("cpu_absolute") or 0.0
                    uptime = res.get("uptime") or 0
                    self.live_stats.update({
                        "state": str(state).lower(),
                        "ram_bytes": int(ram_bytes),
                        "ram_limit_bytes": int(ram_limit) if ram_limit else self.client.ram_limit_bytes,
                        "disk_bytes": int(disk_bytes),
                        "cpu_pct": float(cpu_pct),
                        "uptime": int(uptime),
                    })
            except Exception:
                pass

        st = self.live_stats["state"]
        if st != "running":
            self._online_players.clear()

        ram_gib = self.live_stats["ram_bytes"] / (1024 ** 3)
        ram_lim_bytes = self.live_stats["ram_limit_bytes"] or self.client.ram_limit_bytes or (8 * 1024 ** 3)
        ram_lim_gib = max(0.5, ram_lim_bytes / (1024 ** 3))
        ram_pct = min(100, int((ram_gib / ram_lim_gib) * 100))

        disk_gib = self.live_stats["disk_bytes"] / (1024 ** 3)
        if self.client.disk_limit_bytes > 0:
            disk_lim_gib = self.client.disk_limit_bytes / (1024 ** 3)
            disk_pct = min(100, int((self.live_stats["disk_bytes"] / self.client.disk_limit_bytes) * 100))
            disk_lim_str = f"{disk_lim_gib:.0f} GiB"
        else:
            disk_pct = 0
            disk_lim_str = "—"

        cpu_lim = max(100.0, float(self.client.cpu_limit or 100.0))
        cpu_pct_norm = min(100, int((self.live_stats["cpu_pct"] / cpu_lim) * 100))

        return {
            "configured": True,
            "state": st,
            "server_name": self.client.server_name or "GTNH Server",
            "ram": f"{ram_gib:.2f} GiB",
            "ram_limit": f"{ram_lim_gib:.0f} GiB",
            "ram_pct": ram_pct,
            "cpu": f"{self.live_stats['cpu_pct']:.1f}%",
            "cpu_limit": int(cpu_lim),
            "cpu_pct": cpu_pct_norm,
            "disk": f"{disk_gib:.2f} GiB",
            "disk_limit": disk_lim_str,
            "disk_pct": disk_pct,
            "lag": self._last_lag_info,
            "tps": self._last_tps_info,
            "players": len(self._online_players),
            "player_list": sorted(self._online_players),
            "ws": self._ws_connected,
            "uptime": self.live_stats.get("uptime", 0),
        }
