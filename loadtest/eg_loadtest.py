#!/usr/bin/env python3
"""EnergyGuard JupyterHub capacity test.

How many users can have a server running and execute the quickstart notebook
at the same time on this host?

Each simulated user is a real singleuser container, started like DockerSpawner
starts it (same image, 3 GB memory limit without swap, same start command and
before-notebook hooks, the dataset mounted read-only). After a randomised
arrival delay, the user's server starts and the notebook runs cell by cell in
the image's EnergyGuard kernel. When it finishes, the kernel stays loaded, like
an open browser tab, until every user in that step is done.

The test ramps through concurrency levels (1, 2, 3, ... users) and stops at
the first level that fails. A level fails if a notebook fails, a server runs
out of memory, or the 95th-percentile run time is more than --max-slowdown
times the single-user time.

Production is not touched: containers have no network (no Keycloak, MLflow,
MinIO, Hub), and MLflow writes to a SQLite file inside each test directory.

Safety, so this machine does not go down:
  * All test containers live in one parent cgroup with a hard memory cap
    (MemAvailable at start minus --reserve-gb), no swap, a CPU cap
    (--cpu-quota, default 3 of 4 cores) and a low CPU weight, so production
    services win under contention. The kernel enforces this: if the tests use
    too much, only test containers are OOM-killed.
  * A level starts only if its projected memory fits in that budget.
  * A watchdog samples the host every 2 s and kills every test container at
    once if MemAvailable drops below --hard-reserve-gb, or memory pressure
    (PSI full avg10) goes above --max-mem-psi.
  * Ctrl-C, errors and normal exit all remove the test containers and the
    cgroup. Only containers labelled eg.loadtest=1 are ever touched.

Run as root (needs docker and cgroup access):
    sudo python3 loadtest/eg_loadtest.py --dry-run    # checks only, starts nothing
    sudo python3 loadtest/eg_loadtest.py              # the test (~30-60 min)
Results: /var/tmp/eg-loadtest/<timestamp>/ (summary.md, results.json, host_samples.csv)
"""

import argparse
import csv
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABEL = "eg.loadtest=1"
NAME_PREFIX = "egload-"
SLICE = "egloadtest.slice"           # systemd cgroup driver
CGFS_DIR = "/sys/fs/cgroup/egloadtest"  # cgroupfs cgroup driver
GiB = 1024 ** 3

DEFAULT_NOTEBOOK = "/home/tpountridis/EnergyGuard_quickstart_dataset.ipynb"
DEFAULT_DATASET = ("/mnt/datadisk/volumes/jupyterhub_data/datasets/tpountridis@epu.ntua.gr/"
                   "SMART building energy consumption dataset")
DATASET_MOUNT = "/home/jovyan/work/datasets/SMART building energy consumption dataset"

abort = threading.Event()
abort_reason = []
print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(args, check=True, timeout=None, capture=True):
    r = subprocess.run(args, capture_output=capture, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[:500]}")
    return r


# ------------------------------------------------------------------ host stats
def meminfo():
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        d[k] = int(v.split()[0]) * 1024
    return d


def psi(kind):
    """Return {'some': avg10, 'full': avg10} from /proc/pressure/<kind>."""
    out = {}
    try:
        for line in Path(f"/proc/pressure/{kind}").read_text().splitlines():
            parts = line.split()
            out[parts[0]] = float(parts[1].split("=")[1])
    except OSError:
        pass
    return out


def gb(b):
    return round(b / GiB, 2) if b is not None else None


