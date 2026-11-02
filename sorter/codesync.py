"""Code sync between the machine and a trainer PC.

The machine gets fresh code on every ``tools/pi_deploy.sh`` run, but a
trainer PC is deliberately NOT a git checkout — the repo is private and
test boxes hold no credentials — so historically it was updated by
hand-copying files, and drifted. Drift is not cosmetic here: the trainer
rebuilds crops with its own imaging code, so a stale trainer silently
trains models from geometry the machine will never produce at sort time.

The fix mirrors the dataset pull, in the opposite direction. Both sides
can serve a manifest of their deployable code files (per-file SHA-256
plus one rolled-up digest — that digest IS the version, no git needed);
a trainer compares digests, pulls only what differs from the machine,
deletes what the machine no longer ships, and restarts itself. The
manifest doubles as the serving whitelist, so the file endpoint can
never expose config.json, calibers/, logs, or anything machine-local.

The sync scope below must stay in step with the rsync filters in
``tools/pi_deploy.sh`` — they define what "deployable code" means.
"""
import hashlib
import os
from pathlib import Path

CODE_DIRS = ("docs", "firmware", "sorter", "tools", "webui")
CODE_FILES = ("LICENSE", "README.md", "CHANGELOG.md", "requirements.txt", "VERSION",
              "pyproject.toml", "uv.lock", ".python-version")
_SKIP_DIRS = {"__pycache__", ".git", ".venv", ".claude", "data", "runs"}
_SKIP_SUFFIXES = {".pyc", ".log"}

# sha cache keyed on (size, mtime_ns): docs/ screenshots dominate the tree
# and never change, so steady-state manifest calls hash nothing
_sha_cache = {}


def _sha256(p):
    st = p.stat()
    key, sig = str(p), (st.st_size, st.st_mtime_ns)
    hit = _sha_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    _sha_cache[key] = (sig, h)
    return h


def code_files(root):
    root = Path(root)
    out = [root / n for n in CODE_FILES if (root / n).is_file()]
    for d in CODE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for cur, dirs, files in os.walk(base):
            dirs[:] = sorted(x for x in dirs if x not in _SKIP_DIRS)
            out += [Path(cur) / f for f in sorted(files)
                    if Path(f).suffix.lower() not in _SKIP_SUFFIXES]
    return out


def manifest(root):
    root = Path(root)
    files = [{"path": p.relative_to(root).as_posix(),
              "size": p.stat().st_size, "sha256": _sha256(p)}
             for p in code_files(root)]
    roll = hashlib.sha256("\n".join(
        f"{f['path']} {f['sha256']}" for f in files).encode()).hexdigest()
    return {"digest": roll, "files": files}


def digest(root):
    return manifest(root)["digest"]


def is_git_checkout(root):
    """A git checkout is a dev box: the machine must never clobber
    uncommitted work, so self-update refuses and defers to git."""
    return (Path(root) / ".git").exists()


def plan(root, remote_files):
    """(fetch, delete) to make this install match the machine's manifest."""
    local = {f["path"]: f["sha256"] for f in manifest(root)["files"]}
    want = {f["path"]: f["sha256"] for f in remote_files}
    fetch = sorted(p for p, s in want.items() if local.get(p) != s)
    delete = sorted(p for p in local if p not in want)
    return fetch, delete


def _safe_target(root, rel):
    root = Path(root).resolve()
    p = (root / rel).resolve()
    if not str(p).startswith(str(root) + os.sep):
        raise ValueError(f"path escapes the install: {rel!r}")
    return p


def write_file(root, rel, data):
    p = _safe_target(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".sync-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)  # a crash never leaves a half-written .py behind


def delete_file(root, rel):
    p = _safe_target(root, rel)
    if p.is_file():
        p.unlink()
    d, top = p.parent, Path(root).resolve()
    while d != top and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent
