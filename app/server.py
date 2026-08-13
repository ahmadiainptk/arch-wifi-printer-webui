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
    def __init__(self, name, listen_port, target_v6, target_port, ifname, target_v4=None):
        self.name = name
        self.listen_port = listen_port
        self.target_v6 = target_v6
        self.target_port = target_port
        self.ifname = ifname
        self.target_v4 = target_v4  # if set, use IPv4 upstream instead of IPv6
        self._srv = None
        self._threads = []
        self._running = threading.Event()

    def _upstream(self):
        """Return a connected upstream socket (IPv6 or IPv4)."""
        if self.target_v4:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((self.target_v4, self.target_port))
            return s
        idx = self._ifindex()
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((self.target_v6, self.target_port, 0, idx))
        return s

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
        up = None
        try:
            up = self._upstream()
        except Exception as e:
            print(f"[{self.name}] upstream fail: {e}", flush=True)
            try:
                client.close()
            except Exception:
                pass
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
# Network printer discovery (mDNS / avahi + direct probe)
# ---------------------------------------------------------------------------
def discover_printers(timeout=5):
    """
    Scan the LAN for printers via mDNS (avahi-browse) + NDP IPv6 neighbor scan.
    Returns discovered {name, hostname, ipv4, ipv6, ports, reachable}.

    Why both:
      - mDNS gives brand/hostname/IPv4 but often wrong (APIPA 169.254) and
        frequently does NOT advertise the IPv6 link-local the printer actually
        answers on.
      - NDP (ping ff02::1) populates the kernel neighbor table with every
        host's real IPv6 link-local + MAC. On isolated Wi-Fi (Mikrotik) the
        printer's fe80:: address is the ONLY reachable path.
    We correlate by MAC where possible.
    """
    recs = {}  # hostname -> {name, ipv6, ipv4, types, mac}

    def browse(rt):
        rc, out, _ = run(["avahi-browse", "-rt", rt], timeout=timeout)
        return out

    # --- mDNS pass (parallel) ---
    import concurrent.futures as cf
    type_outs = {}
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(browse, rt): rt for rt in ("_ipp._tcp", "_printer._tcp", "_pdl-datastream._tcp")}
        for f in cf.as_completed(futs):
            rt = futs[f]
            try:
                type_outs[rt] = f.result()
            except Exception:
                type_outs[rt] = ""

    for rt, out in type_outs.items():
        host = None
        cur_name = None
        for line in out.splitlines():
            line = line.rstrip()
            # block header resets current host/name
            if line.startswith("="):
                nm = re.search(r"=\s+\S+\s+(?:IPv4|IPv6)\s+(.*?)\s+(?:Internet Printer|PDL Printer|UNIX Printer)\s+local", line)
                cur_name = nm.group(1).strip() if nm else None
                host = None
            h = re.search(r"hostname\s*=\s*\[([^\]]+)\]", line)
            if h:
                host = h.group(1)
                if host:
                    recs.setdefault(host, {"name": None, "ipv6": None, "ipv4": None, "types": set(), "mac": None, "node": None})
            a = re.search(r"address\s*=\s*\[([^\]]+)\]", line)
            if a and host:
                rec = recs[host]
                ip = a.group(1)
                rec["types"].add(rt)
                if cur_name and rec["name"] is None:
                    rec["name"] = cur_name
                if ":" in ip:
                    if rec["ipv6"] is None and ip.startswith("fe80"):
                        rec["ipv6"] = ip
                elif rec["ipv4"] is None:
                    rec["ipv4"] = ip

    # --- derive IPv6 link-local (EUI-64) from a full MAC, if we can get one ---
    def eui64_linklocal(mac):
        """fe80:: prefix + EUI-64 (insert fffe, flip U/L bit). mac = aa:bb:cc:dd:ee:ff"""
        try:
            b = [int(x, 16) for x in mac.split(":")]
        except Exception:
            return None
        if len(b) != 6:
            return None
        b[0] ^= 0x02  # flip universal/local bit
        import socket as _s
        raw = bytes([b[0], b[1], b[2], 0xff, 0xfe, b[3], b[4], b[5]])
        ret = _s.inet_ntop(_s.AF_INET6, b"\xfe\x80\x00\x00" + b"\x00" * 4 + raw)
        return ret

    # convert hostname -> MAC (Brother: BRW + 12hex)
    def hostname_mac(hostname):
        base = hostname.lower().replace(".local", "")
        if base.startswith("brw"):
            h = base[3:]
            if len(h) == 12 and all(c in "0123456789abcdef" for c in h):
                return ":".join(h[i:i+2] for i in range(0, 12, 2))
        return None

    for host, rec in recs.items():
        mac = hostname_mac(host) or rec.get("mac")
        if mac:
            rec["mac"] = mac
            ll = eui64_linklocal(mac)
            if ll and rec["ipv6"] is None:
                rec["ipv6"] = ll  # always reachable on isolated Wi-Fi

    # --- optionally enrich EPSON node MAC from NDP if available ---
    # Ping multicast group with enough packets + wait for slow printers.
    run(["ping", "-6", "-c", "5", "-W", "2", "-I", IFNAME, "ff02::1"], timeout=timeout)
    import time as _time
    _time.sleep(1.5)  # let slow printers answer
    v6_hosts = []
    for _ in range(3):
        rc, out, _ = run(["ip", "-6", "neigh", "show", "dev", IFNAME])
        v6_hosts = []
        for line in out.splitlines():
            # format: fe80::x lladdr 1a:2b:.. state [router]
            f = line.split()
            if len(f) >= 3 and f[1] == "lladdr":
                v6_hosts.append((f[0], f[2].lower()))
        if v6_hosts:
            break
        _time.sleep(0.8)
    node_mac = {}
    for fe80, m in v6_hosts:
        node_mac.setdefault(m.replace(":", "")[-6:], (m, fe80))
    # known printer OUI prefixes (IEEE registrations)
    PRINTER_OUI = ("90:0f:0c", "38:1a:52", "e0:bb:9e", "24:1f:5b", "9c:3e:53")
    oui_node = {}  # node -> (mac, fe80) restricted to printer OUIs
    for fe80, m in v6_hosts:
        if m.startswith(PRINTER_OUI):
            oui_node.setdefault(m.replace(":", "")[-6:], (m, fe80))
    for host, rec in recs.items():
        base = host.lower().replace(".local", "")
        if base.startswith("epson"):
            node = base[5:]
            if len(node) == 6:
                # prefer printer-OUI match, fallback to any node match
                cand = oui_node.get(node) or node_mac.get(node)
                if cand:
                    full, fe80 = cand
                    if rec["mac"] is None:
                        rec["mac"] = full
                    if rec["ipv6"] is None:
                        ll = eui64_linklocal(full) or fe80
                        rec["ipv6"] = ll

    results = []
    for host, rec in recs.items():
        entry = {
            "name": rec["name"] or host.split(".")[0],
            "hostname": host,
            "ipv4": rec["ipv4"],
            "ipv6": rec["ipv6"],
            "mac": rec["mac"],
            "types": sorted(rec["types"]),
            "ports": [],
            "reachable": False,
        }
        reach_v6 = bool(rec["ipv6"]) and _probe_v6(rec["ipv6"], IFNAME, 631)
        reach_v4 = bool(rec["ipv4"]) and _probe_v4(rec["ipv4"], 631)
        entry["reachable"] = bool(reach_v6 or reach_v4)
        if reach_v6:
            entry["ports"] = _ports_v6(rec["ipv6"], IFNAME, [631, 9100, 515, 80, 443])
            entry["via"] = "ipv6"
        elif reach_v4:
            entry["ports"] = _ports_v4(rec["ipv4"], [631, 9100, 515, 80, 443])
            entry["via"] = "ipv4"
        results.append(entry)
    return results