class ParentCgroup:
    """Parent cgroup for all test containers, with hard memory and CPU caps."""

    def __init__(self, driver, mem_max, cpu_quota_pct, cpu_weight):
        self.driver = driver
        self.mem_max = mem_max
        self.cpu_quota_pct = cpu_quota_pct
        self.cpu_weight = cpu_weight
        if driver == "systemd":
            self.path = Path("/sys/fs/cgroup") / SLICE
            self.docker_parent = SLICE
            self.unit_file = Path("/run/systemd/system") / SLICE
        else:
            self.path = Path(CGFS_DIR)
            self.docker_parent = "/egloadtest"

    def create(self):
        if self.driver == "systemd":
            self.unit_file.write_text(
                "[Unit]\nDescription=EnergyGuard JupyterHub load test (temporary)\n\n"
                "[Slice]\n"
                f"MemoryMax={self.mem_max}\nMemorySwapMax=0\n"
                f"CPUQuota={self.cpu_quota_pct}%\nCPUWeight={self.cpu_weight}\n")
            sh(["systemctl", "daemon-reload"])
            sh(["systemctl", "start", SLICE])
        else:
            root = Path("/sys/fs/cgroup")
            ctl = (root / "cgroup.subtree_control").read_text().split()
            for c in ("memory", "cpu"):
                if c not in ctl:
                    (root / "cgroup.subtree_control").write_text(f"+{c}")
            self.path.mkdir(exist_ok=True)
            (self.path / "memory.max").write_text(str(self.mem_max))
            (self.path / "memory.swap.max").write_text("0")
            (self.path / "cpu.max").write_text(f"{self.cpu_quota_pct * 1000} 100000")
            (self.path / "cpu.weight").write_text(str(self.cpu_weight))
            (self.path / "cgroup.subtree_control").write_text("+memory +cpu")

    def read(self, name):
        try:
            return (self.path / name).read_text()
        except OSError:
            return None

    def mem_current(self):
        v = self.read("memory.current")
        return int(v) if v else 0

    def oom_kills(self):
        for line in (self.read("memory.events") or "").splitlines():
            k, _, n = line.partition(" ")
            if k == "oom_kill":
                return int(n)
        return 0

    def cpu_usec(self):
        for line in (self.read("cpu.stat") or "").splitlines():
            k, _, n = line.partition(" ")
            if k == "usage_usec":
                return int(n)
        return 0

    def remove(self):
        if self.driver == "systemd":
            sh(["systemctl", "stop", SLICE], check=False)
            if self.unit_file.exists():
                self.unit_file.unlink()
            sh(["systemctl", "daemon-reload"], check=False)
        else:
            try:
                self.path.rmdir()
            except OSError:
                pass


class Monitor(threading.Thread):
    """Samples host + test cgroup every 2 s; trips the abort on danger."""

    def __init__(self, cg, out_csv, hard_reserve, max_mem_psi):
        super().__init__(daemon=True)
        self.cg, self.hard_reserve, self.max_mem_psi = cg, hard_reserve, max_mem_psi
        self.f = open(out_csv, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(["time", "level", "mem_available_gb", "swap_used_gb", "load1", "cpu_psi_some",
                         "mem_psi_some", "mem_psi_full", "test_mem_gb", "test_cpu_cores", "test_oom_kills"])
        self.level = 0
        self.stop_ev = threading.Event()
        self.window = None  # per-level aggregates
        self.lock = threading.Lock()

    def start_window(self, level):
        with self.lock:
            self.level = level
            self.window = {"min_mem_available": None, "max_test_mem": 0, "max_load1": 0.0,
                           "max_cpu_psi": 0.0, "max_mem_psi_full": 0.0, "cpu_core_samples": []}

    def end_window(self):
        with self.lock:
            w, self.window = self.window, None
        return w

    def run(self):
        last_t, last_cpu = time.time(), self.cg.cpu_usec()
        while not self.stop_ev.wait(2):
            m = meminfo()
            avail = m["MemAvailable"]
            swap_used = m["SwapTotal"] - m["SwapFree"]
            load1 = os.getloadavg()[0]
            cpu_p, mem_p = psi("cpu"), psi("memory")
            now, cpu = time.time(), self.cg.cpu_usec()
            cores = (cpu - last_cpu) / 1e6 / max(now - last_t, 1e-6)
            last_t, last_cpu = now, cpu
            test_mem = self.cg.mem_current()
            self.w.writerow([datetime.now().isoformat(timespec="seconds"), self.level, gb(avail), gb(swap_used),
                             round(load1, 2), cpu_p.get("some"), mem_p.get("some"), mem_p.get("full"),
                             gb(test_mem), round(cores, 2), self.cg.oom_kills()])
            self.f.flush()
            with self.lock:
                w = self.window
                if w is not None:
                    w["min_mem_available"] = avail if w["min_mem_available"] is None else min(w["min_mem_available"], avail)
                    w["max_test_mem"] = max(w["max_test_mem"], test_mem)
                    w["max_load1"] = max(w["max_load1"], load1)
                    w["max_cpu_psi"] = max(w["max_cpu_psi"], cpu_p.get("some", 0.0))
                    w["max_mem_psi_full"] = max(w["max_mem_psi_full"], mem_p.get("full", 0.0))
                    w["cpu_core_samples"].append(cores)
            reason = None
            if avail < self.hard_reserve:
                reason = f"host MemAvailable {gb(avail)} GB < hard reserve {gb(self.hard_reserve)} GB"
            elif mem_p.get("full", 0.0) > self.max_mem_psi:
                reason = f"host memory pressure full avg10={mem_p['full']} > {self.max_mem_psi}"
            if reason and not abort.is_set():
                abort_reason.append(reason)
                abort.set()
                log(f"!!! SAFETY ABORT: {reason} — killing all test containers")
                kill_all()
        self.f.close()


# ------------------------------------------------------------------ containers
def kill_all():
    ids = sh(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], check=False).stdout.split()
    if ids:
        sh(["docker", "rm", "-f", *ids], check=False)


def wait_server_ready(name, timeout=180):
    """Wait until the Jupyter server inside the container is up (like a spawn)."""
    deadline = time.time() + timeout
    while time.time() < deadline and not abort.is_set():
        r = sh(["docker", "logs", name], check=False)
        logs = r.stdout + r.stderr
        if re.search(r"is running at|Jupyter Server .* is running", logs):
            return True
        st = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False).stdout.strip()
        if st != "true":
            return False
        time.sleep(1)
    return False


def simulate_user(args, level, idx, level_dir, cg, release_file, delay):
    """One user: arrive, start a server, run the notebook, keep kernel loaded."""
    name = f"{NAME_PREFIX}L{level}-u{idx}"
    udir = level_dir / f"u{idx}"
    work = udir / "work"
    work.mkdir(parents=True)
    shutil.copy(args.notebook, work / "notebook.ipynb")
    (work / ".eg-auth").mkdir()
    for p in (udir, work, work / ".eg-auth"):
        os.chown(p, 1000, 100)
    os.chown(work / "notebook.ipynb", 1000, 100)
    r = {"user": idx, "name": name, "arrival_delay_s": round(delay, 1), "status": "not_started"}

    if abort.wait(delay):
        r["status"] = "aborted"
        return r
    t_arrive = time.time()
    mem_bytes = parse_size(args.mem_limit)
    cmd = ["docker", "run", "-d", "--name", name, "--label", LABEL,
           "--cgroup-parent", cg.docker_parent,
           "--memory", str(mem_bytes), "--memory-swap", str(mem_bytes),
           "--network", "none",
           "-e", f"MEM_LIMIT={mem_bytes}",
           "-e", "EG_MLFLOW_SSO_AUTO=0",
           "-e", "EG_MLFLOW_TOKEN_DIR=/home/jovyan/work/.eg-auth",
           "-e", "MLFLOW_TRACKING_URI=sqlite:////home/jovyan/work/mlflow.db",
           "-e", "MLFLOW_S3_ENDPOINT_URL=",
           "-v", f"{work}:/home/jovyan/work",
           "-v", f"{args.dataset_dir}:{DATASET_MOUNT}:ro",
           "-v", f"{HERE / 'run_nb.py'}:/loadtest/run_nb.py:ro",
           "-v", f"{release_file.parent}:/loadtest-ctl:ro",
           args.image]
    # No JUPYTERHUB_API_TOKEN: start-notebook runs a plain Jupyter server instead
    # of jupyterhub-singleuser (same server process, no Hub auth).
    try:
        sh(cmd)
    except RuntimeError as e:
        r.update(status="spawn_failed", error=str(e))
        return r
    if not wait_server_ready(name):
        r.update(status="spawn_failed", error="server did not become ready",
                 spawn_s=round(time.time() - t_arrive, 1))
        return r
    r["spawn_s"] = round(time.time() - t_arrive, 1)
    log(f"L{level} u{idx}: server up after {r['spawn_s']}s, running notebook")

    exec_cmd = ["docker", "exec", "-u", "1000", "-w", "/home/jovyan/work", name,
                "python", "/loadtest/run_nb.py", "/home/jovyan/work/notebook.ipynb",
                "/home/jovyan/work/result.json", "--kernel", args.kernel,
                "--think-mean", str(args.think_mean), "--seed", str(level * 1000 + idx),
                "--release-file", f"/loadtest-ctl/{release_file.name}",
                "--hold-max", str(args.run_timeout)]
    proc = subprocess.Popen(exec_cmd, stdout=open(udir / "runner.log", "w"), stderr=subprocess.STDOUT)
    result_path = work / "result.json"
    deadline = time.time() + args.run_timeout
    res = None
    while time.time() < deadline and not abort.is_set():
        try:
            res = json.loads(result_path.read_text())
        except (OSError, ValueError):
            res = None
        if res and res.get("status") in ("ok", "failed"):
            break
        if proc.poll() is not None:  # runner exited before writing a final result
            break
        time.sleep(2)
    r["proc"] = proc
    if res is None or res.get("status") not in ("ok", "failed"):
        logs = sh(["docker", "logs", "--tail", "50", name], check=False)
        text = logs.stdout + logs.stderr
        if "eg-oom-watchdog] memory limit" in text or (res or {}).get("oom_kills_end"):
            r.update(status="oom", error="server stopped: memory limit reached")
        elif abort.is_set():
            r.update(status="aborted")
        elif time.time() >= deadline:
            r.update(status="timeout", error=f"no result after {args.run_timeout}s")
        else:
            r.update(status="failed", error="runner exited without a result (see runner.log)")
        if res:
            r["partial"] = res
        return r

    r["status"] = res["status"]
    r["error"] = res.get("error")
    r["run_s"] = res.get("run_s")
    r["kernel_start_s"] = res.get("kernel_start_s")
    r["mem_peak_gb"] = gb(res.get("mem_peak"))
    r["mem_after_run_gb"] = gb(res.get("mem_after_run"))
    oom0, oom1 = res.get("oom_kills_start") or 0, res.get("oom_kills_end") or 0
    if oom1 > oom0:
        r["status"], r["error"] = "oom", "kernel OOM-killed inside the 3 GB limit"
    r["cells"] = [(c["index"], c["seconds"]) for c in res.get("cells", [])]
    r["total_s"] = round(time.time() - t_arrive, 1)
    log(f"L{level} u{idx}: {r['status']} in {r.get('run_s')}s, peak {r.get('mem_peak_gb')} GB"
        + (f" — {r['error']}" if r.get("error") else ""))
    return r


