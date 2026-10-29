#!/usr/bin/env python3
"""
Pironman Pilot - control center for the SunFounder Pironman 5 MAX case
on a Raspberry Pi 5.

  * Live stats: temperature, clock, load, RAM, fan RPM, power, throttling
  * RGB LEDs + side-fan LEDs (through SunFounder's pironman5 tool)
  * GPIO side fans mode, Pi 5 CPU tower-fan curve, instant "turbo cooling"
  * OLED: SunFounder's built-in pages, or Pilot's own custom pages
  * Overclocking with a safety net (Pi 5 "tryboot": a bad overclock
    falls back to normal settings after a power-cycle) + stability test

Runs as a root systemd service and serves its UI on http://127.0.0.1:47821
(port set in /etc/default/pironman-pilot)
"""
import fcntl
import glob
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False

VERSION = "1.2.1"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
STATE_DIR = os.environ.get("PILOT_STATE_DIR", "/etc/pironman-pilot")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")
BOOT_DIR = "/boot/firmware" if os.path.isdir("/boot/firmware") else "/boot"
CONFIG_TXT = os.path.join(BOOT_DIR, "config.txt")
TRYBOOT_TXT = os.path.join(BOOT_DIR, "tryboot.txt")
PORT = int(os.environ.get("PILOT_PORT", "47821"))
BIND = os.environ.get("PILOT_BIND", "127.0.0.1")
TOKEN = secrets.token_hex(16)

OC_BEGIN = "# >>> Pironman Pilot overclock >>>"
OC_END = "# <<< Pironman Pilot overclock <<<"
FAN_BEGIN = "# >>> Pironman Pilot fan curve >>>"
FAN_END = "# <<< Pironman Pilot fan curve <<<"

DEFAULT_SETTINGS = {
    "oled_mode": "pironman",          # "pironman" (SunFounder pages) or "pilot" (ours)
    "oled_pages": ["temp", "oc", "stats", "clock"],
    "oled_text": "PIRONMAN 5 MAX",
    "oled_interval": 5,
    "oled_rotate": 0,
    "oled_contrast": 200,
    "unit": "C",
    "turbo_cooling": False,
    "cpu_limit": 0,                   # 0 = full speed, else max MHz (live, no reboot)
    "side_ctrl": "pironman",          # "pironman" (SunFounder on/off modes) or "pilot" (our speed curve)
    "side_curve": {"preset": "silent", "points": [[55, 40], [63, 55], [70, 80], [76, 100]]},
    "side_pwm_hz": 50,
    "side_pin": 6,
    "side_park_pin": 26,
    "fan_curve": {"preset": "stock", "points": None},
    "pending_oc": None,
}

OLED_PAGES = [
    ("temp", "Big temperature"),
    ("oc", "Overclock / clock speed"),
    ("stats", "System stats"),
    ("clock", "Clock & date"),
    ("net", "Network / IP"),
    ("text", "Your custom text"),
]

RGB_STYLES = ["solid", "breathing", "flow", "flow_reverse", "rainbow", "rainbow_reverse", "hue_cycle"]

FAN_PRESETS = {  # (temp C, speed 0-255) x4  for the Pi 5 fan header (CPU tower fan)
    # Speeds never go below ~20% (50/255) so the fan doesn't stall and click.
    "whisper": [(65, 50), (71, 80), (76, 140), (80, 255)],
    "silent": [(60, 60), (67, 100), (73, 160), (78, 255)],
    "quiet": [(55, 90), (63, 140), (70, 200), (77, 255)],
    "cool": [(42, 100), (50, 150), (58, 210), (65, 255)],
    "max": [(30, 180), (40, 220), (48, 255), (55, 255)],
}

OC_PRESETS = {
    "stock": {"arm": 2400, "gpu": 910, "ovd": 0},
    "mild": {"arm": 2600, "gpu": 950, "ovd": 10000},
    "strong": {"arm": 2800, "gpu": 1000, "ovd": 25000},
    "max": {"arm": 3000, "gpu": 1100, "ovd": 50000},
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- helpers
def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out"
    except Exception as e:
        return 1, str(e)


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def boot_id():
    return read("/proc/sys/kernel/random/boot_id", "")


def booted_via_tryboot():
    try:
        with open("/proc/device-tree/chosen/bootloader/tryboot", "rb") as f:
            return int.from_bytes(f.read(4), "big") == 1
    except Exception:
        return None


# --------------------------------------------------------------------------- settings
_lock = threading.RLock()


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(SETTINGS_FILE) as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


SET = load_settings()


def save_settings():
    with _lock:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(SET, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)


# --------------------------------------------------------------------------- pironman5 tool
P5 = {"cfg": None, "last_error": None}
P5_FLAGS = {
    "rgb_enable": "-re", "rgb_color": "-rc", "rgb_brightness": "-rb", "rgb_style": "-rs",
    "rgb_speed": "-rp", "rgb_led_count": "-rl", "gpio_fan_mode": "-gm", "gpio_fan_led": "-fl",
    "oled_enable": "-oe", "oled_rotation": "-or", "oled_sleep_timeout": "-os",
    "temperature_unit": "-u", "gpio_fan_pin": "-gp",
}


def p5_bin():
    for c in (shutil.which("pironman5"), "/usr/local/bin/pironman5", "/opt/pironman5/venv/bin/pironman5"):
        if c and os.path.exists(c):
            return c
    return None


def p5_refresh():
    b = p5_bin()
    if not b:
        P5["cfg"] = None
        return None
    rc, out = run([b, "-c"], 20)
    i, j = out.find("{"), out.rfind("}")
    cfg = None
    if i >= 0 and j > i:
        try:
            data = json.loads(out[i:j + 1])
            cfg = data.get("system", data) if isinstance(data, dict) else None
        except Exception:
            cfg = None
    if cfg is None:
        for path in ("/opt/pironman5/config.json", "/etc/pironman5/config.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                cfg = data.get("system", data)
                break
            except Exception:
                pass
    P5["cfg"] = cfg
    return cfg


def _p5_value(key, v):
    if key in ("rgb_enable", "oled_enable"):
        return "true" if bool(v) else "false"
    if key == "rgb_color":
        v = str(v).lstrip("#").lower()
        if not re.fullmatch(r"[0-9a-f]{6}", v):
            raise ValueError("Colour must be 6 hex digits")
        return v
    if key in ("rgb_brightness", "rgb_speed"):
        v = int(v)
        if not 0 <= v <= 100:
            raise ValueError(f"{key} must be 0-100")
        return str(v)
    if key == "rgb_led_count":
        v = int(v)
        if not 1 <= v <= 200:
            raise ValueError("LED count must be 1-200")
        return str(v)
    if key == "rgb_style":
        if v not in RGB_STYLES:
            raise ValueError("Unknown LED style")
        return v
    if key == "gpio_fan_mode":
        v = int(v)
        if not 0 <= v <= 4:
            raise ValueError("Fan mode must be 0-4")
        return str(v)
    if key == "gpio_fan_led":
        if v not in ("on", "off", "follow"):
            raise ValueError("Fan LED must be on/off/follow")
        return v
    if key == "oled_rotation":
        v = int(v)
        if v not in (0, 180):
            raise ValueError("Rotation must be 0 or 180")
        return str(v)
    if key == "oled_sleep_timeout":
        v = int(v)
        if not 0 <= v <= 86400:
            raise ValueError("Sleep timeout out of range")
        return str(v)
    if key == "gpio_fan_pin":
        v = int(v)
        if not 2 <= v <= 27:
            raise ValueError("Fan pin must be GPIO 2-27")
        return str(v)
    if key == "temperature_unit":
        if v not in ("C", "F"):
            raise ValueError("Unit must be C or F")
        return v
    raise ValueError(f"Unknown setting {key}")


def p5_apply(changes, restart=True):
    b = p5_bin()
    if not b:
        raise RuntimeError("SunFounder's pironman5 software isn't installed. Install it first (see README).")
    args = []
    for k, v in changes.items():
        if k not in P5_FLAGS:
            continue
        args += [P5_FLAGS[k], _p5_value(k, v)]
    if not args:
        return P5["cfg"]
    rc, out = run([b] + args, 30)
    if rc != 0:
        P5["last_error"] = out.strip()[-400:]
        raise RuntimeError("pironman5 refused the change: " + P5["last_error"])
    if restart:
        run(["systemctl", "restart", "pironman5.service"], 40)
    time.sleep(0.5)
    return p5_refresh()


# --------------------------------------------------------------------------- vcgencmd bits
def vc(*args):
    return run(["vcgencmd", *args], 5)[1]


def get_config(key):
    m = re.search(r"=(-?\d+)", vc("get_config", key))
    return int(m.group(1)) if m else None


def throttled():
    m = re.search(r"0x([0-9a-fA-F]+)", vc("get_throttled"))
    v = int(m.group(1), 16) if m else 0
    return {
        "raw": hex(v),
        "undervolt_now": bool(v & 0x1), "capped_now": bool(v & 0x2),
        "throttled_now": bool(v & 0x4), "softtemp_now": bool(v & 0x8),
        "undervolt_past": bool(v & 0x10000), "capped_past": bool(v & 0x20000),
        "throttled_past": bool(v & 0x40000), "softtemp_past": bool(v & 0x80000),
    }


def core_volts():
    m = re.search(r"([\d.]+)V", vc("measure_volts", "core"))
    return float(m.group(1)) if m else None


def board_power():
    amps, volts = {}, {}
    for line in vc("pmic_read_adc").splitlines():
        m = re.match(r"\s*(\S+)\s+(current|volt)\(\d+\)=([\d.]+)", line)
        if not m:
            continue
        name, kind, val = m.groups()
        base = name[:-2] if name.endswith(("_A", "_V")) else name
        (amps if kind == "current" else volts)[base] = float(val)
    w = sum(amps[k] * volts[k] for k in amps if k in volts)
    return round(w, 2) if w else None


# --------------------------------------------------------------------------- fans
def pwm_fan_dev():
    for d in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
        if read(d + "/type") == "pwm-fan":
            return d
    return None


def fan_rpm():
    for p in glob.glob("/sys/devices/platform/cooling_fan/hwmon/*/fan1_input") + \
            glob.glob("/sys/class/hwmon/hwmon*/fan1_input"):
        v = read(p)
        if v and v.isdigit():
            return int(v)
    return None


def gpio_pin_high(pin):
    rc, out = run(["pinctrl", "get", str(pin)], 3)
    if rc != 0:
        return None
    if "| hi" in out:
        return True
    if "| lo" in out:
        return False
    return None


def fan_curve_block(points):
    lines = ["[all]"]
    for i, (t, s) in enumerate(points):
        lines += [f"dtparam=fan_temp{i}={int(t * 1000)}",
                  f"dtparam=fan_temp{i}_hyst=5000",
                  f"dtparam=fan_temp{i}_speed={int(s)}"]
    return FAN_BEGIN + "\n" + "\n".join(lines) + "\n" + FAN_END + "\n"


# --------------------------------------------------------------------------- config.txt
def strip_block(text, begin, end):
    pat = re.compile(r"\n?" + re.escape(begin) + r".*?" + re.escape(end) + r"[^\n]*\n?", re.S)
    return pat.sub("\n", text).rstrip("\n") + "\n"


def get_block(text, begin, end):
    m = re.search(re.escape(begin) + r"(.*?)" + re.escape(end), text, re.S)
    return m.group(1) if m else None


def backup_config():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if os.path.exists(CONFIG_TXT):
        dst = os.path.join(BACKUP_DIR, "config.txt." + time.strftime("%Y%m%d-%H%M%S"))
        n = 1
        while os.path.exists(dst + (f"-{n}" if n > 1 else "")):
            n += 1
        shutil.copy2(CONFIG_TXT, dst + (f"-{n}" if n > 1 else ""))
    olds = sorted(glob.glob(os.path.join(BACKUP_DIR, "config.txt.*")))
    for o in olds[:-15]:
        try:
            os.remove(o)
        except Exception:
            pass


def write_boot_file(path, text):
    tmp = path + ".pilot-tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.sync()


def oc_block(arm, gpu, ovd):
    lines = ["[all]", f"arm_freq={arm}"]
    if gpu and gpu != 910:
        lines.append(f"gpu_freq={gpu}")
    if ovd:
        lines.append(f"over_voltage_delta={ovd}")
    return OC_BEGIN + "\n" + "\n".join(lines) + "\n" + OC_END + "\n"


def parse_oc_block(text):
    blk = get_block(text, OC_BEGIN, OC_END)
    if not blk:
        return None
    out = {"arm": None, "gpu": 910, "ovd": 0}
    for k, key in (("arm_freq", "arm"), ("gpu_freq", "gpu"), ("over_voltage_delta", "ovd")):
        m = re.search(rf"^{k}=(\d+)", blk, re.M)
        if m:
            out[key] = int(m.group(1))
    return out


def validate_oc(body):
    arm, gpu, ovd = int(body.get("arm", 0)), int(body.get("gpu", 910)), int(body.get("ovd", 0))
    if not 1500 <= arm <= 3200:
        raise ValueError("CPU clock must be 1500-3200 MHz")
    if not 500 <= gpu <= 1200:
        raise ValueError("GPU clock must be 500-1200 MHz")
    if not 0 <= ovd <= 80000:
        raise ValueError("Voltage boost must be 0-80000 µV (0-80 mV)")
    return arm, gpu, ovd


RUNNING = {"arm": None, "gpu": None, "ovd": None}


def refresh_running():
    RUNNING["arm"] = get_config("arm_freq")
    RUNNING["gpu"] = get_config("gpu_freq")
    RUNNING["ovd"] = get_config("over_voltage_delta")


def check_pending():
    p = SET.get("pending_oc")
    if not p:
        return
    bid = boot_id()
    changed = False
    if p.get("status") == "waiting_reboot" and p.get("boot_id") != bid:
        arm = RUNNING["arm"]
        p["status"] = "booted" if arm == p["arm"] else "fell_back"
        p["seen_arm"] = arm
        p["checked_boot"] = bid
        changed = True
    elif p.get("status") == "booted" and p.get("checked_boot") != bid:
        p["status"] = "expired"  # rebooted again without pressing Keep -> back on normal config
        changed = True
    if changed:
        save_settings()


def oc_status():
    return {
        "running": dict(RUNNING),
        "configured": parse_oc_block(read_text(CONFIG_TXT)),
        "pending": SET.get("pending_oc"),
        "tryboot_now": booted_via_tryboot(),
        "presets": OC_PRESETS,
        "config_path": CONFIG_TXT,
    }


def delayed_reboot(arg=None):
    def go():
        time.sleep(1.5)
        os.sync()
        run(["reboot", arg] if arg else ["reboot"], 30)
    threading.Thread(target=go, daemon=True).start()


# --------------------------------------------------------------------------- stats sampler
STATS = {}
HIST = {"temp": [], "freq": [], "cpu": [], "rpm": []}
HIST_LEN = 180
_prev_cpu = [None]
MODEL = read("/proc/device-tree/model", "Unknown").replace("\x00", "")


def cpu_percent():
    try:
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
    except Exception:
        return None
    idle, total = vals[3] + vals[4], sum(vals)
    prev = _prev_cpu[0]
    _prev_cpu[0] = (idle, total)
    if not prev or total == prev[1]:
        return 0.0
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def mem():
    info = {}
    for line in (read_text("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) if v.split() else 0
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {"total_mb": total // 1024, "used_mb": (total - avail) // 1024,
            "pct": round(100 * (total - avail) / total, 1) if total else None}


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "no network"


def push(key, val):
    h = HIST[key]
    h.append(val)
    if len(h) > HIST_LEN:
        del h[0]


def sampler():
    tick = 0
    fan_dev = pwm_fan_dev()
    while True:
        try:
            t = read("/sys/class/thermal/thermal_zone0/temp")
            STATS["temp"] = round(int(t) / 1000, 1) if t else None
            f = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
            STATS["freq"] = int(f) // 1000 if f else None
            fm = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
            STATS["freq_max"] = int(fm) // 1000 if fm else None
            STATS["cpu"] = cpu_percent()
            STATS["rpm"] = fan_rpm()
            if fan_dev:
                if SET.get("turbo_cooling"):
                    mx = read(fan_dev + "/max_state")
                    try:
                        with open(fan_dev + "/cur_state", "w") as fh:
                            fh.write(mx or "4")
                    except Exception:
                        pass
                STATS["fan_level"] = int(read(fan_dev + "/cur_state", "0") or 0)
                STATS["fan_level_max"] = int(read(fan_dev + "/max_state", "4") or 4)
            if tick % 2 == 0:
                STATS["throttle"] = throttled()
                STATS["volts"] = core_volts()
                STATS["mem"] = mem()
                if SET.get("side_ctrl") == "pilot":
                    d = SIDE.get("duty")
                    STATS["side_fans_on"] = None if d is None else d > 0
                else:
                    pin = (P5["cfg"] or {}).get("gpio_fan_pin", 6)
                    STATS["side_fans_on"] = gpio_pin_high(pin)
                STATS["side_duty"] = SIDE.get("duty") if SET.get("side_ctrl") == "pilot" else None
            if tick % 4 == 0:
                STATS["power_w"] = board_power()
            if tick % 10 == 0:
                STATS["ip"] = local_ip()
                STATS["host"] = socket.gethostname()
                STATS["uptime_s"] = int(float((read("/proc/uptime", "0") or "0").split()[0]))
            push("temp", STATS.get("temp"))
            push("freq", STATS.get("freq"))
            push("cpu", STATS.get("cpu"))
            push("rpm", STATS.get("rpm"))
            TEST_feed()
        except Exception as e:
            log("sampler error:", e)
        tick += 1
        time.sleep(1)


# --------------------------------------------------------------------------- stability test
TEST = {"running": False}
_test_procs = []


def burn_worker(seconds):
    """Hash chain burner. Same input must always give the same output - a mismatch means the CPU made a maths error."""
    end = time.time() + seconds
    ref, rounds, bad = None, 0, 0
    while time.time() < end:
        h = b"pironman-pilot"
        for _ in range(20000):
            h = hashlib.sha256(h).digest()
        if ref is None:
            ref = h
        elif h != ref:
            bad += 1
        rounds += 1
    print(json.dumps({"rounds": rounds, "bad": bad, "ref": ref.hex() if ref else ""}), flush=True)


def TEST_feed():
    if not TEST.get("running"):
        return
    t, f, th = STATS.get("temp"), STATS.get("freq"), STATS.get("throttle") or {}
    if t is not None:
        TEST["max_temp"] = max(TEST.get("max_temp") or 0, t)
    if f:
        TEST["min_freq"] = min(TEST.get("min_freq") or 99999, f)
    for k in ("undervolt_now", "capped_now", "throttled_now", "softtemp_now"):
        if th.get(k):
            TEST.setdefault("flags", [])
            name = k.replace("_now", "")
            if name not in TEST["flags"]:
                TEST["flags"].append(name)
    TEST["elapsed"] = int(time.time() - TEST["start"])


def test_start(body):
    if TEST.get("running"):
        raise RuntimeError("A test is already running")
    secs = int(body.get("seconds", 120))
    if not 15 <= secs <= 1800:
        raise ValueError("Duration must be 15-1800 s")
    TEST.clear()
    TEST.update({"running": True, "start": time.time(), "duration": secs, "elapsed": 0,
                 "max_temp": None, "min_freq": None, "flags": [], "result": None,
                 "arm": RUNNING.get("arm")})
    threading.Thread(target=_test_thread, args=(secs,), daemon=True).start()
    return {"ok": True}


def _test_thread(secs):
    _test_procs.clear()
    n = os.cpu_count() or 4
    for _ in range(n):
        _test_procs.append(subprocess.Popen([sys.executable, os.path.abspath(__file__), "--burn", str(secs)],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
    results = []
    for p in _test_procs:
        try:
            out, _ = p.communicate(timeout=secs + 60)
            results.append(json.loads(out.strip().splitlines()[-1]))
        except Exception:
            results.append(None)
    cancelled = TEST.get("cancelled")
    refs = {r["ref"] for r in results if r and r.get("ref")}
    bad = sum(r["bad"] for r in results if r) + (len(refs) - 1 if len(refs) > 1 else 0)
    crashed = sum(1 for r in results if r is None)
    flags = TEST.get("flags", [])
    if cancelled:
        verdict = "cancelled"
    elif bad or crashed:
        verdict = "unstable"
    elif "undervolt" in flags:
        verdict = "power"
    elif "throttled" in flags or "softtemp" in flags or "capped" in flags:
        verdict = "hot"
    else:
        verdict = "pass"
    TEST.update({"running": False, "result": verdict, "errors": bad, "crashed": crashed,
                 "rounds": sum(r["rounds"] for r in results if r), "finished": time.time()})


def test_stop(_body):
    if TEST.get("running"):
        TEST["cancelled"] = True
        for p in _test_procs:
            try:
                p.terminate()
            except Exception:
                pass
    return {"ok": True}


# --------------------------------------------------------------------------- OLED (SSD1306 128x64 @0x3C)
class SSD1306:
    def __init__(self, bus=1, addr=0x3C, rotate=0, contrast=200):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, 0x0703, addr)  # I2C_SLAVE
        seg, com = (0xA0, 0xC0) if rotate == 180 else (0xA1, 0xC8)
        for c in (0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x00,
                  seg, com, 0xDA, 0x12, 0x81, max(0, min(255, contrast)), 0xD9, 0xF1, 0xDB, 0x40,
                  0xA4, 0xA6, 0xAF):
            self.cmd(c)

    def cmd(self, *c):
        os.write(self.fd, bytes([0x00, *c]))

    def show(self, buf):
        self.cmd(0x21, 0, 127)
        self.cmd(0x22, 0, 7)
        for i in range(0, 1024, 32):
            os.write(self.fd, bytes([0x40]) + bytes(buf[i:i + 32]))

    def blank(self):
        self.show(bytearray(1024))
        self.cmd(0xAE)

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


_fonts = {}


def font(size, bold=True):
    key = (size, bold)
    if key in _fonts:
        return _fonts[key]
    names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"] if bold else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"])
    f = None
    for n in names:
        hits = glob.glob(f"/usr/share/fonts/truetype/*/{n}")
        if hits:
            f = ImageFont.truetype(hits[0], size)
            break
    if f is None:
        try:
            f = ImageFont.load_default(size)
        except TypeError:
            f = ImageFont.load_default()
    _fonts[key] = f
    return f


def _tw(d, text, f):
    try:
        return d.textlength(text, font=f)
    except Exception:
        return len(text) * 6


def _center(d, y, text, f):
    d.text(((128 - _tw(d, text, f)) / 2, y), text, font=f, fill=1)


def _temp_str(t):
    if t is None:
        return "--"
    if SET.get("unit") == "F":
        return f"{t * 9 / 5 + 32:.0f}°F"
    return f"{t:.0f}°C"


def _wrap(d, text, f, width=126):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split():
            test = (cur + " " + w).strip()
            if _tw(d, test, f) <= width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def render_page(name):
    img = Image.new("1", (128, 64), 0)
    d = ImageDraw.Draw(img)
    st = STATS
    if name == "temp":
        t = st.get("temp")
        d.text((0, 0), "CPU TEMP", font=font(10), fill=1)
        rpm = st.get("rpm")
        r = f"{rpm} rpm" if rpm is not None else ""
        d.text((128 - _tw(d, r, font(10, False)), 0), r, font=font(10, False), fill=1)
        _center(d, 13, _temp_str(t), font(32))
        frac = max(0.0, min(1.0, ((t or 30) - 30) / 55))
        d.rectangle((0, 55, 127, 63), outline=1)
        d.rectangle((2, 57, 2 + int(123 * frac), 61), fill=1)
    elif name == "oc":
        f = st.get("freq") or 0
        d.text((0, 0), "CPU CLOCK", font=font(10), fill=1)
        th = st.get("throttle") or {}
        bad = th.get("throttled_now") or th.get("capped_now") or th.get("undervolt_now")
        tag = "THROTTLE" if bad else "OK"
        d.text((128 - _tw(d, tag, font(10)), 0), tag, font=font(10), fill=1)
        _center(d, 13, f"{f / 1000:.2f} GHz", font(24))
        arm, gpu, ovd = RUNNING.get("arm"), RUNNING.get("gpu"), RUNNING.get("ovd")
        sub = f"max {arm or '?'}  GPU {gpu or '?'}"
        if ovd:
            sub += f"  +{ovd // 1000}mV"
        for sz in (10, 9, 8):
            if _tw(d, sub, font(sz, False)) <= 126:
                break
        _center(d, 47, sub, font(sz, False))
    elif name == "stats":
        m = st.get("mem") or {}
        rows = [
            f"CPU {st.get('cpu') or 0:.0f}%   RAM {m.get('pct') or 0:.0f}%",
            f"TEMP {_temp_str(st.get('temp'))}  FAN {st.get('rpm') or 0}",
            f"{(st.get('freq') or 0)} MHz  {st.get('power_w') or 0:.1f} W",
            f"{st.get('ip') or ''}",
        ]
        for i, r in enumerate(rows):
            d.text((0, i * 16 + 1), r, font=font(10, False), fill=1)
    elif name == "clock":
        _center(d, 2, time.strftime("%H:%M"), font(34))
        _center(d, 46, time.strftime("%a %d %b %Y"), font(12, False))
    elif name == "net":
        d.text((0, 0), "NETWORK", font=font(10), fill=1)
        _center(d, 18, st.get("host") or socket.gethostname(), font(14))
        _center(d, 40, st.get("ip") or "", font(13, False))
    elif name == "text":
        text = (SET.get("oled_text") or "").strip() or "Hello!"
        for size in (26, 20, 16, 13, 11, 9):
            f = font(size)
            lines = _wrap(d, text, f)
            lh = size + 2
            if len(lines) * lh <= 64 and all(_tw(d, ln, f) <= 127 for ln in lines):
                y = (64 - len(lines) * lh) // 2
                for ln in lines:
                    _center(d, y, ln, f)
                    y += lh
                break
    return img


def image_to_buf(img):
    px = img.load()
    buf = bytearray(1024)
    for page in range(8):
        base = page * 128
        for x in range(128):
            b = 0
            for bit in range(8):
                if px[x, page * 8 + bit]:
                    b |= 1 << bit
            buf[base + x] = b
    return buf


class OledRunner(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.error = None
        self.current = None

    def run(self):
        dev, applied, idx, last = None, None, 0, time.time()
        while not self.stop_evt.is_set():
            try:
                want = (int(SET.get("oled_rotate", 0)), int(SET.get("oled_contrast", 200)))
                if dev is None or applied != want:
                    if dev:
                        dev.close()
                    dev = SSD1306(rotate=want[0], contrast=want[1])
                    applied = want
                pages = [p for p in SET.get("oled_pages", []) if p in dict(OLED_PAGES)] or ["temp"]
                if time.time() - last >= max(2, int(SET.get("oled_interval", 5))):
                    idx += 1
                    last = time.time()
                idx %= len(pages)
                self.current = pages[idx]
                dev.show(image_to_buf(render_page(pages[idx])))
                self.error = None
            except Exception as e:
                self.error = f"OLED: {e}"
                if dev:
                    dev.close()
                dev = None
                self.stop_evt.wait(3)
            self.stop_evt.wait(1)
        if dev:
            try:
                dev.blank()
            except Exception:
                pass
            dev.close()



# --------------------------------------------------------------------------- side RGB fans (Pilot speed curve)
# The MAX's two side fans are plain 2-wire fans switched by GPIO6. SunFounder only switches them on/off.
# Pilot can take the pin over and pulse it (software PWM via lgpio) to run them at partial speed.
# SunFounder's pironman5 is moved to a spare "parking" pin so the two programs don't fight over GPIO6.
SIDE_PRESETS = {  # [temp C, speed %] x4; fans are off below the first temperature
    "whisper": [[60, 35], [68, 50], [74, 75], [78, 100]],
    "silent": [[55, 40], [63, 55], [70, 80], [76, 100]],
    "balanced": [[50, 45], [58, 65], [65, 85], [70, 100]],
    "cool": [[42, 50], [50, 70], [57, 90], [62, 100]],
}
SIDE = {"duty": None, "method": None, "error": None, "override": None, "running": False}

try:
    import lgpio
    HAVE_LGPIO = True
except Exception:
    HAVE_LGPIO = False


def chip_info(h):
    """lgpio returns (status, lines, name, label); older builds may omit status."""
    info = lgpio.gpio_get_chip_info(h)
    info = list(info) if isinstance(info, (list, tuple)) else [info]
    if len(info) == 4:
        info = info[1:]
    return info  # [lines, name, label]


def find_rp1_chip():
    """Find the gpiochip that drives the Pi 5 header (label 'pinctrl-rp1', 54 lines)."""
    fallback = None
    for chip in range(0, 16):
        try:
            h = lgpio.gpiochip_open(chip)
        except Exception:
            continue
        try:
            info = chip_info(h)
        except Exception:
            info = []
        finally:
            lgpio.gpiochip_close(h)
        text = " ".join(str(x) for x in info).lower()
        if "rp1" in text:
            return chip
        if info and str(info[0]) == "54" and fallback is None:
            fallback = chip
    return fallback


class SideFanDriver:
    """PWM through lgpio if possible, otherwise plain on/off through `pinctrl`."""

    def __init__(self, pin, hz):
        self.pin, self.hz, self.h, self.last = pin, hz, None, None
        self.note = None
        if not HAVE_LGPIO:
            self.note = "python3-lgpio isn't installed"
        else:
            chip = find_rp1_chip()
            if chip is None:
                self.note = "couldn't find the Pi 5 GPIO chip (rp1)"
            else:
                h = lgpio.gpiochip_open(chip)
                try:
                    lgpio.gpio_claim_output(h, pin, 0)
                except Exception as e:
                    lgpio.gpiochip_close(h)
                    raise RuntimeError(f"GPIO{pin} is still held by another program (usually SunFounder's "
                                       f"pironman5) - {e}. Switch to On/off and back to Speed curve, or reboot.")
                self.h = h
        self.method = "pwm" if self.h is not None else "onoff"

    def set(self, pct):
        pct = max(0, min(100, int(round(pct))))
        if pct == self.last:
            return
        self.last = pct
        if self.h is not None:
            if pct in (0, 100):
                lgpio.tx_pwm(self.h, self.pin, 0, 0)
                lgpio.gpio_write(self.h, self.pin, 1 if pct else 0)
            else:
                lgpio.tx_pwm(self.h, self.pin, self.hz, pct)
        else:
            run(["pinctrl", "set", str(self.pin), "op", "dh" if pct > 0 else "dl"], 3)

    def close(self, leave_on=True):
        try:
            if self.h is not None:
                lgpio.tx_pwm(self.h, self.pin, 0, 0)
                lgpio.gpio_write(self.h, self.pin, 1 if leave_on else 0)
                lgpio.gpio_free(self.h, self.pin)
                lgpio.gpiochip_close(self.h)
            else:
                run(["pinctrl", "set", str(self.pin), "op", "dh" if leave_on else "dl"], 3)
        except Exception:
            pass


def side_curve_points():
    c = SET.get("side_curve") or {}
    pts = SIDE_PRESETS.get(c.get("preset")) or c.get("points") or SIDE_PRESETS["silent"]
    return [[float(t), float(p)] for t, p in pts]


def side_target(temp, was_on, points):
    """Speed % for a temperature. Off below the first point (3°C hysteresis), linear in between."""
    if temp is None:
        return 100
    t0 = points[0][0]
    if temp < t0 - (3 if was_on else 0):
        return 0
    if temp <= t0:
        return points[0][1]
    for (ta, pa), (tb, pb) in zip(points, points[1:]):
        if temp <= tb:
            return pa + (pb - pa) * (temp - ta) / (tb - ta)
    return points[-1][1]


class SideFanRunner(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()

    def run(self):
        drv, temps = None, []
        SIDE["running"] = True
        while not self.stop_evt.is_set():
            try:
                if drv is None:
                    drv = SideFanDriver(int(SET.get("side_pin", 6)), int(SET.get("side_pwm_hz", 50)))
                    SIDE["method"] = drv.method
                    SIDE["note"] = drv.note
                hz = int(SET.get("side_pwm_hz", 50))
                if drv.hz != hz:
                    drv.hz, drv.last = hz, None
                t = STATS.get("temp")
                if t is not None:
                    temps = (temps + [t])[-5:]
                avg = sum(temps) / len(temps) if temps else None
                ov = SIDE.get("override")
                if ov and time.time() < ov[1]:
                    want = ov[0]
                elif SET.get("turbo_cooling"):
                    want = 100
                else:
                    SIDE["override"] = None
                    want = side_target(avg, (SIDE.get("duty") or 0) > 0, side_curve_points())
                    if 0 < want < 100 and drv.method == "onoff":
                        want = 100  # can't do partial speed without PWM
                prev = SIDE.get("duty") or 0
                if prev == 0 and 0 < want < 60:
                    drv.set(100)          # kick-start so a slow fan actually begins turning
                    self.stop_evt.wait(1.2)
                drv.set(want)
                SIDE["duty"] = int(round(want))
                SIDE["error"] = None
            except Exception as e:
                SIDE["error"] = f"Side fans: {e}"
                if drv:
                    drv.close()
                drv = None
                self.stop_evt.wait(3)
            self.stop_evt.wait(1)
        if drv:
            drv.close(leave_on=True)   # hand back with fans running - safest
        SIDE.update(running=False, duty=None)


SIDE_RUN = {"runner": None}


def side_start():
    if SIDE_RUN["runner"] and SIDE_RUN["runner"].is_alive():
        return
    SIDE_RUN["runner"] = SideFanRunner()
    SIDE_RUN["runner"].start()


def side_stop():
    r = SIDE_RUN["runner"]
    if r:
        r.stop_evt.set()
        r.join(timeout=5)
    SIDE_RUN["runner"] = None


def act_side(body):
    """Switch who runs the side fans and/or save the curve."""
    mode = body.get("mode", SET.get("side_ctrl"))
    if mode not in ("pironman", "pilot"):
        raise ValueError("Unknown side-fan mode")
    if "curve" in body:
        c = body["curve"] or {}
        preset = c.get("preset", "custom")
        if preset in SIDE_PRESETS:
            SET["side_curve"] = {"preset": preset, "points": SIDE_PRESETS[preset]}
        elif preset == "custom":
            pts, last = [], 0
            for t, p in (c.get("points") or []):
                t, p = float(t), int(p)
                if not 25 <= t <= 85 or not 0 <= p <= 100 or t <= last:
                    raise ValueError("Temperatures must rise 25-85°C, speeds 0-100%")
                last = t
                pts.append([t, p])
            if len(pts) != 4:
                raise ValueError("Custom curve needs 4 points")
            SET["side_curve"] = {"preset": "custom", "points": pts}
        else:
            raise ValueError("Unknown side-fan preset")
    if "pwm_hz" in body:
        hz = int(body["pwm_hz"])
        if hz not in (25, 50, 100, 200, 1000):
            raise ValueError("Unsupported PWM frequency")
        SET["side_pwm_hz"] = hz
    prev = SET.get("side_ctrl")
    pin, park = int(SET.get("side_pin", 6)), int(SET.get("side_park_pin", 26))
    if mode == "pilot" and prev != "pilot":
        if p5_bin():  # move SunFounder's fan control off GPIO6 so it stops switching our pin
            p5_apply({"gpio_fan_pin": park, "gpio_fan_mode": 4})
        SET["side_ctrl"] = "pilot"
        save_settings()
        side_start()
    elif mode == "pironman" and prev == "pilot":
        side_stop()
        SET["side_ctrl"] = "pironman"
        save_settings()
        if p5_bin():
            p5_apply({"gpio_fan_pin": pin})
    else:
        save_settings()
        if mode == "pilot":
            side_start()  # make sure the loop is running
    return {"ok": True, "method": SIDE.get("method")}


def act_side_diag(_body):
    """Plain-language report so a non-working curve can be pinned down."""
    lines = [f"Pironman Pilot {VERSION} - side fan check - {time.strftime('%Y-%m-%d %H:%M')}"]
    lines.append(f"Board: {MODEL}")
    lines.append(f"Who runs the side fans: {SET.get('side_ctrl')}")
    lines.append(f"Pilot fan loop running: {SIDE.get('running')}, speed now: {SIDE.get('duty')}%, method: {SIDE.get('method')}")
    if SIDE.get("error"):
        lines.append(f"Fan loop error: {SIDE['error']}")
    if SIDE.get("note"):
        lines.append(f"Note: {SIDE['note']}")
    lines.append(f"lgpio installed: {HAVE_LGPIO}")
    if HAVE_LGPIO:
        for chip in range(0, 16):
            try:
                h = lgpio.gpiochip_open(chip)
            except Exception:
                continue
            try:
                lines.append(f"  gpiochip{chip}: {chip_info(h)}")
            except Exception as e:
                lines.append(f"  gpiochip{chip}: info failed ({e})")
            finally:
                lgpio.gpiochip_close(h)
        lines.append(f"  chosen chip: {find_rp1_chip()}")
    pin, park = int(SET.get("side_pin", 6)), int(SET.get("side_park_pin", 26))
    for g in (pin, park, 5):
        lines.append(f"pinctrl GPIO{g}: {run(['pinctrl', 'get', str(g)], 3)[1].strip()}")
    cfg = p5_refresh() or {}
    lines.append(f"pironman5 installed: {bool(p5_bin())}, its fan pin: {cfg.get('gpio_fan_pin')}, "
                 f"fan mode: {cfg.get('gpio_fan_mode')}, fan LED: {cfg.get('gpio_fan_led')}")
    lines.append(f"pironman5 service: {run(['systemctl', 'is-active', 'pironman5.service'], 5)[1].strip()}")
    users = run(["sh", "-c", "fuser -v /dev/gpiochip* 2>&1 | tail -n +2"], 5)[1].strip()
    lines.append("Programs using GPIO chips:\n" + (users or "  (none listed)"))
    lines.append(f"CPU temp: {STATS.get('temp')}°C, curve: {side_curve_points()}, PWM {SET.get('side_pwm_hz')} Hz")
    return {"ok": True, "report": "\n".join(lines)}


def act_side_test(body):
    pct = int(body.get("pct", 50))
    if not 0 <= pct <= 100:
        raise ValueError("Speed must be 0-100%")
    if SET.get("side_ctrl") != "pilot":
        raise RuntimeError("Switch the side fans to Pilot speed curve first")
    SIDE["override"] = (pct, time.time() + 10)
    return {"ok": True}


OLED = {"runner": None}


def oled_start():
    if not HAVE_PIL:
        raise RuntimeError("Python Pillow is missing (sudo apt install python3-pil)")
    if OLED["runner"] and OLED["runner"].is_alive():
        return
    OLED["runner"] = OledRunner()
    OLED["runner"].start()


def oled_stop():
    r = OLED["runner"]
    if r:
        r.stop_evt.set()
        r.join(timeout=5)
    OLED["runner"] = None


# --------------------------------------------------------------------------- API actions
def act_p5(body):
    changes = body.get("changes") or {}
    p5_apply(changes)
    return {"ok": True, "p5": P5["cfg"]}


def act_oled(body):
    with _lock:
        for k in ("oled_pages", "oled_text", "oled_interval", "oled_rotate", "oled_contrast", "unit"):
            if k in body:
                SET[k] = body[k]
        SET["oled_text"] = str(SET.get("oled_text", ""))[:120]
        SET["oled_interval"] = max(2, min(60, int(SET.get("oled_interval", 5))))
        SET["oled_rotate"] = 180 if int(SET.get("oled_rotate", 0)) == 180 else 0
        SET["oled_contrast"] = max(1, min(255, int(SET.get("oled_contrast", 200))))
        mode = body.get("mode", SET.get("oled_mode"))
        if mode not in ("pironman", "pilot"):
            raise ValueError("Unknown OLED mode")
        prev = SET.get("oled_mode")
        SET["oled_mode"] = mode
        save_settings()
    if mode == "pilot":
        if prev != "pilot" and p5_bin():
            p5_apply({"oled_enable": False})
        oled_start()
    else:
        if prev == "pilot":
            oled_stop()
        p5c = body.get("p5") or {}
        if prev == "pilot":
            p5c.setdefault("oled_enable", True)
        if p5c and p5_bin():
            p5_apply(p5c)
    return {"ok": True}


def act_turbo(body):
    SET["turbo_cooling"] = bool(body.get("on"))
    save_settings()
    return {"ok": True}


CPU_LIMITS = (0, 1500, 1800, 2000, 2200)


def apply_cpu_limit(mhz):
    """Caps the CPU clock live (no reboot). 0 = back to full speed."""
    pols = glob.glob("/sys/devices/system/cpu/cpufreq/policy*")
    for pol in pols:
        hw_max = read(pol + "/cpuinfo_max_freq")
        hw_min = read(pol + "/cpuinfo_min_freq")
        if not hw_max:
            continue
        khz = int(hw_max) if not mhz else max(int(hw_min or 0), min(int(hw_max), mhz * 1000))
        with open(pol + "/scaling_max_freq", "w") as f:
            f.write(str(khz))
    return bool(pols)


def act_cpu_limit(body):
    mhz = int(body.get("mhz", 0))
    if mhz not in CPU_LIMITS:
        raise ValueError("Unknown speed limit")
    if not apply_cpu_limit(mhz):
        raise RuntimeError("Couldn't find the CPU speed controls")
    SET["cpu_limit"] = mhz
    save_settings()
    return {"ok": True}


def act_silent(_body):
    """One tap: Whisper tower curve + side fans only when hot + turbo off."""
    SET["turbo_cooling"] = False
    save_settings()
    act_fan_curve({"preset": "whisper"})
    note = None
    if SET.get("side_ctrl") == "pilot":
        SET["side_curve"] = {"preset": "whisper", "points": SIDE_PRESETS["whisper"]}
        save_settings()
    elif p5_bin():
        try:
            p5_apply({"gpio_fan_mode": 4})
        except Exception as e:
            note = str(e)
    return {"ok": True, "reboot_needed": True, "note": note}


def act_fan_curve(body):
    preset = body.get("preset", "stock")
    text = strip_block(read_text(CONFIG_TXT), FAN_BEGIN, FAN_END)
    if preset == "stock":
        points = None
    elif preset in FAN_PRESETS:
        points = FAN_PRESETS[preset]
    elif preset == "custom":
        pts = body.get("points") or []
        if len(pts) != 4:
            raise ValueError("Custom curve needs 4 points")
        points = []
        last = 0
        for t, pct in pts:
            t, pct = float(t), int(pct)
            if not 25 <= t <= 85 or not 0 <= pct <= 100 or t <= last:
                raise ValueError("Temperatures must rise 25-85°C, speeds 0-100%")
            last = t
            points.append((t, round(pct * 255 / 100)))
    else:
        raise ValueError("Unknown fan preset")
    if points:
        text = text.rstrip("\n") + "\n\n" + fan_curve_block(points)
    backup_config()
    write_boot_file(CONFIG_TXT, text)
    SET["fan_curve"] = {"preset": preset, "points": points}
    save_settings()
    return {"ok": True, "reboot_needed": True}


def act_oc_test(body):
    arm, gpu, ovd = validate_oc(body)
    base = strip_block(read_text(CONFIG_TXT), OC_BEGIN, OC_END)
    write_boot_file(TRYBOOT_TXT, base.rstrip("\n") + "\n\n" + oc_block(arm, gpu, ovd))
    SET["pending_oc"] = {"arm": arm, "gpu": gpu, "ovd": ovd, "label": body.get("label", "custom"),
                         "status": "waiting_reboot", "boot_id": boot_id(), "created": time.time()}
    save_settings()
    delayed_reboot("0 tryboot")
    return {"ok": True, "rebooting": True}


def _remove_tryboot():
    try:
        os.remove(TRYBOOT_TXT)
    except FileNotFoundError:
        pass


def act_oc_keep(_body):
    p = SET.get("pending_oc")
    if not p or p.get("status") != "booted":
        raise RuntimeError("Nothing to keep - do a safe test boot first")
    base = strip_block(read_text(CONFIG_TXT), OC_BEGIN, OC_END)
    backup_config()
    write_boot_file(CONFIG_TXT, base.rstrip("\n") + "\n\n" + oc_block(p["arm"], p["gpu"], p["ovd"]))
    _remove_tryboot()
    SET["pending_oc"] = None
    save_settings()
    return {"ok": True}


def act_oc_discard(_body):
    _remove_tryboot()
    SET["pending_oc"] = None
    save_settings()
    return {"ok": True}


def act_oc_direct(body):
    arm, gpu, ovd = validate_oc(body)
    base = strip_block(read_text(CONFIG_TXT), OC_BEGIN, OC_END)
    backup_config()
    write_boot_file(CONFIG_TXT, base.rstrip("\n") + "\n\n" + oc_block(arm, gpu, ovd))
    _remove_tryboot()
    SET["pending_oc"] = None
    save_settings()
    return {"ok": True, "reboot_needed": True}


def act_oc_stock(_body):
    backup_config()
    write_boot_file(CONFIG_TXT, strip_block(read_text(CONFIG_TXT), OC_BEGIN, OC_END))
    _remove_tryboot()
    SET["pending_oc"] = None
    save_settings()
    return {"ok": True, "reboot_needed": True}


def act_reboot(_body):
    delayed_reboot()
    return {"ok": True}


def act_p5_refresh(_body):
    p5_refresh()
    return {"ok": True, "p5": P5["cfg"]}


ROUTES = {
    "/api/p5": act_p5, "/api/p5/refresh": act_p5_refresh, "/api/oled": act_oled,
    "/api/fan/turbo": act_turbo, "/api/fan/curve": act_fan_curve,
    "/api/fan/silent": act_silent, "/api/cpu/limit": act_cpu_limit,
    "/api/fan/side": act_side, "/api/fan/side/test": act_side_test, "/api/fan/side/diag": act_side_diag,
    "/api/oc/test": act_oc_test, "/api/oc/keep": act_oc_keep, "/api/oc/discard": act_oc_discard,
    "/api/oc/direct": act_oc_direct, "/api/oc/stock": act_oc_stock, "/api/reboot": act_reboot,
    "/api/test/start": test_start, "/api/test/stop": test_stop,
}


def state():
    r = OLED["runner"]
    return {
        "ok": True, "version": VERSION, "model": MODEL,
        "stats": STATS, "hist": HIST, "p5": P5["cfg"], "p5_available": bool(p5_bin()),
        "settings": {k: SET.get(k) for k in ("oled_mode", "oled_pages", "oled_text", "oled_interval",
                                               "oled_rotate", "oled_contrast", "unit", "turbo_cooling", "cpu_limit",
                                               "side_ctrl", "side_curve", "side_pwm_hz", "side_park_pin",
                                               "fan_curve")},
        "oled": {"pages": OLED_PAGES, "error": r.error if r else None, "current": r.current if r else None,
                 "pil": HAVE_PIL, "i2c": os.path.exists("/dev/i2c-1")},
        "fan_presets": FAN_PRESETS, "side_presets": SIDE_PRESETS,
        "side": {"duty": SIDE.get("duty"), "method": SIDE.get("method"), "error": SIDE.get("error"),
                 "note": SIDE.get("note"),
                 "running": SIDE.get("running"), "lgpio": HAVE_LGPIO,
                 "testing": bool(SIDE.get("override") and time.time() < SIDE["override"][1])},
        "oc": oc_status(), "test": TEST, "rgb_styles": RGB_STYLES,
    }


# --------------------------------------------------------------------------- HTTP
_IP_RE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|\[[0-9a-fA-F:]+\])$")


class Handler(BaseHTTPRequestHandler):
    server_version = "PironmanPilot/" + VERSION

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host in ("127.0.0.1", "localhost", "[::1]"):
            return True
        if BIND in ("127.0.0.1", "localhost"):
            return False
        hn = socket.gethostname().lower()
        return bool(_IP_RE.match(host)) or host in (hn, hn + ".local")

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"ok": False, "error": "bad host"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            html = read_text(os.path.join(WEB_DIR, "index.html")).replace("__PILOT_TOKEN__", TOKEN)
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/pilot-id":
            return self._send(200, {"app": "pironman-pilot", "version": VERSION})
        if u.path == "/icon.svg":
            return self._send(200, read_text(os.path.join(APP_DIR, "icon.svg")), "image/svg+xml")
        tok = self.headers.get("X-Pilot-Token") or (q.get("t") or [""])[0]
        if not secrets.compare_digest(tok, TOKEN):
            return self._send(403, {"ok": False, "error": "bad token - reload the page"})
        if u.path == "/api/state":
            return self._send(200, state())
        if u.path == "/api/oled/preview":
            if not HAVE_PIL:
                return self._send(404, {"ok": False, "error": "no PIL"})
            page = (q.get("page") or ["temp"])[0]
            if page not in dict(OLED_PAGES):
                page = "temp"
            img = render_page(page).convert("L").point(lambda v: 255 if v else 0)
            bio = io.BytesIO()
            img.save(bio, "PNG")
            return self._send(200, bio.getvalue(), "image/png")
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"ok": False, "error": "bad host"})
        if not secrets.compare_digest(self.headers.get("X-Pilot-Token") or "", TOKEN):
            return self._send(403, {"ok": False, "error": "bad token - reload the page"})
        path = urlparse(self.path).path
        fn = ROUTES.get(path)
        if not fn:
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            res = fn(body)
            if path not in ("/api/test/start", "/api/test/stop", "/api/reboot", "/api/oc/test"):
                refresh_running_soon()
            return self._send(200, res)
        except Exception as e:
            log("action error", path, e)
            return self._send(400, {"ok": False, "error": str(e)})


def refresh_running_soon():
    threading.Thread(target=refresh_running, daemon=True).start()


def main():
    if os.geteuid() != 0:
        log("warning: not running as root - hardware controls will fail")
    refresh_running()
    check_pending()
    threading.Thread(target=p5_refresh, daemon=True).start()
    if SET.get("cpu_limit"):
        try:
            apply_cpu_limit(int(SET["cpu_limit"]))
        except Exception as e:
            log("cpu limit:", e)
    threading.Thread(target=sampler, daemon=True).start()
    if SET.get("side_ctrl") == "pilot":
        side_start()
    if SET.get("oled_mode") == "pilot" and HAVE_PIL:
        try:
            oled_start()
        except Exception as e:
            log("oled:", e)

    try:
        srv = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as e:
        log(f"Can't use port {PORT}: {e}. Another app is using it - change PILOT_PORT in /etc/default/pironman-pilot")
        oled_stop()
        sys.exit(1)
    srv.daemon_threads = True

    def bye(*_):
        oled_stop()
        side_stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    log(f"Pironman Pilot {VERSION} on http://{BIND}:{PORT}  ({MODEL})")
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--burn":
        burn_worker(int(sys.argv[2]))
    else:
        main()
