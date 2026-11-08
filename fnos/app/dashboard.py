#!/usr/bin/env python3
"""NAS overview on the local console (default /dev/tty1). stdlib only.
Debug over SSH:  python3 dashboard.py --once      (prints one frame to stdout)
Config: $NASSCREEN_CONFIG (json, auto-created, reloaded every 30s)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import traceback, collections, ctypes, glob, json, os, platform, re, shutil, signal, socket, struct, subprocess, sys, threading, time

CONF_PATH = os.environ.get("NASSCREEN_CONFIG", "/tmp/nasscreen.json")
DEFAULTS = {
    "tty": "/dev/tty1",
    "smart_interval_sec": 60,
    "font": "auto",             # auto | off | /path/to/font.psf.gz (applied only at start)
    "graph_height": 4,          # rows per curve (auto-reduced on small screens)
    "graph_step_sec": 3,        # seconds per graph column (width 70 -> ~3.5 min window)
    "graph": "block",           # block (uses full/half block glyphs) | ascii
    "view": "auto",             # auto (Windows agent online -> Windows view) | nas | windows
    "agent_port": 18765,        # Windows agent reports here (needs restart to change)
    "agent_token": "",          # optional shared secret (agent.json "token" must match)
    "agent_allow_ip": "",       # optional: only accept reports from this IP
    "agent_timeout_sec": 8,     # no report for this long = Windows is off -> back to NAS view
}
RST = "\033[0m"
ANSI = re.compile(r"(\033\[[0-9;]*m)")
SPARK = "_.:-=+*#%@"


# ---------------------------------------------------------------- helpers
def col(code, s): return f"\033[{code}m{s}{RST}"
def rd(p, d=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return d
def lvl(v, a, b): return 32 if v < a else 33 if v < b else 31
def level(p): return lvl(p, 70, 90)
def dim(s): return col(90, s)


def bar(p, w):
    p = max(0.0, min(100.0, p)); n = int(round(p * w / 100))
    return "[" + col(level(p), "#" * n) + dim("-" * (w - n)) + "]"


def human(n):
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def fmt_up(sec):
    d, r = divmod(int(sec), 86400); h, r = divmod(r, 3600)
    return (f"{d}d " if d else "") + f"{h}h {r // 60}m"


def vlen(s): return len(ANSI.sub("", s))


def fit(s, w):
    out, n = [], 0
    for tok in ANSI.split(s):
        if not tok:
            continue
        if tok.startswith("\033["):
            out.append(tok); continue
        room = w - n
        if room > 0:
            out.append(tok[:room]); n += min(len(tok), room)
    return "".join(out) + RST + " " * (w - n)


def title(name, W):
    return col("1;36", f"-- {name} ") + dim("-" * max(0, W - 4 - len(name)))


def wrap(prefix, items, W):
    lines, cur = [], prefix
    for it in items:
        if vlen(cur) + 2 + vlen(it) > W and vlen(cur) > len(prefix):
            lines.append(cur); cur = " " * len(prefix)
        cur += ("  " if vlen(cur) > len(prefix) else "") + it
    lines.append(cur)
    return lines


# ---------------------------------------------------------------- config
def load_conf():
    c = dict(DEFAULTS)
    try:
        with open(CONF_PATH) as f:
            c.update(json.load(f))
    except FileNotFoundError:
        save_conf(c)
    except Exception:
        pass
    return c


def save_conf(c):
    try:
        with open(CONF_PATH, "w") as f:
            json.dump(c, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------- data: fast
DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+|mmcblk\d+)$")


def list_disks():
    out = []
    for n in sorted(os.listdir("/sys/block")):
        if DISK_RE.match(n):
            sz = int(rd(f"/sys/block/{n}/size", "0") or 0) * 512
            out.append({"name": n, "model": rd(f"/sys/block/{n}/device/model", "?"),
                        "size": sz, "rot": rd(f"/sys/block/{n}/queue/rotational") == "1"})
    return out


def hwmon_scan():
    temps, fans = [], []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = rd(h + "/name")
        if name in ("drivetemp", "nvme"):
            continue
        tl = []
        for f in sorted(glob.glob(h + "/temp*_input")):
            try:
                v = int(rd(f)) / 1000
            except ValueError:
                continue
            if 0 < v < 150:
                tl.append((rd(f.replace("_input", "_label"), os.path.basename(f).split("_")[0]), v))
        if name in ("coretemp", "k10temp", "zenpower"):
            pk = [x for x in tl if re.match(r"(Package|Tctl|Tdie)", x[0])]
            if pk or tl:
                temps.append(("CPU", pk[0][1] if pk else max(v for _, v in tl)))
            core = [v for l, v in tl if l.startswith("Core")]
            if core:
                temps.append(("cores max", max(core)))
        else:
            for lab, v in tl[:6]:
                temps.append((f"{name}.{lab}" if len(tl) > 1 else name, v))
        for f in sorted(glob.glob(h + "/fan*_input")):
            try:
                r = int(rd(f))
            except ValueError:
                continue
            fans.append((f"{name}.{rd(f.replace('_input', '_label'), os.path.basename(f).split('_')[0])}", r))
    if not any(k == "CPU" for k, _ in temps):
        for z in glob.glob("/sys/class/thermal/thermal_zone*"):
            t = rd(z + "/type")
            if "pkg" in t or "cpu" in t:
                try:
                    temps.insert(0, ("CPU", int(rd(z + "/temp")) / 1000)); break
                except ValueError:
                    pass
    return temps, fans



# ---------------------------------------------------------------- Intel iGPU (i915 PMU, RC6 fallback)
_SYS = {"x86_64": 298, "aarch64": 241}.get(platform.machine(), 298)
ENG_NAMES = {"rcs": "Render", "bcs": "Blitter", "vcs": "Video", "vecs": "VidEnh", "ccs": "Compute"}


def perf_open(typ, config):
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        attr = ctypes.create_string_buffer(struct.pack("IIQQQQQ", typ, 128, config, 0, 0, 0, 0).ljust(128, b"\0"), 128)
        return libc.syscall(_SYS, attr, -1, 0, -1, 0)  # pid=-1 cpu=0 (system-wide), needs root
    except Exception:
        return -1


def first(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


class IntelGpu:
    def __init__(self):
        self.card, self.fds, self.pv, self.t = None, {}, {}, None
        self.rc6_prev = self.rapl_prev = None
        self.data = {}
        try:
            for c in sorted(glob.glob("/sys/class/drm/card[0-9]")):
                if rd(c + "/device/vendor") == "0x8086":
                    self.card = c; break
            if not self.card:
                return
            c = self.card
            self.driver = os.path.basename(os.path.realpath(c + "/device/driver"))
            self.freq = first([c + "/gt/gt0/rps_act_freq_mhz", c + "/gt_act_freq_mhz",
                               c + "/gt/gt0/rps_cur_freq_mhz", c + "/gt_cur_freq_mhz"])
            self.fmax = first([c + "/gt/gt0/rps_max_freq_mhz", c + "/gt_max_freq_mhz", c + "/gt/gt0/rps_RP0_freq_mhz"])
            self.rc6 = first([c + "/gt/gt0/rc6_residency_ms", c + "/power/rc6_residency_ms"])
            self.temp = first(glob.glob(c + "/device/hwmon/hwmon*/temp1_input"))
            self.rapl = next((os.path.dirname(f) for f in glob.glob("/sys/class/powercap/intel-rapl:*:*/name")
                              if rd(f) == "uncore"), None)
            for dev in sorted(glob.glob("/sys/bus/event_source/devices/i915*")):
                typ = int(rd(dev + "/type", "-1"))
                for ev in sorted(glob.glob(dev + "/events/*-busy")):
                    m = re.search(r"config=(0x[0-9a-fA-F]+)", rd(ev))
                    if m:
                        fd = perf_open(typ, int(m.group(1), 16))
                        if fd >= 0:
                            self.fds[os.path.basename(ev)[:-5]] = fd
                if self.fds:
                    break
        except Exception:
            self.card = None

    def read(self):
        if not self.card:
            return {}
        now = time.monotonic(); dt = (now - self.t) if self.t else None; self.t = now
        d = {"name": f"Intel iGPU ({self.driver})", "util": None, "eng": {}, "mode": None}
        try:
            for n, fd in self.fds.items():
                try:
                    v = struct.unpack("Q", os.read(fd, 8))[0]
                except OSError:
                    continue
                if n in self.pv and dt:
                    nm = ENG_NAMES.get(re.sub(r"\d+$", "", n), n)
                    d["eng"][nm] = max(d["eng"].get(nm, 0), min(100.0, max(0.0, (v - self.pv[n]) / (dt * 1e9) * 100)))
                self.pv[n] = v
            if d["eng"]:
                d["util"], d["mode"] = max(d["eng"].values()), "pmu"
            elif self.rc6 and dt:
                r = int(rd(self.rc6, "0") or 0)
                if self.rc6_prev is not None:
                    d["util"] = max(0.0, min(100.0, 100 * (1 - (r - self.rc6_prev) / (dt * 1000))))
                    d["mode"] = "rc6"
                self.rc6_prev = r
            if self.freq:
                d["freq"] = int(rd(self.freq, "0") or 0)
            if self.fmax:
                d["fmax"] = int(rd(self.fmax, "0") or 0)
            if self.temp:
                d["temp"] = int(rd(self.temp, "0") or 0) / 1000
            if self.rapl:
                e, mx = int(rd(self.rapl + "/energy_uj", "0") or 0), int(rd(self.rapl + "/max_energy_range_uj", "0") or 0)
                if self.rapl_prev is not None and dt:
                    x = e - self.rapl_prev
                    d["watts"] = (x + (mx if x < 0 else 0)) / 1e6 / dt
                self.rapl_prev = e
        except Exception:
            pass
        self.data = d
        return d


def gpu_view(L, S, temps):
    """Unified GPU info: Intel iGPU first, else NVIDIA. Returns dict(util,temp,note,lines)."""
    g = L.gpu or {}
    cpu_t = next((v for k, v in temps if k == "CPU"), None)
    v = {"util": None, "temp": None, "note": "", "lines": []}
    if g:
        v["util"] = g.get("util")
        v["temp"] = g.get("temp")
        if v["temp"] is None and cpu_t is not None:
            v["temp"], v["note"] = cpu_t, "pkg"   # iGPU has no own sensor: shares the CPU package die
        tag = {"pmu": "", "rc6": " [est. via RC6]"}.get(g.get("mode"), " [no util source]")
        v["lines"].append(f" GPU   {g['name']}{tag}")
        ex = []
        if "freq" in g: ex.append(f"FREQ {g['freq']}" + (f"/{g['fmax']}" if g.get("fmax") else "") + "MHz")
        if g.get("watts") is not None: ex.append(f"PWR {g['watts']:.1f}W")
        if v["temp"] is not None: ex.append(f"TEMP {v['temp']:.0f}C" + (" (pkg)" if v["note"] else ""))
        v["ex"] = ex
        v["eng"] = g.get("eng") or {}
    elif S.gpu:
        try:
            r = S.gpu[0]
            v["util"], v["temp"] = float(r[1]), float(r[2])
            v["lines"].append(f" GPU   {r[0][:30]}")
            v["ex"] = [f"VRAM {r[3]}/{r[4]}M", f"PWR {r[5]}W", f"TEMP {r[2]}C"]
            v["eng"] = {}
        except (IndexError, ValueError):
            pass
    return v


class History:
    def __init__(self, n=7200):
        self.d = {k: collections.deque(maxlen=n) for k in ("cpu", "cput", "gpu", "gput")}

    def add(self, **kw):
        for k, x in kw.items():
            self.d[k].append(x)


def graph(series, W, h, lo, hi, a, b, step, ascii_mode):
    need = W * step
    data = [None] * max(0, need - len(series)) + list(series)[-need:]
    cols = []
    for i in range(W):
        ch = [x for x in data[i * step:(i + 1) * step] if x is not None]
        cols.append(sum(ch) / len(ch) if ch else None)
    full, half_c, zero = ("#", ":", "_") if ascii_mode else ("\u2588", "\u2584", "_")
    rows = []
    for r in range(h - 1, -1, -1):
        line = []
        for v in cols:
            if v is None:
                line.append(" "); continue
            hv = int(round((min(max(v, lo), hi) - lo) / (hi - lo) * 2 * h))
            if hv >= 2 * (r + 1): c = full
            elif hv == 2 * r + 1: c = half_c
            elif r == 0 and hv == 0: line.append(dim(zero)); continue
            else: line.append(" "); continue
            line.append(col(lvl(v, a, b), c))
        rows.append("".join(line))
    return rows, cols


def one_curve(label, series, unit, lo, hi, a, b, W, h, step, ascii_mode, na=""):
    vals = [x for x in series if x is not None]
    if not vals:
        return [f" {label}  " + dim(na or "n/a")] + [""] * h
    rows, cols = graph(series, W, h, lo, hi, a, b, step, ascii_mode)
    vis = [c for c in cols if c is not None]
    if not vis:   # data exists in history but none inside the visible window (source went away)
        return [f" {label}  " + dim(na or "no recent data")] + [""] * h
    cur = vals[-1]
    hdr = (f" {label}  {col(lvl(cur, a, b), f'{cur:.0f}{unit}')}"
           + dim(f"   min {min(vis):.0f}  max {max(vis):.0f}  range {lo:.0f}-{hi:.0f}{unit}  1col={step}s"))
    return [hdr] + rows


class Live:
    def __init__(self):
        self.clk = os.sysconf("SC_CLK_TCK")
        self.cores_prev, self.net_prev, self.io_prev = self._cores(), self._net(), self._io()
        self.rapl_prev = self._rapl()
        self.t = time.time()
        self.cpu_pct, self.core_pct, self.nic, self.io, self.watts = 0.0, [], {}, {}, None
        self.proc_prev, self.proc_t, self.top = self._procs(), time.time(), []
        self.disks, self.disks_t = list_disks(), time.time()
        self.igpu, self.gpu = IntelGpu(), {}

    @staticmethod
    def _cores():
        res = []
        for l in rd("/proc/stat").splitlines():
            if not l.startswith("cpu"):
                break
            v = [int(x) for x in l.split()[1:]]
            res.append((v[3] + v[4], sum(v)))
        return res

    @staticmethod
    def _net():
        r = {}
        for line in rd("/proc/net/dev").splitlines()[2:]:
            name, data = line.split(":", 1); name = name.strip()
            if os.path.exists(f"/sys/class/net/{name}/device"):
                f = data.split(); r[name] = (int(f[0]), int(f[8]))
        return r

    @staticmethod
    def _io():
        r = {}
        for line in rd("/proc/diskstats").splitlines():
            f = line.split()
            if len(f) > 9 and DISK_RE.match(f[2]):
                r[f[2]] = (int(f[5]) * 512, int(f[9]) * 512)
        return r

    @staticmethod
    def _rapl():
        p = "/sys/class/powercap/intel-rapl:0/energy_uj"
        try:
            return int(rd(p)), int(rd("/sys/class/powercap/intel-rapl:0/max_energy_range_uj", "0") or 0)
        except ValueError:
            return None

    def _procs(self):
        r = {}
        for p in glob.glob("/proc/[0-9]*"):
            try:
                s = open(p + "/stat").read()
                a, b = s.index("("), s.rindex(")")
                f = s[b + 2:].split()
                rss = int(open(p + "/statm").read().split()[1]) * 4096
                r[int(p[6:])] = (s[a + 1:b], int(f[11]) + int(f[12]), rss)
            except Exception:
                pass
        return r

    def update(self):
        now = time.time(); dt = max(now - self.t, 1e-3)
        c = self._cores(); pc = []
        for (i0, t0), (i1, t1) in zip(self.cores_prev, c):
            pc.append(100.0 * (1 - (i1 - i0) / (t1 - t0)) if t1 > t0 else 0.0)
        if pc:
            self.cpu_pct, self.core_pct = pc[0], pc[1:]
        self.cores_prev = c
        n = self._net()
        self.nic = {k: (max(0, v[0] - self.net_prev.get(k, v)[0]) / dt, max(0, v[1] - self.net_prev.get(k, v)[1]) / dt, v[0], v[1])
                    for k, v in n.items()}
        self.net_prev = n
        io = self._io()
        self.io = {k: (max(0, v[0] - self.io_prev.get(k, v)[0]) / dt, max(0, v[1] - self.io_prev.get(k, v)[1]) / dt)
                   for k, v in io.items()}
        self.io_prev = io
        rp = self._rapl()
        if rp and self.rapl_prev:
            d = rp[0] - self.rapl_prev[0]
            if d < 0:
                d += rp[1]
            self.watts = d / 1e6 / dt
        self.rapl_prev = rp
        self.t = now
        self.gpu = self.igpu.read()
        if now - self.proc_t >= 3:
            pr = self._procs(); pdt = now - self.proc_t; rows = []
            for pid, (nm, tk, rss) in pr.items():
                if pid in self.proc_prev:
                    rows.append((nm, pid, (tk - self.proc_prev[pid][1]) / self.clk / pdt * 100, rss))
            self.top = sorted(rows, key=lambda x: -x[2])[:8]
            self.proc_prev, self.proc_t = pr, now
        if now - self.disks_t >= 30:
            self.disks, self.disks_t = list_disks(), now


# ---------------------------------------------------------------- data: slow (background thread)
def smart_one(dev, prev):
    d = dict(prev or {})
    try:
        p = subprocess.run(["smartctl", "-j", "-n", "standby", "-H", "-A", f"/dev/{dev}"],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        d["nosmart"] = True; return d
    if re.search(r"device is in (standby|sleep)", (p.stdout + p.stderr).lower()):
        d["state"] = "standby"; return d
    try:
        j = json.loads(p.stdout)
    except ValueError:
        return d
    d["state"] = "active"
    t = j.get("temperature", {}).get("current")
    if t is not None: d["temp"] = t
    if "smart_status" in j: d["ok"] = j["smart_status"].get("passed")
    poh = j.get("power_on_time", {}).get("hours")
    if poh is not None: d["poh"] = poh
    nv = j.get("nvme_smart_health_information_log")
    if nv and nv.get("percentage_used") is not None: d["wear"] = nv["percentage_used"]
    return d


def hwmon_disk_temps():
    r = {}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        nm = rd(h + "/name")
        try:
            v = int(rd(h + "/temp1_input")) / 1000
        except ValueError:
            continue
        if nm == "drivetemp":
            for b in glob.glob(h + "/device/block/*"):
                r[os.path.basename(b)] = v
        elif nm == "nvme":
            ctl = os.path.basename(os.path.realpath(h + "/device"))
            for b in glob.glob(f"/sys/block/{ctl}n*"):
                r[os.path.basename(b)] = v
    return r


class Slow(threading.Thread):
    def __init__(self, get_conf):
        super().__init__(daemon=True)
        self.get_conf = get_conf
        self.smart, self.docker, self.gpu, self.ips = {}, None, [], {}

    def collect(self, what=("ips", "gpu", "docker", "smart")):
        c = self.get_conf()
        for w in what:
            try:
                getattr(self, "do_" + w)(c)
            except Exception:
                pass

    def do_ips(self, c):
        r = {}
        for fam in ("-4", "-6"):
            o = subprocess.run(["ip", fam, "-o", "addr", "show", "scope", "global"],
                               capture_output=True, text=True, timeout=3).stdout
            for l in o.splitlines():
                f = l.split()
                if len(f) > 3:
                    r.setdefault(f[1], []).append(f[3].split("/")[0])
        self.ips = r

    def do_gpu(self, c):
        if not shutil.which("nvidia-smi"):
            return
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8).stdout
        self.gpu = [[x.strip() for x in l.split(",")] for l in o.splitlines() if l.strip()]

    def do_docker(self, c):
        if not shutil.which("docker"):
            return
        o = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}"],
                           capture_output=True, text=True, timeout=8).stdout
        rows = [l.split("|") for l in o.splitlines() if "|" in l]
        run = [n for n, s in rows if s == "running"]
        self.docker = (len(run), len(rows), run)

    def do_smart(self, c):
        hw = hwmon_disk_temps(); new = {}
        for d in list_disks():
            r = smart_one(d["name"], self.smart.get(d["name"]))
            if r.get("temp") is None and d["name"] in hw:
                r["temp"] = hw[d["name"]]
            new[d["name"]] = r
        self.smart = new

    def run(self):
        last, n = {}, 0
        while True:
            c = self.get_conf(); now = time.time()
            iv = {"ips": 10, "gpu": 5, "docker": 20, "smart": max(10, c.get("smart_interval_sec", 60))}
            for w, every in iv.items():
                if now - last.get(w, 0) >= every:
                    last[w] = now; self.collect((w,))
            time.sleep(1)


# ---------------------------------------------------------------- panels
def sec_system(L, W):
    os_name = ""
    for l in rd("/etc/os-release").splitlines():
        if l.startswith("PRETTY_NAME="):
            os_name = l.split("=", 1)[1].strip('"')
    model = next((l.split(":", 1)[1].strip() for l in rd("/proc/cpuinfo").splitlines() if l.startswith("model name")), "")
    la = rd("/proc/loadavg").split()
    up = float(rd("/proc/uptime", "0").split()[0])
    return [title("SYSTEM", W),
            f" OS    {os_name}   kernel {os.uname().release} {os.uname().machine}",
            f" UP    {fmt_up(up)}   LOAD {' '.join(la[:3])}   TASKS {la[3] if len(la) > 3 else '?'}",
            f" CPU   {model} ({len(L.core_pct)} threads)"]


def sec_cpu(L, W, temps):
    bw = max(8, min(30, W - 30)); t = next((v for k, v in temps if k == "CPU"), None)
    o = [title("CPU", W)]
    o.append(f" USE   {bar(L.cpu_pct, bw)} {L.cpu_pct:5.1f}%" + (f"  {col(lvl(t, 60, 80), f'{t:.0f}C')}" if t is not None else ""))
    fr = [int(x) for x in (rd(f) for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if x.isdigit()]
    ex = []
    if fr: ex.append(f"FREQ {sum(fr) / len(fr) / 1e6:.2f}GHz")
    if L.watts is not None: ex.append(f"PWR {L.watts:.1f}W")
    if ex: o.append(" " + dim("--") + "    " + "  ".join(ex))
    o.append(" CORE  " + "".join(col(level(p), SPARK[min(9, int(p / 10))]) for p in L.core_pct))
    return o


def sec_gpu(gv, W):
    if not gv["lines"]:
        return []
    bw = max(8, min(30, W - 30))
    o = [title("GPU", W)] + gv["lines"]
    u = gv["util"]
    o.append(f" USE   {bar(u, bw)} {u:5.1f}%" if u is not None else " USE   n/a")
    if gv.get("ex"): o.append(" " + dim("--") + "    " + "  ".join(gv["ex"]))
    if gv.get("eng"): o.append(" ENG   " + "  ".join(f"{k} {v:.0f}%" for k, v in gv["eng"].items()))
    return o


def sec_mem(W):
    m = {}
    for l in rd("/proc/meminfo").splitlines():
        k, v = l.split(":", 1); m[k] = int(v.split()[0]) * 1024
    tot = m.get("MemTotal", 1); av = m.get("MemAvailable", 0); used = tot - av; bw = max(8, min(30, W - 30))
    o = [title("MEMORY", W), f" RAM   {bar(used * 100 / tot, bw)} {used * 100 / tot:5.1f}%  {human(used)}/{human(tot)}",
         f" CACHE {human(m.get('Cached', 0))}  BUF {human(m.get('Buffers', 0))}  AVAIL {human(av)}"]
    st = m.get("SwapTotal", 0)
    if st:
        su = st - m.get("SwapFree", 0)
        o.append(f" SWAP  {bar(su * 100 / st, bw)} {su * 100 / st:5.1f}%  {human(su)}/{human(st)}")
    return o


def sec_net(L, S, W):
    o = [title("NETWORK", W)]
    for n in sorted(L.nic):
        rx, tx, rt, tt = L.nic[n]
        sp = rd(f"/sys/class/net/{n}/speed", "-1"); st = rd(f"/sys/class/net/{n}/operstate", "?")
        sp = f"{sp}M" if sp.lstrip("-").isdigit() and int(sp) > 0 else "?"
        up = col(32, "up") if st == "up" else col(31, st)
        o.append(f" {n:<7}{sp:>6} {up}  {' '.join(S.ips.get(n, [])[:2])}")
        o.append(f"        DOWN {human(rx)}/s  UP {human(tx)}/s   total {human(rt)}/{human(tt)}")
    tcp = rd("/proc/net/snmp").splitlines()
    try:
        rows = [l.split() for l in tcp if l.startswith("Tcp:")]
        o.append(f" TCP   established {dict(zip(rows[0], rows[1]))['CurrEstab']}")
    except Exception:
        pass
    return o


def sec_storage(L, W):
    bw = max(8, min(30, W - 40)); o = [title("STORAGE", W)]; seen = set()
    for line in rd("/proc/mounts").splitlines():
        p = line.split()
        if len(p) > 2 and re.fullmatch(r"/vol\d+", p[1]) and p[1] not in seen:
            seen.add(p[1])
            try:
                s = os.statvfs(p[1]); tot = s.f_blocks * s.f_frsize; used = tot - s.f_bavail * s.f_frsize
            except OSError:
                continue
            if tot:
                o.append(f" {p[1]:<5} {p[2][:5]:<5} {bar(used * 100 / tot, bw)} {used * 100 / tot:5.1f}%  {human(used)}/{human(tot)}")
    if not seen:
        o.append(" no /volN mounted")
    r = sum(v[0] for v in L.io.values()); w = sum(v[1] for v in L.io.values())
    o.append(f" DISK IO  read {human(r)}/s  write {human(w)}/s")
    return o


def sec_disks(L, S, W):
    mw = max(6, min(16, W - 61))
    o = [title("DISKS", W), dim(f" {'DEV':<6} {'MODEL':<{mw}} {'SIZE':>6} {'TEMP':>5} {'HLTH':>4} {'POH':>9} {'STATE':<8} {'READ/s':>6} {'WRITE/s':>6}")]
    for d in L.disks:
        n = d["name"]; sm = S.smart.get(n, {}); t = sm.get("temp")
        hi = (45, 55) if d["rot"] else (60, 70)
        tc = "  n/a" if t is None else col(lvl(t, *hi), f"{t:>4.0f}C")
        ok = sm.get("ok"); hl = col(32, "  OK") if ok is True else col(31, "FAIL") if ok is False else dim("   ?")
        poh = f"{sm['poh']}h" if "poh" in sm else "-"
        if sm.get("wear") is not None: poh += f"/{sm['wear']}%w"
        stt = sm.get("state", "?"); stt = dim(f"{stt:<8}") if stt == "standby" else f"{stt:<8}"
        r, w = L.io.get(n, (0, 0))
        o.append(f" {n:<6} {d['model'][:mw]:<{mw}} {human(d['size']):>6} {tc} {hl} {poh[:9]:>9} {stt} {human(r):>6} {human(w):>6}")
    if not L.disks:
        o.append(" no disks found")
    return o


def sec_sensors(W, temps, fans):
    o = [title("SENSORS", W)]
    items = [f"{k} {col(lvl(v, 60, 80), f'{v:.0f}C')}" for k, v in temps]
    o += wrap(" TEMP  ", items, W) if items else [" TEMP  n/a"]
    o += wrap(" FAN   ", [f"{k} {r}rpm" for k, r in fans], W) if fans else [" FAN   n/a"]
    return o


def sec_procs(L, W):
    o = [title("TOP PROCESSES", W), dim(" PID     NAME              CPU%      MEM")]
    for nm, pid, cpu, rss in L.top[:6]:
        o.append(f" {pid:<7} {nm[:16]:<16} {col(level(cpu), f'{cpu:6.1f}')} {human(rss):>8}")
    return o


def sec_docker(S, W):
    if not S.docker:
        return []
    r, t, names = S.docker
    return [title("DOCKER", W)] + wrap(f" {r}/{t} running  ", names, W)


# ---------------------------------------------------------------- render
def sec_trends(H, W, h, conf, two):
    st, asc = max(1, int(conf.get("graph_step_sec", 3))), conf.get("graph") == "ascii"
    cw = W if two else W
    cpu = (one_curve("CPU use ", H.d["cpu"], "%", 0, 100, 70, 90, cw, h, st, asc)
           + one_curve("CPU temp", H.d["cput"], "C", 20, 100, 60, 80, cw, h, st, asc))
    gpu = (one_curve("GPU use ", H.d["gpu"], "%", 0, 100, 70, 90, cw, h, st, asc, "no GPU utilisation source")
           + one_curve("GPU temp", H.d["gput"], "C", 20, 100, 60, 80, cw, h, st, asc, "no GPU temperature source"))
    return [title("CPU CURVES", W)] + cpu, [title("GPU CURVES", W)] + gpu


def render(L, S, H, conf, cols, rows, snap):
    temps, fans, gv = snap
    left = f" fnOS NAS  {socket.gethostname()}"
    right = time.strftime("%Y-%m-%d %H:%M:%S %Z") + " "
    head = [col("1;36", left) + " " * max(1, cols - 1 - len(left) - len(right)) + right, dim("-" * (cols - 1))]
    wide, two_tr = cols >= 130, cols >= 100
    W = (cols - 3) // 2 if wide else cols - 1
    if wide:
        A = sec_system(L, W) + sec_cpu(L, W, temps) + sec_gpu(gv, W) + sec_mem(W) + sec_net(L, S, W) + sec_docker(S, W)
        B = sec_storage(L, W) + sec_disks(L, S, W) + sec_sensors(W, temps, fans) + sec_procs(L, W)
        h = max(len(A), len(B)); A += [""] * (h - len(A)); B += [""] * (h - len(B))
        body = [fit(a, W) + dim(" | ") + fit(b, W) for a, b in zip(A, B)]
    else:
        body = [fit(x, W) for x in (sec_system(L, W) + sec_cpu(L, W, temps) + sec_gpu(gv, W) + sec_mem(W) + sec_net(L, S, W)
                                    + sec_storage(L, W) + sec_disks(L, S, W) + sec_sensors(W, temps, fans)
                                    + sec_procs(L, W) + sec_docker(S, W))]
    # curves always get their rows; the body above is trimmed to make room
    tw = (cols - 3) // 2 if two_tr else cols - 1
    gh = max(2, int(conf.get("graph_height", 4)))
    need = lambda g: (1 + 2 * (1 + g)) if two_tr else 2 * (1 + 2 * (1 + g))
    while gh > 2 and len(head) + 10 + need(gh) > rows - 1:
        gh -= 1
    c, g = sec_trends(H, tw, gh, conf, two_tr)
    if two_tr:
        h = max(len(c), len(g)); c += [""] * (h - len(c)); g += [""] * (h - len(g))
        trend = [fit(a, tw) + dim(" | ") + fit(b, tw) for a, b in zip(c, g)]
    else:
        trend = [fit(x, tw) for x in c + g]
    room = max(0, rows - 1 - len(head) - len(trend))
    return (head + body[:room] + trend)[: max(3, rows - 1)]



# ---------------------------------------------------------------- Windows agent receiver
class Agent(threading.Thread):
    def __init__(self, get_conf):
        super().__init__(daemon=True)
        self.get_conf, self.report, self.ts, self.src = get_conf, None, 0.0, ""

    def current(self):
        return (self.report, time.time() - self.ts) if self.report else (None, 1e9)

    def run(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, code, body=b"ok"):
                self.send_response(code); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                self._send(200 if self.path == "/ping" else 404)

            def do_POST(self):
                c, ip = outer.get_conf(), self.client_address[0]
                if c.get("agent_allow_ip") and ip != c["agent_allow_ip"]:
                    return self._send(403, b"forbidden")
                if c.get("agent_token") and self.headers.get("X-Token") != c["agent_token"]:
                    return self._send(401, b"bad token")
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if not 0 < n <= 262144:
                        raise ValueError
                    rep = json.loads(self.rfile.read(n))
                    if not isinstance(rep, dict):
                        raise ValueError
                except Exception:
                    return self._send(400, b"bad request")
                outer.report, outer.ts, outer.src = rep, time.time(), ip
                self._send(200)

        while True:
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", int(self.get_conf().get("agent_port", 18765))), H)
                srv.daemon_threads = True
                srv.serve_forever()
            except Exception:
                time.sleep(30)


def nv(x, d=0):
    return d if x is None else x


def w_system(R, W):
    c = R.get("cpu") or {}
    o = [title("SYSTEM", W), f" OS    {R.get('os', '?')}",
         f" UP    {fmt_up(nv(R.get('uptime')))}   PROCS {R.get('nproc', '?')}   USER {R.get('user', '-')}",
         f" CPU   {c.get('model', '?')} ({c.get('physical', '?')}C/{c.get('threads', '?')}T)"]
    b = R.get("battery")
    if b:
        o.append(f" BATT  {nv(b.get('pct')):.0f}%  {'charging / AC' if b.get('plugged') else 'on battery'}")
    return o


def w_cpu(R, W):
    c = R.get("cpu") or {}; bw = max(8, min(30, W - 30)); p = nv(c.get("pct")); t = c.get("temp")
    o = [title("CPU", W), f" USE   {bar(p, bw)} {p:5.1f}%" + (f"  {col(lvl(t, 60, 80), f'{t:.0f}C')}" if t is not None else "")]
    ex = []
    if c.get("freq"): ex.append(f"FREQ {c['freq'] / 1000:.2f}GHz")
    if c.get("watts") is not None: ex.append(f"PWR {c['watts']:.1f}W")
    if ex: o.append(" " + dim("--") + "    " + "  ".join(ex))
    o.append(" CORE  " + "".join(col(level(x), SPARK[min(9, int(x / 10))]) for x in (c.get("cores") or [])))
    return o


def w_gpu(R, W):
    gl = R.get("gpus") or []
    if not gl:
        return []
    bw = max(8, min(30, W - 30)); o = [title("GPU", W)]
    for g in gl[:2]:
        u, t = g.get("util"), g.get("temp")
        o.append(f" GPU   {str(g.get('name', '?'))[:W - 8]}")
        o.append(f" USE   {bar(u, bw)} {u:5.1f}%" if u is not None else " USE   n/a")
        ex = []
        if g.get("vram_used") is not None:
            ex.append(f"VRAM {human(g['vram_used'])}" + (f"/{human(g['vram_total'])}" if g.get("vram_total") else ""))
        if g.get("watts") is not None: ex.append(f"PWR {g['watts']:.0f}W")
        if t is not None: ex.append(f"TEMP {col(lvl(t, 60, 80), f'{t:.0f}C')}")
        if ex: o.append(" " + dim("--") + "    " + "  ".join(ex))
    return o


def w_mem(R, W):
    m = R.get("mem") or {}; tot = m.get("total") or 1; used = nv(m.get("used")); bw = max(8, min(30, W - 30))
    o = [title("MEMORY", W), f" RAM   {bar(used * 100 / tot, bw)} {used * 100 / tot:5.1f}%  {human(used)}/{human(tot)}",
         f" AVAIL {human(nv(m.get('avail')))}"]
    st = m.get("swap_total")
    if st:
        su = nv(m.get("swap_used"))
        o.append(f" PAGE  {bar(su * 100 / st, bw)} {su * 100 / st:5.1f}%  {human(su)}/{human(st)}")
    return o


def w_net(R, W):
    o = [title("NETWORK", W)]
    for n in (R.get("net") or [])[:4]:
        sp = f"{n['speed']}M" if n.get("speed") else "?"
        o.append(f" {str(n.get('name', '?'))[:14]:<14}{sp:>6}  {n.get('ip', '')}")
        o.append(f"        DOWN {human(nv(n.get('rx')))}/s  UP {human(nv(n.get('tx')))}/s   total {human(nv(n.get('rx_tot')))}/{human(nv(n.get('tx_tot')))}")
    if len(o) == 1:
        o.append(" no active interface")
    return o


def w_disks(R, W):
    bw = max(8, min(30, W - 40)); o = [title("DISKS", W)]
    for d in (R.get("disks") or [])[:10]:
        tot = d.get("total") or 0
        if tot:
            u = nv(d.get("used"))
            o.append(f" {str(d.get('mount', '?'))[:4]:<4} {str(d.get('fs', ''))[:5]:<5} {bar(u * 100 / tot, bw)} {u * 100 / tot:5.1f}%  {human(u)}/{human(tot)}")
    io = R.get("io") or {}
    o.append(f" DISK IO  read {human(nv(io.get('read')))}/s  write {human(nv(io.get('write')))}/s")
    return o


def w_procs(R, W):
    o = [title("TOP PROCESSES", W), dim(" PID     NAME              CPU%      MEM")]
    for r in (R.get("procs_cpu") or [])[:5]:
        o.append(f" {r[1]:<7} {str(r[0])[:16]:<16} {col(level(r[2]), f'{r[2]:6.1f}')} {human(r[3]):>8}")
    o.append(dim(" by memory"))
    for r in (R.get("procs_mem") or [])[:4]:
        o.append(f" {r[1]:<7} {str(r[0])[:16]:<16} {r[2]:6.1f} {human(r[3]):>8}")
    return o


def nas_strip(L, W, temps):
    m = {}
    for l in rd("/proc/meminfo").splitlines():
        k, v = l.split(":", 1); m[k] = int(v.split()[0])
    mp = 100 * (1 - m.get("MemAvailable", 0) / max(1, m.get("MemTotal", 1)))
    t = next((v for k, v in temps if k == "CPU"), None)
    rx = sum(v[0] for v in L.nic.values()); tx = sum(v[1] for v in L.nic.values())
    txt = (col("1;36", " NAS ") + f"CPU {col(level(L.cpu_pct), f'{L.cpu_pct:.0f}%')}"
           + (f" {t:.0f}C" if t is not None else "") + f"   MEM {col(level(mp), f'{mp:.0f}%')}"
           + f"   NET v{human(rx)}/s ^{human(tx)}/s   up {fmt_up(float(rd('/proc/uptime', '0').split()[0]))}")
    return fit(txt, W)


def render_win(R, WH, conf, cols, rows, age, L, temps):
    left = f" Windows  {R.get('host', '?')}"
    right = time.strftime("%Y-%m-%d %H:%M:%S %Z") + (f"  agent {age:.0f}s ago " if age > 3 else "  agent live ")
    head = [col("1;35", left) + " " * max(1, cols - 1 - len(left) - len(right)) + right, dim("-" * (cols - 1))]
    wide, two_tr = cols >= 130, cols >= 100
    W = (cols - 3) // 2 if wide else cols - 1
    if wide:
        A = w_system(R, W) + w_cpu(R, W) + w_gpu(R, W) + w_mem(R, W) + w_net(R, W)
        B = w_disks(R, W) + sec_sensors(W, [tuple(x) for x in (R.get("sensors") or {}).get("temps", [])],
                                        [tuple(x) for x in (R.get("sensors") or {}).get("fans", [])]) + w_procs(R, W)
        h = max(len(A), len(B)); A += [""] * (h - len(A)); B += [""] * (h - len(B))
        body = [fit(a, W) + dim(" | ") + fit(b, W) for a, b in zip(A, B)]
    else:
        body = [fit(x, W) for x in (w_system(R, W) + w_cpu(R, W) + w_gpu(R, W) + w_mem(R, W) + w_net(R, W)
                                    + w_disks(R, W) + w_procs(R, W))]
    tw = (cols - 3) // 2 if two_tr else cols - 1
    gh = max(2, int(conf.get("graph_height", 4)))
    need = lambda g: (1 + 2 * (1 + g)) if two_tr else 2 * (1 + 2 * (1 + g))
    while gh > 2 and len(head) + 10 + need(gh) + 1 > rows - 1:
        gh -= 1
    c, g = sec_trends(WH, tw, gh, conf, two_tr)
    if two_tr:
        h = max(len(c), len(g)); c += [""] * (h - len(c)); g += [""] * (h - len(g))
        trend = [fit(a, tw) + dim(" | ") + fit(b, tw) for a, b in zip(c, g)]
    else:
        trend = [fit(x, tw) for x in c + g]
    strip = [nas_strip(L, cols - 1, temps)]
    room = max(0, rows - 1 - len(head) - len(trend) - 1)
    return (head + body[:room] + trend + strip)[: max(3, rows - 1)]


def get_size(fd):
    s = os.environ.get("NASSCREEN_SIZE")
    if s:
        c, r = s.split("x"); return int(c), int(r)
    try:
        return tuple(os.get_terminal_size(fd))
    except OSError:
        return 80, 25


def maybe_font(conf, cols):
    f = conf.get("font", "auto")
    if f in ("", "off", None):
        return
    if f == "auto":
        if cols < 200:
            return
        c = sorted(glob.glob("/usr/share/consolefonts/*Terminus*32x16*"))
        if not c:
            return
        f = c[0]
    try:
        subprocess.run(["setfont", "-C", conf["tty"], f], timeout=10, capture_output=True)
    except Exception:
        pass


def snapshot(L, S, H):
    temps, fans = hwmon_scan()
    gv = gpu_view(L, S, temps)
    H.add(cpu=L.cpu_pct, cput=next((v for k, v in temps if k == "CPU"), None), gpu=gv["util"], gput=gv["temp"])
    return temps, fans, gv


def main():
    if "--once" in sys.argv:
        conf = load_conf(); L = Live(); H = History(); S = Slow(lambda: conf)
        S.collect()
        for _ in range(4):
            time.sleep(1); L.update(); snap = snapshot(L, S, H)
        c, r = get_size(1) if sys.stdout.isatty() else (int(os.environ.get("COLUMNS", 160)), int(os.environ.get("ROWS", 60)))
        print("\n".join(render(L, S, H, conf, c, r, snap)) + RST)
        return
    conf = load_conf(); fd = os.open(conf["tty"], os.O_WRONLY | os.O_NOCTTY)
    put = lambda s: os.write(fd, s.encode())

    def bye(*_):
        try:
            put("\033]R\033[?25h\033[0m\033[2J\033[H")
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
    maybe_font(conf, get_size(fd)[0])
    holder = {"c": conf}
    S = Slow(lambda: holder["c"]); S.start()
    A = Agent(lambda: holder["c"]); A.start()
    L, H, WH, tick, was_win, last_ts = Live(), History(), History(), 0, False, 0.0
    put("\033]R\033[0m\033[?25l\033[2J")   # always dark: reset palette, clear
    while True:
        time.sleep(1); L.update()
        if tick % 30 == 0:
            holder["c"] = conf = load_conf()
            put("\033[9;0]\033[14;0]\033[13]")
        tick += 1
        cols, rows = get_size(fd)
        try:
            snap = snapshot(L, S, H)
            rep, age = A.current(); vm = conf.get("view", "auto")
            use_win = bool(rep) and (vm == "windows" or (vm == "auto" and age <= float(conf.get("agent_timeout_sec", 8))))
            if use_win != was_win:
                put("\033[2J")
                if not use_win:
                    WH = History()          # Windows went away: drop its curves
                was_win = use_win
            if use_win:
                if A.ts != last_ts and age < 30:
                    last_ts = A.ts; g0 = (rep.get("gpus") or [{}])[0]
                    WH.add(cpu=(rep.get("cpu") or {}).get("pct"), cput=(rep.get("cpu") or {}).get("temp"),
                           gpu=g0.get("util"), gput=g0.get("temp"))
                lines = render_win(rep, WH, conf, cols, rows, age, L, snap[0])
            else:
                lines = render(L, S, H, conf, cols, rows, snap)
            put("\033[H" + "".join(l + RST + "\033[K\r\n" for l in lines) + "\033[J")
        except OSError:
            pass
        except Exception as e:  # never die on a rendering bug
            try:
                with open(os.path.join(os.path.dirname(CONF_PATH) or ".", "error.log"), "a") as f:
                    f.write(time.strftime("%F %T ") + traceback.format_exc() + "\n")
                put(f"\033[H\033[Jrender error: {e!r}  (see error.log)\r\n")
            except OSError:
                pass


if __name__ == "__main__":
    main()