def parse_size(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgKMG]?)[bB]?", s.strip())
    if not m:
        raise ValueError(f"bad size {s}")
    return int(float(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": GiB}[m.group(2).lower()])


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


# ------------------------------------------------------------------ one level
def run_level(args, level, cg, mon, out_dir, rng):
    level_dir = out_dir / f"L{level:02d}"
    level_dir.mkdir()
    ctl = level_dir / "ctl"
    ctl.mkdir()
    release = ctl / "release"
    # First user arrives at t=0; the others spread uniformly over the window.
    delays = [0.0] + [rng.uniform(0, args.arrival_window) for _ in range(level - 1)]
    log(f"=== Level {level}: {level} user(s), arrivals over {args.arrival_window if level > 1 else 0}s ===")
    mon.start_window(level)
    oom0 = cg.oom_kills()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=level) as ex:
        futs = [ex.submit(simulate_user, args, level, i, level_dir, cg, release, d)
                for i, d in enumerate(delays)]
        users = [f.result() for f in futs]
    # Everyone done: all servers up with loaded kernels (open tabs). Measure.
    time.sleep(5)
    idle_mem = cg.mem_current()
    release.touch()
    for u in users:
        p = u.pop("proc", None)
        if p:
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
    w = mon.end_window()
    kill_all()
    wall = round(time.time() - t0, 1)

    ok = [u for u in users if u["status"] == "ok"]
    runs = [u["run_s"] for u in ok if u.get("run_s") is not None]
    peaks = [u["mem_peak_gb"] for u in ok if u.get("mem_peak_gb") is not None]
    holds = [u["mem_after_run_gb"] for u in ok if u.get("mem_after_run_gb") is not None]
    cores = w["cpu_core_samples"] if w else []
    summary = {
        "level": level, "users": level, "ok": len(ok),
        "failed": [{"user": u["user"], "status": u["status"], "error": u.get("error")} for u in users if u["status"] != "ok"],
        "run_s_median": pct(runs, 50), "run_s_p95": pct(runs, 95), "run_s_max": max(runs) if runs else None,
        "spawn_s_p95": pct([u["spawn_s"] for u in users if u.get("spawn_s") is not None], 95),
        "mem_peak_gb_max": max(peaks) if peaks else None,
        "mem_peak_gb_median": pct(peaks, 50),
        "mem_held_after_run_gb_median": pct(holds, 50),
        "test_mem_all_idle_gb": gb(idle_mem),
        "test_mem_peak_gb": gb(w["max_test_mem"]) if w else None,
        "host_min_mem_available_gb": gb(w["min_mem_available"]) if w else None,
        "host_max_load1": round(w["max_load1"], 2) if w else None,
        "host_max_cpu_psi_some": w["max_cpu_psi"] if w else None,
        "host_max_mem_psi_full": w["max_mem_psi_full"] if w else None,
        "test_cpu_cores_mean": round(statistics.mean(cores), 2) if cores else None,
        "test_cpu_cores_max": round(max(cores), 2) if cores else None,
        "budget_oom_kills": cg.oom_kills() - oom0,
        "wall_s": wall,
        "users_detail": users,
    }
    (level_dir / "level.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


# ------------------------------------------------------------------ ramp
state = {"verdict": None}


def run_levels(args, levels, cg, mon, out_dir, rng, budget, results):
    baseline = None
    for level in levels:
        if abort.is_set():
            break
        if baseline:
            need = level * baseline["per_user_peak"] * 1.15
            if need > budget:
                state["verdict"] = (f"stopped before {level} users: projected {gb(need)} GB > test budget "
                           f"{gb(budget)} GB (memory-bound)")
                log(state["verdict"])
                break
        s = run_level(args, level, cg, mon, out_dir, rng)
        results.append(s)
        log(f"Level {level}: {s['ok']}/{level} ok, run median {s['run_s_median']}s p95 {s['run_s_p95']}s, "
            f"peak/user {s['mem_peak_gb_max']} GB, test mem peak {s['test_mem_peak_gb']} GB, "
            f"min host avail {s['host_min_mem_available_gb']} GB, max load {s['host_max_load1']}")
        if level == 1:
            if s["ok"] != 1:
                state["verdict"] = f"single-user baseline failed: {s['failed']}"
                log(state["verdict"])
                break
            baseline = {"run_s": s["run_s_median"], "per_user_peak": (s["mem_peak_gb_max"] or 3.0) * GiB}
            s["pass"] = True
        else:
            reasons = []
            if s["ok"] < level:
                reasons.append(f"{level - s['ok']} user(s) failed")
            if s["run_s_p95"] and s["run_s_p95"] > args.max_slowdown * baseline["run_s"]:
                reasons.append(f"p95 {s['run_s_p95']}s > {args.max_slowdown}x baseline {baseline['run_s']}s")
            if s["budget_oom_kills"]:
                reasons.append("test memory budget exhausted (OOM inside budget)")
            if abort.is_set():
                reasons.append("safety abort: " + "; ".join(abort_reason))
            s["pass"] = not reasons
            s["fail_reasons"] = reasons
            if reasons:
                state["verdict"] = f"level {level} failed: " + "; ".join(reasons)
                log(state["verdict"])
                break
        if baseline:
            peak = max(baseline["per_user_peak"], (s["mem_peak_gb_max"] or 0) * GiB)
            baseline["per_user_peak"] = peak
        # let the host settle before the next level
        for _ in range(args.cooldown):
            if abort.wait(1):
                break


# ------------------------------------------------------------------ main
def preflight(args):
    problems = []
    if os.geteuid() != 0:
        problems.append("run as root (sudo): needs docker and cgroup access")
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        problems.append("cgroup v2 not found at /sys/fs/cgroup")
    for p, what in ((args.notebook, "notebook"), (args.dataset_dir, "dataset dir"),
                    (HERE / "run_nb.py", "run_nb.py")):
        if not Path(p).exists():
            problems.append(f"{what} not found: {p}")
    driver = None
    if shutil.which("docker") is None:
        problems.append("docker CLI not found")
    elif os.geteuid() == 0:
        r = sh(["docker", "info", "--format", "{{.CgroupDriver}}"], check=False)
        driver = r.stdout.strip() or None
        if driver not in ("systemd", "cgroupfs"):
            problems.append(f"cannot read docker cgroup driver: {r.stderr.strip()[:200]}")
        if sh(["docker", "image", "inspect", args.image], check=False).returncode != 0:
            problems.append(f"image {args.image} not found — build it first (see README)")
        else:
            r = sh(["docker", "run", "--rm", "--network", "none", "-e", "EG_MLFLOW_SSO_AUTO=0",
                    "-e", "MLFLOW_TRACKING_URI=", "--entrypoint", "python", args.image, "-c",
                    "import darts, jupyter_client, json, pathlib, sys; "
                    "p=pathlib.Path('/usr/local/share/jupyter/kernels/" + args.kernel + "/kernel.json'); "
                    "print(darts.__version__, sys.version.split()[0], p.exists())"],
                   check=False, timeout=300)
            out = r.stdout.strip().split()
            if r.returncode != 0:
                problems.append(f"image check failed (darts missing? image not rebuilt?): {r.stderr.strip()[-300:]}")
            elif out[-1] != "True":
                problems.append(f"kernel {args.kernel} not in image")
            else:
                log(f"image ok: darts {out[0]}, Python {out[1]}, kernel {args.kernel} present")
        left = sh(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], check=False).stdout.split()
        if left:
            problems.append(f"{len(left)} leftover test containers exist (label {LABEL}); "
                            "remove with: docker rm -f $(docker ps -aq --filter label=eg.loadtest=1)")
    if driver == "systemd" and Path("/sys/fs/cgroup", SLICE).exists():
        problems.append(f"{SLICE} already exists (a previous run still active?)")
    return problems, driver


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default="energyguard-singleuser:latest")
    ap.add_argument("--notebook", default=DEFAULT_NOTEBOOK)
    ap.add_argument("--dataset-dir", default=DEFAULT_DATASET)
    ap.add_argument("--kernel", default="eg-default", help="kernel to run the notebook in")
    ap.add_argument("--levels", default="1,2,3,4,5,6,8,10,12,15,20",
                    help="concurrency levels to try, in order (first must be 1)")
    ap.add_argument("--arrival-window", type=float, default=60,
                    help="users of a level arrive at random within this many seconds")
    ap.add_argument("--think-mean", type=float, default=0,
                    help="mean pause between cells, seconds (0 = Run All, the notebook's instructions)")
    ap.add_argument("--max-slowdown", type=float, default=3.0,
                    help="level fails if p95 run time > this x single-user run time")
    ap.add_argument("--mem-limit", default="3g", help="per-container limit, as DockerSpawner mem_limit")
    ap.add_argument("--reserve-gb", type=float, default=6,
                    help="memory left for the rest of the host: test budget = MemAvailable - this")
    ap.add_argument("--hard-reserve-gb", type=float, default=3,
                    help="emergency stop if host MemAvailable falls below this")
    ap.add_argument("--max-mem-psi", type=float, default=20,
                    help="emergency stop if memory PSI full avg10 exceeds this (%%)")
    ap.add_argument("--cpu-quota", type=int, default=300,
                    help="CPU cap for all test containers together, %% of one core")
    ap.add_argument("--cpu-weight", type=int, default=50, help="cgroup cpu.weight (default 100 for others)")
    ap.add_argument("--run-timeout", type=int, default=1800, help="per-user timeout, seconds")
    ap.add_argument("--cooldown", type=int, default=20, help="pause between levels, seconds")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/var/tmp/eg-loadtest")
    ap.add_argument("--dry-run", action="store_true", help="run the checks and print the plan only")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",")]
    if levels[0] != 1:
        sys.exit("--levels must start with 1 (single-user baseline)")

    problems, driver = preflight(args)
    m = meminfo()
    budget = int(m["MemAvailable"] - args.reserve_gb * GiB)
    log(f"host: {os.cpu_count()} CPUs, MemTotal {gb(m['MemTotal'])} GB, MemAvailable {gb(m['MemAvailable'])} GB, "
        f"load1 {os.getloadavg()[0]:.2f}")
    log(f"test memory budget: {gb(budget)} GB (MemAvailable - {args.reserve_gb} GB reserve), "
        f"CPU cap {args.cpu_quota / 100:g} cores, emergency stop below {args.hard_reserve_gb} GB available")
    log(f"levels {levels}, arrival window {args.arrival_window}s, max slowdown {args.max_slowdown}x, "
        f"per-container limit {args.mem_limit}, cgroup driver {driver}")
    if budget < parse_size(args.mem_limit):
        problems.append(f"budget {gb(budget)} GB is smaller than one container limit — not enough free memory")
    if problems:
        for p in problems:
            log(f"PROBLEM: {p}")
        sys.exit(1)
    if args.dry_run:
        log("dry run: all checks passed, nothing started")
        return

    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    (out_dir / "config.json").write_text(json.dumps(vars(args) | {"budget_bytes": budget, "driver": driver,
                                                                  "host_cpus": os.cpu_count(),
                                                                  "host_mem_total": m["MemTotal"],
                                                                  "host_mem_available_start": m["MemAvailable"]},
                                                    indent=1))
    log(f"results -> {out_dir}")

    cg = ParentCgroup(driver, budget, args.cpu_quota, args.cpu_weight)
    mon = Monitor(cg, out_dir / "host_samples.csv", int(args.hard_reserve_gb * GiB), args.max_mem_psi)

    def on_signal(signum, frame):
        abort_reason.append(f"signal {signum}")
        abort.set()
        log("interrupted — cleaning up")
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    rng = random.Random(args.seed)
    results = []
    try:
        cg.create()
        mon.start()
        run_levels(args, levels, cg, mon, out_dir, rng, budget, results)
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        state["verdict"] = state["verdict"] or f"test stopped by error: {type(e).__name__}: {e}"
    finally:
        kill_all()
        mon.stop_ev.set()
        mon.join(timeout=10)
        cg.remove()

    write_report(args, out_dir, results, state["verdict"], budget, m)
    sudo_uid, sudo_gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if sudo_uid:
        for root, dirs, files in os.walk(out_dir.parent):
            for n in dirs + files:
                try:
                    os.chown(os.path.join(root, n), int(sudo_uid), int(sudo_gid))
                except OSError:
                    pass
        os.chown(out_dir.parent, int(sudo_uid), int(sudo_gid))
    log(f"done — see {out_dir / 'summary.md'}")


