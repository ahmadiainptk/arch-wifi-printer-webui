# arch-wifi-printer-webui

Web UI manajemen printer untuk jaringan Wi-Fi dengan **client isolation** (Mikrotik).
Melihat queue CUPS, quick print, dan buka web admin printer — tanpa akses router.

## Masalah yang dipecahkan

Di jaringan kantor dengan Mikrotik client-isolation, printer sering HANYA bisa
dijangkau lewat **IPv6 link-local** (`fe80::…`), karena unicast IPv4 antar client
diblokir. CUPS tidak bisa langsung pakai alamat link-local (butuh scope-id),
jadi app ini menjembatani:

```
Browser / CUPS  →  127.0.0.1:3631 (IPP)   →  [fe80::…%wlan0]:631 (printer)
                 →  127.0.0.1:9100 (raw)   →  [fe80::…%wlan0]:9100
                 →  127.0.0.1:8080 (web)   →  [fe80::…%wlan0]:80
```

IPv6 link-local tidak di-routing, jadi tidak kena blokir unicast IPv4 dari
isolation. Proxy TCP berjalan di localhost, CUPS tinggal diarahkan ke
`ipp://127.0.0.1:3631/ipp/print`.

## Fitur

- 📊 Dashboard printer CUPS (state, jobs, default)
- 📋 Lihat print queue live
- 🖨 Quick print (teks langsung ke printer)
- 🌐 Buka web admin printer (status tinta, maintenance)
- 🔌 IPv6 link-local proxy built-in (bypass isolation)
- 🚀 Zero dependency — pure Python stdlib + vanilla JS

## Requirement

- Linux (Arch tested) + `cups` (dan `cups-filters`/`libcupsfilters` untuk driverless)
- Printer yang support **IPP Everywhere / Mopria** (mayoritas Brother/EPSON/HP modern)
- Interface Wi-Fi dengan IPv6 link-local ke printer

## Setup printer sekali (manual)

Printer harus didaftarkan ke CUPS lewat proxy IPP:

```bash
# Jalankan server dulu (proxy aktif di :3631)
python3 app/server.py

# Di terminal lain:
lpadmin -p Brother-DCP-T720DW -E -v "ipp://127.0.0.1:3631/ipp/print" -m everywhere
lpoptions -d Brother-DCP-T720DW
```

Ganti `Brother-DCP-T720DW` sesuai nama printer lo.

## Install sebagai systemd service

```bash
sudo ./scripts/install.sh --setup-cups
```

Lalu buka **http://127.0.0.1:8642**.

Untuk aksi enable/disable printer dari web UI (butuh sudo tanpa password):

```bash
sudo visudo -f /etc/sudoers.d/pwebui
# tambahkan:
# <user> ALL=(ALL) NOPASSWD: /usr/bin/cupsenable, /usr/bin/cupsdisable, /usr/bin/lpoptions
```

## Konfigurasi (env)

| Var | Default | Fungsi |
|-----|---------|--------|
| `PWEBUI_HOST` | `0.0.0.0` | Bind address dashboard |
| `PWEBUI_PORT` | `8642` | Port dashboard |
| `PWEBUI_CUPS_QUEUE` | `Brother-DCP-T720DW` | Queue default untuk print |
| `PWEBUI_IF` | `wlan0` | Interface Wi-Fi ke printer |
| `PWEBUI_PROXY_JSON` | *(default map)* | JSON array proxy custom |

Contoh multi-printer:

```bash
PWEBUI_PROXY_JSON='[{"name":"brother","listen":3631,"target_v6":"fe80::920f:0cff:fe9c:aaf3","target_port":631}]' \
  python3 app/server.py
```

## Cara kerja (teknis)

1. `PrinterProxy` bind `127.0.0.1:<port>` dan forward ke
   `[target_v6%ifname]:<port>` — pakai `socket.if_nametoindex()` untuk scope-id.
2. `/api/printers`, `/api/queue`, `/api/status` parse output `lpstat` — no root.
3. `/api/print` kirim teks via `lp -d <queue>`.
4. `/api/printer/action` panggil `cupsenable/cupsdisable/lpoptions` (via sudoers NOPASSWD).
5. Frontend vanilla JS, dark theme, auto-refresh status tiap 5 detik.

## Struktur

```
app/server.py          # backend: HTTP + proxy IPv6 + CUPS glue
ui/index.html          # frontend single-page
scripts/install.sh     # install systemd + setup CUPS
systemd/arch-wifi-printer-webui.service   # unit file
docs/                  # dokumentasi tambahan
```

## Troubleshooting

**Queue error "Printer does not exist / No IPP attributes"**
Pastikan proxy IPP jalan (`ss -tlnp | grep 3631`), dan CUPS queue pakai
`ipp://127.0.0.1:3631/ipp/print`.

**Can't find printer via IPv4**
Normal — printer APIPA `169.254.x.x` karena DHCP gagal / isolation. Pakai IPv6
link-local; cek dengan `avahi-browse -rt _ipp._tcp`.

**Proxy "Network is unreachable"**
Interface salah — set `PWEBUI_IF=wlan0` sesuai nama interface lo
(`ip link` untuk cek).

## License

MIT — lihat [LICENSE](LICENSE).
