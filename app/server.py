#!/usr/bin/env python3
"""
arch-wifi-printer-webui — CUPS printer manager for isolated Wi-Fi networks.

Problem this solves: on Mikrotik networks with client-isolation, printers are
often reachable ONLY via IPv6 link-local (fe80::), because IPv4 unicast between
clients is blocked. CUPS cannot natively use scoped link-local addresses, so we
bridge `127.0.0.1:<port>` -> printer IPv6 link-local through a TCP proxy, then
point CUPS at localhost.

This app: management web UI + the IPv6 proxy + CUPS glue, in one process.

Zero dependencies: pure Python stdlib (http.server + sockets + subprocess).
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = os.environ.get("PWEBUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("PWEBUI_PORT", "8642"))
CUPS_QUEUE = os.environ.get("PWEBUI_CUPS_QUEUE", "Brother-DCP-T720DW")

# Default proxy map: <label> -> (target IPv6 host, target port)
# Override with PWEBUI_PROXY_JSON=json string if you want multiple printers.
DEFAULT_PROXIES = [
    {"name": "ipp", "listen_port": 3631, "target_v6": "fe80::920f:0cff:fe9c:aaf3", "target_port": 631},
    {"name": "raw", "listen_port": 9100, "target_v6": "fe80::920f:0cff:fe9c:aaf3", "target_port": 9100},
    {"name": "web", "listen_port": 8080, "target_v6": "fe80::920f:0cff:fe9c:aaf3", "target_port": 80},
]
IFNAME = os.environ.get("PWEBUI_IF", "wlan0")
PROXY_WEB_PORT = 8080  # where the printer web admin is reachable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=10):
    """Run a command, return (exit_code, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def have(bin):
    return shutil.which(bin) is not None


def is_number(s):
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


def stdin_yes(prompt, default="no"):
    """Ask on stdin (for manual runs). Non-interactive -> default."""
    try:
        if sys.stdin.isatty():
            ans = input(f"{prompt} [y/N] ").strip().lower()
            if ans in ("y", "yes"):
                return True
    except (EOFError, KeyboardInterrupt, OSError):
        pass
    return default == "yes"


# ---------------------------------------------------------------------------
# IPv6 link-local TCP proxy (bypasses IPv4 client-isolation)
# ---------------------------------------------------------------------------
class PrinterProxy:
    def __init__(self, name, listen_port, target_v6, target_port, ifname):
        self.name = name
        self.listen_port = listen_port
        self.target_v6 = target_v6
        self.target_port = target_port
        self.ifname = ifname
        self._srv = None
        self._threads = []
        self._running = threading.Event()

    def _ifindex(self):
        try:
            return socket.if_nametoindex(self.ifname)
        except (OSError, AttributeError):
            for line in open("/proc/net/if_inet6"):
                p = line.split()
                if len(p) >= 6 and p[5] == self.ifname:
                    return int(p[1], 16)
        raise RuntimeError(f"no ifindex for {self.ifname}")

    def _pipe(self, a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except Exception:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass

    def _handle(self, client):
        try:
            idx = self._ifindex()
        except RuntimeError as e:
            print(f"[{self.name}] proxy error: {e}", flush=True)
            client.close()
            return
        up = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        up.settimeout(10)
        try:
            up.connect((self.target_v6, self.target_port, 0, idx))
        except Exception as e:
            print(f"[{self.name}] upstream {self.target_v6}:{self.target_port} fail: {e}", flush=True)
            client.close()
            return
        up.settimeout(None)
        client.settimeout(None)
        t1 = threading.Thread(target=self._pipe, args=(client, up), daemon=True)
        t2 = threading.Thread(target=self._pipe, args=(up, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.listen_port))
        self._srv.listen(16)
        self._running.set()
        print(f"[{self.name}] proxy 127.0.0.1:{self.listen_port} "
              f"-> [{self.target_v6}%{self.ifname}]:{self.target_port}", flush=True)
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def _accept_loop(self):
        try:
            while True:
                c, _ = self._srv.accept()
                threading.Thread(target=self._handle, args=(c,), daemon=True).start()
        except OSError:
            pass

    def stop(self):
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass


def load_proxies():
    raw = os.environ.get("PWEBUI_PROXY_JSON")
    if raw:
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError:
            cfg = DEFAULT_PROXIES
    else:
        cfg = DEFAULT_PROXIES
    for entry in cfg:
        entry.setdefault("ifname", IFNAME)
    return [PrinterProxy(**e) for e in cfg]


# ---------------------------------------------------------------------------
# CUPS status collection
# ---------------------------------------------------------------------------
def get_printers():
    """Parse `lpstat -p` -> list of printer dicts."""
    rc, out, err = run(["lpstat", "-p"])
    printers = []
    current = None
    for line in out.splitlines():
        m = re.match(r"printer (\S+) is (idle|disabled).*", line.strip())
        if m:
            if current:
                printers.append(current)
            current = {"name": m.group(1), "state": m.group(2), "jobs": []}
            continue
        if current is not None:
            m2 = re.match(r"enabled since (.*)", line.strip())
            if m2:
                current["enabled_since"] = m2.group(1).strip()
    if current:
        printers.append(current)

    # fill job counts from -o
    rc2, out2, _ = run(["lpstat", "-o"])
    for p in printers:
        p["jobs"] = [l for l in out2.splitlines() if l.startswith(p["name"] + "-")]

    # default printer
    rc3, out3, _ = run(["lpstat", "-d"])
    default = None
    m = re.search(r"system default destination: (\S+)", out3)
    if m:
        default = m.group(1)
    for p in printers:
        p["is_default"] = p["name"] == default
    return printers


def get_queue():
    """Parse `lpstat -o` -> list of queued job dicts."""
    rc, out, _ = run(["lpstat", "-o"])
    jobs = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) >= 5:
            jobs.append({
                "id": parts[0],
                "user": parts[1],
                "size": parts[2],
                "time": " ".join(parts[3:5]),
                "rest": parts[5] if len(parts) > 5 else "",
            })
    return jobs


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (above app/)
UI_DIR = os.path.join(PROJECT_ROOT, "ui")


def json_resp(handler, payload, code=200):
    body = json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def serve_static(handler, relpath, send_default=[]):
    allowed = ["index.html"]
    safe = os.path.normpath(relpath)
    if safe in ("/", ""):
        safe = "index.html"
    if safe.startswith(("..", "/")):
        safe = "index.html"
    full = os.path.join(UI_DIR, safe)
    if not os.path.isfile(full):
        full = os.path.join(UI_DIR, "index.html")
    ctype = {
        ".html": "text/html", ".js": "application/javascript",
        ".css": "text/css", ".svg": "image/svg+xml"
    }.get(os.path.splitext(full)[1], "application/octet-stream")
    with open(full, "rb") as f:
        data = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    proxies = None
    cups_queue = CUPS_QUEUE

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[http] {self.address_string()} {fmt % args}\n")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            json_resp(self, {"ok": True, "queue": self.cups_queue})

        elif path == "/api/printers":
            json_resp(self, {"printers": get_printers()})

        elif path == "/api/queue":
            json_resp(self, {"jobs": get_queue()})

        elif path == "/api/status":
            # check prerequisites
            status = {
                "cups": have("lpstat"),
                "default_queue": self.cups_queue,
                "proxies": [
                    {"name": p.name,
                     "listen_port": p.listen_port,
                     "running": p._running.is_set()}
                    for p in (self.proxies or [])
                ],
            }
            json_resp(self, status)

        elif path == "/api/admin-open":
            # returns the URL (browser opens it client-side) for the printer web admin
            json_resp(self, {"url": f"http://127.0.0.1:{PROXY_WEB_PORT}/"})

        elif path.startswith("/api/") or path in ("/", "/index.html"):
            serve_static(self, path.lstrip("/"))

        else:
            json_resp(self, {"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        if path == "/api/print":
            # Accept either raw text body or multipart upload
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" in ctype:
                try:
                    data = urllib.parse.parse_qs(body.decode("utf-8"))
                except Exception:
                    data = {}
                text = data.get("content", [""])[0]
                filename = data.get("filename", ["upload.txt"])[0]
            else:
                text = body.decode("utf-8", "replace")
                filename = "upload.txt"
            tmp = os.path.join("/tmp", f"pwebui_{os.getpid()}_{int(__import__('time').time())}.txt")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            rc, out, err = run(["lp", "-d", self.cups_queue, "-o", "media=A4", tmp])
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if rc == 0:
                json_resp(self, {"ok": True, "message": out.strip()})
            else:
                json_resp(self, {"ok": False, "error": err or out}, 500)

        elif path == "/api/printer/action":
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = {}
            action = data.get("action")
            name = data.get("name", self.cups_queue)
            if action == "enable":
                rc, out, err = run(["sudo", "-n", "cupsenable", name])
            elif action == "disable":
                rc, out, err = run(["sudo", "-n", "cupsdisable", name])
            elif action == "set-default":
                rc, out, err = run(["sudo", "-n", "lpoptions", "-d", name])
            else:
                json_resp(self, {"ok": False, "error": f"unknown action {action}"}, 400)
                return
            ok = rc == 0
            json_resp(self, {"ok": ok, "error": (err or out).strip() if not ok else None})

        elif path == "/api/print-quick":
            # convenience: print the text everyone passes
            text = body.decode("utf-8", "replace")
            tmp = os.path.join("/tmp", f"pwebui_{os.getpid()}.txt")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            rc, out, err = run(["lp", "-d", self.cups_queue, tmp])
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if rc == 0:
                json_resp(self, {"ok": True, "message": out.strip()})
            else:
                json_resp(self, {"ok": False, "error": err.strip() or out.strip()}, 500)

        elif path == "/api/admin-open":
            json_resp(self, {"ok": True, "url": f"http://127.0.0.1:{PROXY_WEB_PORT}/"})

        else:
            json_resp(self, {"error": "not found"}, 404)

    # silence favicon
    def do_HEAD(self):
        self.send_response(204)
        self.end_headers()


def main():
    proxies = load_proxies()
    Handler.proxies = proxies
    for p in proxies:
        p.start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"\n[dashboard] http://127.0.0.1:{PORT}  (CUPS queue = {CUPS_QUEUE})", flush=True)
    print("[dashboard] Ctrl-C to stop. Printer web admin -> http://127.0.0.1:%d/\n" % PROXY_WEB_PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopping...")
    finally:
        for p in proxies:
            p.stop()
        srv.server_close()


if __name__ == "__main__":
    main()