def _probe_v6(ip, ifname, port):
    try:
        idx = socket.if_nametoindex(ifname)
    except (OSError, AttributeError):
        return False
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((ip, port, 0, idx))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _probe_v4(ip, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _ports_v6(ip, ifname, ports):
    try:
        idx = socket.if_nametoindex(ifname)
    except (OSError, AttributeError):
        return []
    open_ports = []
    for p in ports:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(1.2)
        try:
            s.connect((ip, p, 0, idx))
            open_ports.append(p)
        except Exception:
            pass
        finally:
            s.close()
    return open_ports


def _ports_v4(ip, ports):
    open_ports = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.2)
        try:
            s.connect((ip, p))
            open_ports.append(p)
        except Exception:
            pass
        finally:
            s.close()
    return open_ports


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
    _proxy_next_port = [4000]  # dynamic proxy spawn (scan-add)

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

        elif path == "/api/scan":
            # LAN printer scan: mDNS + port probe. Takes a few seconds.
            try:
                found = discover_printers()
                json_resp(self, {"printers": found})
            except Exception as e:
                json_resp(self, {"error": str(e)}, 500)

        elif path == "/api/scan/avahi-check":
            # whether avahi-browse exists
            json_resp(self, {"avahi": have("avahi-browse")})

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

        elif path == "/api/scan/add":
            # Register a discovered printer into CUPS:
            #   {hostname, ipv6?, ipv4?, name?}  -> spawn dynamic proxy -> lpadmin -m everywhere
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = {}
            host = data.get("hostname", "")
            v6 = data.get("ipv6") or data.get("ipv6_addr")
            v4 = data.get("ipv4")
            if not host:
                json_resp(self, {"ok": False, "error": "hostname required"}, 400)
                return
            # pick target: prefer IPv6 (bypasses isolation)
            if v6:
                target_v6, target_port = v6, 631
            elif v4:
                target_v6, target_port = None, 631
            else:
                json_resp(self, {"ok": False, "error": "no address to connect"}, 400)
                return
            # allocate a listen port for the new IPP proxy
            port = Handler._proxy_next_port[0]
            Handler._proxy_next_port[0] += 1
            name_slug = host.split(".")[0].replace("_", "-") or "printer"
            qname = data.get("name") or name_slug
            # sanitize: lpadmin queue names = [a-z0-9_-], no spaces/uppercase
            qname = re.sub(r"[^a-z0-9_-]", "-", qname.lower()).strip("-") or "printer"
            p = None
            if v6:
                p = PrinterProxy(f"scan-{name_slug}", port, v6, 631, IFNAME)
            elif v4:
                p = PrinterProxy(f"scan-{name_slug}", port, v4, 631, IFNAME, target_v4=v4)
            if p is None:
                json_resp(self, {"ok": False, "error": "no address to connect"}, 400)
                return
            try:
                p.start()
            except Exception as e:
                json_resp(self, {"ok": False, "error": f"proxy: {e}"}, 500)
                return
            prox = Handler.proxies or []
            prox.append(p)
            Handler.proxies = prox
            # register into CUPS via IPP
            # NOTE: use hostname "localhost" (resolves to 127.0.0.1 but the
            # printer sees a matching Host header). Some printers (e.g. EPSON)
            # reject the request if device-uri Host header is 127.0.0.1.
            # EPSON PPD polling is slow — give lpadmin a generous timeout.
            rc, out, err = run([
                "lpadmin", "-p", qname, "-E",
                "-v", f"ipp://localhost:{port}/ipp/print",
                "-m", "everywhere",
            ], timeout=30)
            if rc != 0:
                # roll back the proxy if registration failed
                p.stop()
                json_resp(self, {"ok": False, "error": err or out, "proxy_port": port}, 500)
                return
            run(["lpoptions", "-d", qname])
            json_resp(self, {"ok": True, "queue": qname, "proxy_port": port,
                             "via": "ipv6" if target_v6 else "ipv4"})

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