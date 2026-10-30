#!/usr/bin/env python
"""GPU job runner — the one blessed way to run training in the WSL GPU
sandbox from Windows.

Hard-won rules, each guarding a real failure mode:
  * jobs launch DETACHED under an independent process owner — live
    pipes to wsl.exe drop under load and take the process tree with
    them, and WSL reaps orphans when the launching session dies
  * progress and results travel by FILES (run.log, exit.marker) on the
    Windows side, immune to every transport failure mode seen
  * preflight before launch: VM responsive (one wsl --shutdown recovery
    attempt), GPU visible, TF imports in the venv, venv sealed against
    torch/cu13 contamination (mixed CUDA runtimes read as phantom
    OOMs), and memory headroom
  * one job at a time: a lock marker prevents concurrent GPU jobs

Usage:
    python tools/gpu_run.py preflight
    python tools/gpu_run.py submit <name> <script.sh>   # detached start
    python tools/gpu_run.py wait <name> [timeout_s]     # block on marker
    python tools/gpu_run.py run <name> <script.sh> [timeout_s]  # both
    python tools/gpu_run.py status <name>               # tail the log

The script runs INSIDE WSL with LD_LIBRARY_PATH prepared for the venv
at /opt/sortiq-gpu312; write it in WSL terms (bash, /mnt/c/... paths).
Job files live under calibers-adjacent gpu_jobs/ (gitignored territory
is fine — these are run artifacts, not code).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The trainer server runs windowless (pythonw), so every wsl.exe child
# would otherwise conjure a console window in front of everything the
# moment the GPU probe runs. CREATE_NO_WINDOW keeps them invisible.
NOWIN = 0x08000000 if os.name == "nt" else 0

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "gpu_jobs"
VENV = "/opt/sortiq-gpu312"
DIST = "Ubuntu"


def _wsl(args, timeout=30):
    """Run a quick WSL command; (rc, output). Never raises."""
    try:
        r = subprocess.run(["wsl", "-d", DIST, "-u", "root", "--"] + args,
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=NOWIN)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)


def preflight(recover=True):
    """-> (ok, report list). One wsl --shutdown recovery attempt."""
    checks = []

    def fail(name, detail):
        checks.append(f"FAIL {name}: {detail.strip()[:200]}")
        return False

    def ok(name, detail=""):
        checks.append(f"ok   {name} {detail.strip()[:80]}")
        return True

    rc, out = _wsl(["true"], timeout=20)
    if rc != 0:
        if recover:
            subprocess.run(["wsl", "--shutdown"], capture_output=True,
                           timeout=60, creationflags=NOWIN)
            time.sleep(8)
            return preflight(recover=False)
        return False, checks + ["FAIL vm: unresponsive after recovery"]
    ok("vm")

    rc, out = _wsl(["nvidia-smi", "--query-gpu=name,memory.used",
                    "--format=csv,noheader"], timeout=30)
    if rc != 0 or "NVIDIA" not in out:
        return fail("gpu", out), checks
    ok("gpu", out)

    rc, out = _wsl(["bash", "-c",
                    f"ls {VENV}/lib/python3.12/site-packages | "
                    f"grep -ci 'cu13\\|^torch$' || true"], timeout=30)
    if out.strip() and out.strip() != "0":
        return fail("venv-seal", f"{out.strip()} torch/cu13 packages "
                                 "present — mixed CUDA runtimes"), checks
    ok("venv-seal")

    rc, out = _wsl(["bash", "-c", "free -g | awk '/Mem:/ {print $7}'"],
                   timeout=20)
    try:
        if int(out.strip()) < 8:
            return fail("memory", f"only {out.strip()}GB available"), checks
        ok("memory", f"{out.strip()}GB available")
    except ValueError:
        ok("memory", "unreadable — proceeding")

    # TF must SEE the GPU under the exact env jobs get — import success
    # alone once masked a silent CPU fallback. Script goes via file:
    # inline bash across the Windows->WSL boundary gets quote-mangled.
    JOBS.mkdir(parents=True, exist_ok=True)
    probe = JOBS / "_preflight_tfgpu.sh"
    probe.write_bytes("\n".join([
        "P=''",
        f"for d in {VENV}/lib/python3.12/site-packages/nvidia/*/lib; do",
        '  P="$P:$d"',
        "done",
        'export LD_LIBRARY_PATH="${P#:}"',
        f"{VENV}/bin/python -c 'import tensorflow as tf; "
        "print(\"TFGPU=\" + str(len(tf.config.list_physical_devices"
        "(\"GPU\"))), tf.__version__)'",
        ""]).encode())
    rc, out = _wsl(["bash", _win_to_wsl(probe)], timeout=120)
    sentinel = [l for l in out.splitlines() if l.startswith("TFGPU=")]
    if rc != 0 or not sentinel:
        return fail("tensorflow", out), checks
    if sentinel[0].startswith("TFGPU=0"):
        return fail("tf-gpu", "TF sees 0 GPUs — job would silently "
                              "run on CPU"), checks
    ok("tensorflow", sentinel[0])
    return True, checks


def _win_to_wsl(p):
    p = str(Path(p).resolve())
    return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")


def submit(name, script):
    if any(c in name for c in "/\\. "):
        print("bad job name")
        return 2
    job = JOBS / name
    if (job / "running.marker").exists() and not (job / "exit.marker").exists():
        print(f"job {name} already running")
        return 2
    busy = [d.name for d in JOBS.glob("*/running.marker")
            if not (d.parent / "exit.marker").exists()]
    if busy:
        print(f"another GPU job is running: {busy}")
        return 2
    good, checks = preflight()
    (JOBS / name).mkdir(parents=True, exist_ok=True)
    (job / "preflight.txt").write_text("\n".join(checks), encoding="utf-8")
    if not good:
        print("PREFLIGHT FAILED — not launching. Consider the CPU path.")
        print("\n".join(checks))
        return 1
    src = Path(script).read_text(encoding="utf-8").replace("\r\n", "\n")
    (job / "job.sh").write_bytes(src.encode())
    log_w = _win_to_wsl(job / "run.log")
    marker_w = _win_to_wsl(job / "exit.marker")
    job_w = _win_to_wsl(job / "job.sh")
    # the wrapper travels as a FILE: inline bash across the Windows->WSL
    # boundary gets quote-mangled (this exact bug silently dropped
    # LD_LIBRARY_PATH once — TF fell back to CPU without erroring)
    wrapper = "\n".join([
        "P=''",
        f"for d in {VENV}/lib/python3.12/site-packages/nvidia/*/lib; do",
        '  P="$P:$d"',
        "done",
        'export LD_LIBRARY_PATH="${P#:}"',
        # allow_growth counters the BFC preallocation fragmentation that
        # aborted a long run ("Unexpected Event status"). NOT
        # cuda_malloc_async: on WSL2 it stalled fine-tune epochs 200x.
        "export TF_FORCE_GPU_ALLOW_GROWTH=true",
        f"bash {job_w} > {log_w} 2>&1",
        f"echo $? > {marker_w}",
        ""])
    (job / "wrapper.sh").write_bytes(wrapper.encode())
    wrapper_w = _win_to_wsl(job / "wrapper.sh")
    (job / "exit.marker").unlink(missing_ok=True)
    (job / "running.marker").write_text(time.strftime("%F %T"))
    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: the wsl.exe gets a
    # hidden console (DETACHED_PROCESS still flashed a blank window)
    # and belongs to no process group of ours; Windows children survive
    # their parent's death regardless, so the job outlives this process
    # either way
    subprocess.Popen(["wsl", "-d", DIST, "-u", "root", "--",
                      "bash", wrapper_w],
                     creationflags=0x08000000 | 0x00000200,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    print(f"launched {name}")
    return 0


def wait(name, timeout_s=7200):
    job = JOBS / name
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        m = job / "exit.marker"
        if m.exists():
            rc = m.read_text().strip()
            print(f"{name} finished rc={rc}")
            tail = (job / "run.log").read_text(encoding="utf-8",
                                              errors="replace").splitlines()
            print("\n".join(tail[-15:]))
            return 0 if rc == "0" else 1
        time.sleep(20)
    print(f"{name} still running after {timeout_s}s")
    return 3


def kill(name):
    """Reap a hung or runaway job: kill its process tree, clear marker."""
    job = JOBS / name
    job_w = _win_to_wsl(job / "job.sh")
    _wsl(["pkill", "-f", job_w], timeout=20)
    for script in ("tools/embedding_bench2.py", "tools/distill_student.py",
                   "tools/build_gallery.py"):
        _wsl(["pkill", "-f", script], timeout=20)
    time.sleep(3)
    (job / "running.marker").unlink(missing_ok=True)
    print(f"killed {name}")
    return 0


def status(name):
    job = JOBS / name
    for f in ("preflight.txt", "run.log"):
        p = job / f
        if p.exists():
            lines = p.read_text(encoding="utf-8",
                                errors="replace").splitlines()
            print(f"--- {f} (last 10)")
            print("\n".join(lines[-10:]))
    m = job / "exit.marker"
    print("exit:", m.read_text().strip() if m.exists() else "(running)")
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "preflight":
        good, checks = preflight()
        print("\n".join(checks))
        return 0 if good else 1
    if cmd == "submit":
        return submit(sys.argv[2], sys.argv[3])
    if cmd == "wait":
        return wait(sys.argv[2],
                    int(sys.argv[3]) if len(sys.argv) > 3 else 7200)
    if cmd == "run":
        rc = submit(sys.argv[2], sys.argv[3])
        if rc:
            return rc
        return wait(sys.argv[2],
                    int(sys.argv[4]) if len(sys.argv) > 4 else 7200)
    if cmd == "status":
        return status(sys.argv[2])
    if cmd == "kill":
        return kill(sys.argv[2])
    print(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
