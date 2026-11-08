#!/usr/bin/env python3
"""NAS Screen - Windows agent.
Reports this PC's status to the NAS once per second. While reports arrive, the NAS screen shows
this PC; when the PC is off/asleep the reports stop and the screen goes back to the NAS view.
Needs: Python 3.8+ and `pip install psutil`.   Setup: python nasscreen_agent.py --setup"""
import csv, ctypes, json, os, platform, re, shutil, socket, subprocess, sys, threading, time, urllib.request

import psutil
try:
    import winreg
except ImportError:
    winreg = None

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "agent.json")
LOG_PATH = os.path.join(HERE, "agent.log")
DEFAULT = {"nas_host": "", "port": 18765, "token": "", "interval": 1.0,
           "lhm_url": "http://127.0.0.1:8085/data.json"}   # LibreHardwareMonitor remote web server (optional, for temps/fans)
NOWIN = 0x08000000 if os.name == "nt" else 0
VIRT = re.compile(r"(vEthernet|VMware|VirtualBox|Loopback|Bluetooth|Hyper-V|TAP|Tailscale|ZeroTier|WSL|Npcap|isatap|Teredo)", re.I)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never go through a system proxy for LAN


def load_cfg():
    c = dict(DEFAULT)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    return c


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 500_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def setup():
    c = load_cfg()
    host = input(f"NAS IP address [{c['nas_host']}]: ").strip() or c["nas_host"]
    port = input(f"Port [{c['port']}]: ").strip() or c["port"]
    token = input("Token (leave empty if the NAS has none): ").strip()
    c.update(nas_host=host, port=int(port), token=token)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    try:
        OPENER.open(f"http://{host}:{c['port']}/ping", timeout=4).read()
        print("OK - NAS is reachable.")
    except Exception as e:
        print(f"WARNING: cannot reach NAS ({e}). Make sure the NAS Screen app is running and the port is open.")