def write_report(args, out_dir, results, verdict, budget, m0):
    passed = [r for r in results if r.get("pass")]
    best = passed[-1]["level"] if passed else 0
    lines = [f"# JupyterHub capacity test — {datetime.now():%Y-%m-%d %H:%M}", "",
             f"- Host: {os.cpu_count()} CPUs, {gb(m0['MemTotal'])} GB RAM, {gb(m0['MemAvailable'])} GB available at start",
             f"- Test budget: {gb(budget)} GB memory, {args.cpu_quota / 100:g} CPU cores (weight {args.cpu_weight})",
             f"- Per user: {args.mem_limit} limit, kernel `{args.kernel}`, arrivals within {args.arrival_window}s, "
             f"think time {args.think_mean}s",
             f"- Pass rule: every notebook succeeds, p95 run time <= {args.max_slowdown}x single user", "",
             f"**Highest passing concurrency: {best} user(s) running the notebook at the same time.**",
             f"Stop reason: {verdict or 'all requested levels passed'}", ""]
    if abort_reason:
        lines += [f"Safety abort: {'; '.join(abort_reason)}", ""]
    lines += ["| users | ok | run median s | run p95 s | slowdown p95 | spawn p95 s | peak/user GB | held/user GB "
              "| test mem peak GB | min host avail GB | max load1 | test cores mean | pass |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    base = results[0]["run_s_median"] if results and results[0]["run_s_median"] else None
    for r in results:
        slow = round(r["run_s_p95"] / base, 2) if base and r["run_s_p95"] else None
        lines.append(f"| {r['users']} | {r['ok']} | {r['run_s_median']} | {r['run_s_p95']} | {slow} | {r['spawn_s_p95']} "
                     f"| {r['mem_peak_gb_max']} | {r['mem_held_after_run_gb_median']} | {r['test_mem_peak_gb']} "
                     f"| {r['host_min_mem_available_gb']} | {r['host_max_load1']} | {r['test_cpu_cores_mean']} "
                     f"| {'yes' if r.get('pass') else 'no'} |")
    held = [r["mem_held_after_run_gb_median"] for r in results if r.get("mem_held_after_run_gb_median")]
    if held:
        h = max(held)
        lines += ["", f"Memory held by a server after the run (kernel still loaded): ~{h} GB. "
                  f"Within the {gb(budget)} GB budget that is room for ~{int(gb(budget) // h)} such servers "
                  "open at once (not all running), before the 1 h idle culler frees them."]
    for r in results:
        for f in r["failed"]:
            lines.append(f"- L{r['level']} user {f['user']}: {f['status']} — {f['error']}")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
