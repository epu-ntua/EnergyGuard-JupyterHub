"""Run a notebook the way a user does, inside one singleuser container.

Started by eg_loadtest.py with `docker exec`. It starts a kernel, runs every
code cell in order ("Run All"), optionally pausing between cells like a user
reading along. Then it writes a result JSON and keeps the kernel alive, like
a user who leaves the tab open, until the orchestrator creates the release
file or --hold-max seconds pass.

Only uses packages that are in the image (jupyter_client + ipykernel).
"""

import argparse
import json
import os
import queue
import random
import sys
import time
from pathlib import Path

from jupyter_client.manager import start_new_kernel

CG = Path("/sys/fs/cgroup")


def cg_read(name):
    """Read a value of this container's cgroup (cgroup v2, private namespace)."""
    try:
        return (CG / name).read_text()
    except OSError:
        return None


def cg_int(name):
    v = cg_read(name)
    try:
        return int(v.strip()) if v is not None else None
    except ValueError:
        return None


def oom_kills():
    v = cg_read("memory.events") or ""
    for line in v.splitlines():
        k, _, n = line.partition(" ")
        if k == "oom_kill":
            return int(n)
    return None


def run_cell(km, kc, code, timeout):
    """Execute one cell; returns (status, errors). Notices a dead kernel within ~1 s."""
    msg_id = kc.execute(code, allow_stdin=False, stop_on_error=True)
    deadline = time.time() + timeout
    errors = []
    while True:
        if time.time() > deadline:
            return "timeout", errors + [f"cell timed out after {timeout}s"]
        if not km.is_alive():
            return "dead", errors + ["kernel died (killed, e.g. out of memory)"]
        try:
            msg = kc.get_iopub_msg(timeout=1)
        except queue.Empty:
            continue
        if msg["parent_header"].get("msg_id") != msg_id:
            continue
        if msg["msg_type"] == "error":
            errors.append(f'{msg["content"].get("ename")}: {msg["content"].get("evalue")}')
        elif msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
            break
    while True:  # the matching execute_reply carries the final status
        try:
            reply = kc.get_shell_msg(timeout=30)
        except queue.Empty:
            return "no_reply", errors
        if reply["parent_header"].get("msg_id") == msg_id:
            return reply["content"]["status"], errors


def write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("notebook")
    ap.add_argument("result")
    ap.add_argument("--kernel", default="eg-default")
    ap.add_argument("--cell-timeout", type=int, default=1200)
    ap.add_argument("--think-mean", type=float, default=0.0,
                    help="mean pause between code cells in seconds (exponential), 0 = Run All")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--release-file", default="")
    ap.add_argument("--hold-max", type=int, default=0)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    nb = json.loads(Path(a.notebook).read_text())
    cells = [("".join(c["source"]) if isinstance(c["source"], list) else c["source"])
             for c in nb["cells"] if c.get("cell_type") == "code"]

    res = {"status": "running", "kernel": a.kernel, "cells": [], "error": None,
           "oom_kills_start": oom_kills(), "t_start": time.time()}
    write_json(a.result, res)

    t0 = time.time()
    km, kc = start_new_kernel(kernel_name=a.kernel, startup_timeout=180,
                              cwd=str(Path(a.notebook).parent))
    res["kernel_start_s"] = round(time.time() - t0, 2)

    try:
        for i, code in enumerate(cells):
            if not code.strip():
                continue
            if a.think_mean > 0 and i > 0:
                time.sleep(rng.expovariate(1.0 / a.think_mean))
            c0 = time.time()
            status, errors = run_cell(km, kc, code, a.cell_timeout)
            res["cells"].append({"index": i, "seconds": round(time.time() - c0, 2), "status": status,
                                 "mem_current": cg_int("memory.current")})
            if status != "ok":
                res["error"] = f"cell {i}: " + ("; ".join(errors) or status)
                break
            write_json(a.result, res)
            if not km.is_alive():
                res["error"] = f"kernel died after cell {i}"
                break
    except Exception as e:  # kernel died mid-cell, lost connection, ...
        res["error"] = f"{type(e).__name__}: {e}"

    if res["error"] is None and not km.is_alive():
        res["error"] = "kernel died"
    res["status"] = "ok" if res["error"] is None else "failed"
    res["t_end"] = time.time()
    res["run_s"] = round(res["t_end"] - res["t_start"], 2)
    res["mem_peak"] = cg_int("memory.peak")
    res["mem_after_run"] = cg_int("memory.current")
    res["oom_kills_end"] = oom_kills()
    write_json(a.result, res)

    # Keep the loaded kernel around like an open tab, until released.
    deadline = time.time() + a.hold_max
    while a.release_file and time.time() < deadline and not os.path.exists(a.release_file):
        time.sleep(1)
    try:
        kc.stop_channels()
        km.shutdown_kernel(now=True)
    except Exception:
        pass
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