# ------------------------------------------------------------------ static info
def reg_str(path, name, root=None):
    if not winreg:
        return None
    try:
        with winreg.OpenKey(root or winreg.HKEY_LOCAL_MACHINE, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def os_name():
    p = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    pn = reg_str(p, "ProductName")
    if pn:
        try:
            if int(reg_str(p, "CurrentBuild") or 0) >= 22000:
                pn = pn.replace("Windows 10", "Windows 11")
        except ValueError:
            pass
        return f"{pn} {reg_str(p, 'DisplayVersion') or ''} (build {reg_str(p, 'CurrentBuild')})".replace("  ", " ")
    return platform.platform()


def cpu_model():
    return (reg_str(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString") or platform.processor() or "CPU").strip()


def gpu_names():
    out = []
    if not winreg:
        return out
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    for i in range(8):
        sub = f"{base}\\{i:04d}"
        n = reg_str(sub, "DriverDesc")
        if n and "basic" not in n.lower():
            mem = reg_str(sub, "HardwareInformation.qwMemorySize")
            out.append((n, int(mem) if isinstance(mem, int) and mem > 0 else None))
    return out


# ------------------------------------------------------------------ PDH (English counter names, locale independent)
class _VAL(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("v", ctypes.c_double)]


class _ITEM(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("val", _VAL)]


def pdh_sample(paths, wait=1.0):
    """{path: [(instance, value)]} - re-opened every call because GPU Engine instances are per-process."""
    if os.name != "nt":
        time.sleep(wait); return {}
    pdh = ctypes.windll.pdh
    q = ctypes.c_void_p(); out = {}
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)):
        time.sleep(wait); return {}
    try:
        cs = {}
        for p in paths:
            h = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(q, p, 0, ctypes.byref(h)) == 0:
                cs[p] = h
        pdh.PdhCollectQueryData(q); time.sleep(wait); pdh.PdhCollectQueryData(q)
        for p, h in cs.items():
            size, cnt = ctypes.c_ulong(0), ctypes.c_ulong(0)
            pdh.PdhGetFormattedCounterArrayW(h, 0x200, ctypes.byref(size), ctypes.byref(cnt), None)
            if not size.value:
                continue
            buf = ctypes.create_string_buffer(size.value)
            if pdh.PdhGetFormattedCounterArrayW(h, 0x200, ctypes.byref(size), ctypes.byref(cnt), buf) == 0:
                arr = (_ITEM * cnt.value).from_buffer(buf)
                out[p] = [(arr[i].name, arr[i].val.v) for i in range(cnt.value) if arr[i].val.CStatus in (0, 1)]
    finally:
        pdh.PdhCloseQuery(q)
    return out


# ------------------------------------------------------------------ LibreHardwareMonitor (optional)
GROUPS = {"Voltages", "Clocks", "Temperatures", "Load", "Fans", "Flow", "Controls", "Powers", "Power",
          "Data", "SmallData", "Throughput", "Levels", "Factors", "Energy", "Noise", "Humidity"}
GPU_RE = re.compile(r"(GeForce|Radeon|RTX|GTX|Quadro|Arc |UHD|Iris|Graphics)", re.I)


def num(v):
    m = re.search(r"-?\d+(?:[.,]\d+)?", v or "")
    return float(m.group().replace(",", ".")) if m else None


def parse_lhm(j):
    res = {"temps": [], "fans": [], "cpu_temp": None, "cpu_watts": None, "gpus": {}}
    first_cpu_t = []

    def walk(n, path, icons):
        text, kids = n.get("Text", ""), n.get("Children") or []
        if kids:
            for k in kids:
                walk(k, path + [text], icons + [n.get("ImageURL", "") or ""])
            return
        gs = [i for i, p in enumerate(path) if p in GROUPS]
        if not gs or len(path) < 3:
            return
        group, hw = path[gs[-1]], path[2]
        icon = (icons[2] if len(icons) > 2 else "").lower()
        kind = "cpu" if "cpu" in icon else "gpu" if (any(x in icon for x in ("nvidia", "ati", "amd", "gpu")) or GPU_RE.search(hw)) else "other"
        raw = n.get("Value", ""); val = num(raw)
        if val is None:
            return
        short = {"cpu": "CPU", "gpu": "GPU"}.get(kind, hw[:14])
        g = res["gpus"].setdefault(hw, {"name": hw, "src": "lhm"}) if kind == "gpu" else None
        if group == "Temperatures":
            res["temps"].append([f"{short} {text}", val])
            if kind == "cpu":
                first_cpu_t.append(val)
                if re.search(r"(Package|Tctl|Tdie|Core Max)", text) and res["cpu_temp"] is None:
                    res["cpu_temp"] = val
            if g is not None and "core" in text.lower() and g.get("temp") is None:
                g["temp"] = val
        elif group == "Fans":
            res["fans"].append([f"{short} {text}", val])
        elif group in ("Powers", "Power"):
            if kind == "cpu" and "Package" in text:
                res["cpu_watts"] = val
            if g is not None and g.get("watts") is None:
                g["watts"] = val
        elif group == "Load" and g is not None and text == "GPU Core":
            g["util"] = val
        elif group in ("Data", "SmallData") and g is not None:
            mult = 1024 ** 3 if "GB" in raw else 1024 ** 2 if "MB" in raw else 1
            if text == "GPU Memory Used": g["vram_used"] = val * mult
            if text == "GPU Memory Total": g["vram_total"] = val * mult

    walk(j, [], [])
    if res["cpu_temp"] is None and first_cpu_t:
        res["cpu_temp"] = max(first_cpu_t)
    res["temps"], res["fans"] = res["temps"][:14], res["fans"][:8]
    res["gpus"] = list(res["gpus"].values())
    return res


# ------------------------------------------------------------------ sampler
class Sampler:
    def __init__(self, cfg):
        self.cfg, self.t = cfg, time.time()
        self.os, self.model, self.names = os_name(), cpu_model(), gpu_names()
        self.net, self.io = psutil.net_io_counters(pernic=True), psutil.disk_io_counters()
        psutil.cpu_percent(percpu=True)
        self.procs_t, self.top_cpu, self.top_mem, self.nproc = 0, [], [], 0
        self.gpu, self.lhm = [], {}
        threading.Thread(target=self.gpu_loop, daemon=True).start()
        if cfg.get("lhm_url"):
            threading.Thread(target=self.lhm_loop, daemon=True).start()

    # -- background collectors
    def gpu_loop(self):
        smi = shutil.which("nvidia-smi")
        if not smi and os.name == "nt":
            p = r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
            smi = p if os.path.exists(p) else None
        while True:
            try:
                self.gpu = self.q_smi(smi) if smi else self.q_pdh()
            except Exception as e:
                log(f"gpu: {e!r}")
                time.sleep(3)
            time.sleep(0.5)

    def q_smi(self, smi):
        o = subprocess.run([smi, "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8, creationflags=NOWIN).stdout
        f = lambda x: None if "N/A" in x or not x.strip() else float(x)
        res = []
        for l in o.splitlines():
            p = [x.strip() for x in l.split(",")]
            if len(p) >= 6:
                res.append({"name": p[0], "util": f(p[1]), "temp": f(p[2]),
                            "vram_used": f(p[3]) and f(p[3]) * 1048576, "vram_total": f(p[4]) and f(p[4]) * 1048576,
                            "watts": f(p[5]), "src": "nvidia-smi"})
        time.sleep(1.5)
        return res

    def q_pdh(self):
        d = pdh_sample([r"\GPU Engine(*)\Utilization Percentage", r"\GPU Adapter Memory(*)\Dedicated Usage"])
        eng, mem = {}, {}
        for inst, v in d.get(r"\GPU Engine(*)\Utilization Percentage", []):
            m = re.search(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+_eng_(\d+)_engtype_3d", inst.lower())
            if m:
                e = eng.setdefault(m.group(1), {})
                e[m.group(2)] = e.get(m.group(2), 0) + v          # sum over processes, per engine
        for inst, v in d.get(r"\GPU Adapter Memory(*)\Dedicated Usage", []):
            m = re.search(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", inst.lower())
            if m:
                mem[m.group(1)] = v
        res = []
        for i, luid in enumerate(sorted(set(eng) | set(mem))):
            name, tot = (self.names[0] if len(set(eng) | set(mem)) == 1 and self.names else (f"GPU {i}", None))
            res.append({"name": name, "util": min(100.0, max(eng.get(luid, {0: 0}).values())),
                        "temp": None, "vram_used": mem.get(luid), "vram_total": tot, "watts": None, "src": "pdh"})
        return res

    def lhm_loop(self):
        while True:
            try:
                with OPENER.open(self.cfg["lhm_url"], timeout=2) as r:
                    self.lhm = parse_lhm(json.load(r))
                time.sleep(2)
            except Exception:
                self.lhm = {}
                time.sleep(5)

    # -- per-report
    def sample_procs(self, now):
        if now - self.procs_t < 3:
            return
        self.procs_t = now; n = psutil.cpu_count() or 1; rows = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                rows.append((p.info["name"] or "?", p.info["pid"], p.cpu_percent(None) / n, p.memory_info().rss))
            except (psutil.Error, OSError):
                pass
        self.nproc = len(rows)
        self.top_cpu = [list(r) for r in sorted(rows, key=lambda x: -x[2])[:6]]
        self.top_mem = [list(r) for r in sorted(rows, key=lambda x: -x[3])[:4]]

    def collect(self):
        now = time.time(); dt = max(now - self.t, 1e-3); self.t = now
        cores = psutil.cpu_percent(percpu=True)
        cpu = {"model": self.model, "threads": psutil.cpu_count(), "physical": psutil.cpu_count(logical=False),
               "pct": sum(cores) / len(cores) if cores else 0.0, "cores": cores,
               "temp": self.lhm.get("cpu_temp"), "watts": self.lhm.get("cpu_watts")}
        try:
            f = psutil.cpu_freq()
            if f: cpu["freq"] = f.current
        except Exception:
            pass
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        # NICs
        cur = psutil.net_io_counters(pernic=True); addrs = psutil.net_if_addrs(); st = psutil.net_if_stats(); nics = []
        for n, c in cur.items():
            s = st.get(n)
            if VIRT.search(n) or not s or not s.isup:
                continue
            ip = next((a.address for a in addrs.get(n, []) if a.family == socket.AF_INET
                       and not a.address.startswith(("127.", "169.254."))), None)
            if ip:
                p = self.net.get(n, c)
                nics.append({"name": n, "ip": ip, "speed": s.speed, "rx": max(0, c.bytes_recv - p.bytes_recv) / dt,
                             "tx": max(0, c.bytes_sent - p.bytes_sent) / dt, "rx_tot": c.bytes_recv, "tx_tot": c.bytes_sent})
        self.net = cur
        # disks
        disks = []
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in p.opts or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append({"mount": p.mountpoint.rstrip("\\") or p.mountpoint, "fs": p.fstype, "used": u.used, "total": u.total})
            except OSError:
                pass
        io, r_io = psutil.disk_io_counters(), {"read": 0, "write": 0}
        if io and self.io:
            r_io = {"read": max(0, io.read_bytes - self.io.read_bytes) / dt, "write": max(0, io.write_bytes - self.io.write_bytes) / dt}
        self.io = io
        self.sample_procs(now)
        # GPUs: nvidia-smi / PDH counters, completed by LibreHardwareMonitor when it is running
        gpus = [dict(g) for g in self.gpu]; lg = self.lhm.get("gpus") or []
        if not gpus:
            gpus = [dict(g) for g in lg]
        elif lg:
            for k in ("temp", "watts", "vram_total"):
                if gpus[0].get(k) is None and lg[0].get(k) is not None:
                    gpus[0][k] = lg[0][k]
        bat = None
        try:
            b = psutil.sensors_battery()
            if b: bat = {"pct": b.percent, "plugged": b.power_plugged}
        except Exception:
            pass
        try:
            user = psutil.users()[0].name
        except Exception:
            user = "-"
        return {"host": socket.gethostname(), "os": self.os, "user": user, "ts": now, "uptime": now - psutil.boot_time(),
                "nproc": self.nproc, "cpu": cpu,
                "mem": {"total": vm.total, "used": vm.total - vm.available, "avail": vm.available,
                        "swap_total": sw.total, "swap_used": sw.used},
                "gpus": gpus, "net": nics, "disks": disks, "io": r_io,
                "sensors": {"temps": self.lhm.get("temps", []), "fans": self.lhm.get("fans", [])},
                "procs_cpu": self.top_cpu, "procs_mem": self.top_mem, "battery": bat}


def main():
    if "--setup" in sys.argv:
        return setup()
    cfg = load_cfg()
    if not cfg["nas_host"]:
        print("Not configured. Run: python nasscreen_agent.py --setup"); log("not configured"); return
    url = f"http://{cfg['nas_host']}:{cfg['port']}/report"
    s = Sampler(cfg); time.sleep(1.2); last = ""
    if "--once" in sys.argv:
        print(json.dumps(s.collect(), indent=1, ensure_ascii=False)); return
    while True:
        t0 = time.time()
        try:
            req = urllib.request.Request(url, json.dumps(s.collect()).encode(),
                                         {"Content-Type": "application/json", "X-Token": cfg.get("token", "")})
            OPENER.open(req, timeout=3).read()
            if last:
                log("reconnected"); last = ""
        except Exception as e:
            if str(e) != last:
                last = str(e); log(f"send failed: {e!r}")
        time.sleep(max(0.05, float(cfg.get("interval", 1.0)) - (time.time() - t0)))


if __name__ == "__main__":
    main()
