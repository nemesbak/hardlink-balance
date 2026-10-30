#!/usr/bin/env python3
"""
hardlink_balance.py / EquiLink — Unraid array disk balancer with hardlink protection.

Features & Guarantees:
  1. Inode-Aware Grouping: groups by (st_dev, st_ino) on each source disk and only
     moves a group when ALL its links were found (st_nlink == paths found).
  2. Folder Units: files are planned per folder (season / movie folder, chosen from
     the deepest link, i.e. the library side), so folders are not scattered.
  3. Safe Copy: rsync to a hidden temp name, verify size, then publish with a
     no-clobber link. A crash never leaves a half file under the real name.
  4. Active-File Guard: skips folders with files modified/linked recently, files
     open for writing by any process (qBittorrent, *arr, shfs...) and partial
     downloads (.!qB, .part...). Everything is re-checked right before deleting.
  5. Busy Guard: refuses to run during Mover, parity check/rebuild or unBALANCE.
  6. Nobody:Users (99:100) 775 on new folders; empty source folders are pruned.
  7. XFS / BTRFS / ZFS: ZFS disks report pool capacity (child datasets included).
"""

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections import defaultdict

DISK_NAME_RE = re.compile(r"^disk\d+$")

LOCK_FILE = "/tmp/hardlink_balance.lock"
LOG_FILE = "/var/log/hardlink_balance.log"
STATUS_FILE = "/tmp/hardlink_balance_status.json"
PLAN_FILE = "/tmp/hardlink_balance_plan.json"
CFG_FILE = "/boot/config/plugins/hardlink-balance/hardlink-balance.cfg"
SHARES_CFG_DIR = "/boot/config/shares"      # per-share shareInclude / shareExclude
LOG_MAX_BYTES = 5 * 1024 * 1024             # /var/log is a small tmpfs: rotate to .1

# Defaults, overridable in CFG_FILE (KEY="value" lines)
CFG = {
    "WEBHOOK": "",                                   # optional Alertmanager-style webhook
    "RUNS_DIR": "/boot/config/plugins/hardlink-balance/runs",
    "MIN_AGE_MIN": "60",                             # skip folders touched in the last N minutes
}

# Shares/directories that should never be rebalanced automatically
DEFAULT_EXCLUDE_DIRS = {
    ".rebalance_stage",
    ".Trash",
    ".Recycle.Bin",
    "system",
    "appdata",
    "domains",
    "isos",
}

# Partial/in-progress download extensions
PARTIAL_EXTS = (".!qb", ".part", ".partial", ".tmp", ".crdownload", ".!ut", ".aria2")
TMP_SUFFIX = ".hlb-tmp"

# Minimum safety headroom on destination disks (10 GB)
MIN_FREE_HEADROOM_KB = 10 * 1024 * 1024

_STOP = False


def _handle_sigterm(signum, frame):
    global _STOP
    _STOP = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def pid_is_us(pid):
    """True if pid is alive AND is a hardlink_balance process (pids get reused)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"hardlink_balance" in f.read()
    except OSError:
        return False


def fsync_path(path, directory=False):
    """Flush a file (or a directory entry) to disk. Without it, a power cut right after
    removing the source could lose both copies (XFS keeps fresh data in RAM for seconds)."""
    fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def share_allowed_disks(share, all_disks):
    """Disks a share may live on, from Unraid's shareInclude / shareExclude (empty = all)."""
    vals = {}
    try:
        with open(os.path.join(SHARES_CFG_DIR, share + ".cfg")) as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"')
    except OSError:
        return set(all_disks)
    inc = {x.strip() for x in vals.get("shareInclude", "").split(",") if x.strip()}
    exc = {x.strip() for x in vals.get("shareExclude", "").split(",") if x.strip()}
    return {d for d in all_disks if (not inc or d in inc) and d not in exc}


def load_cfg():
    try:
        with open(CFG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                CFG[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass


def notify(summary, description, severity="info"):
    if not CFG["WEBHOOK"]:
        return
    payload = {
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "EquiLink",
                "severity": severity,
                "container": "hardlink_balance",
                "instance": os.uname().nodename,
            },
            "annotations": {
                "summary": summary,
                "description": description,
            },
        }]
    }
    try:
        subprocess.run(
            ["curl", "-fsS", "--max-time", "10",
             "-H", "Content-Type: application/json",
             "--data-binary", "@-", CFG["WEBHOOK"]],
            input=json.dumps(payload), capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        log(f"WARNING notify() failed: {e}")


def makedirs_nobody(d):
    """Como os.makedirs, pero las carpetas NUEVAS quedan nobody:users 775 (este script corre como root).
    Sin esto, qBittorrent (usuario nobody) no puede borrar luego lo que hay dentro: 'Permission denied'."""
    nuevas, x = [], d
    while x and not os.path.exists(x):
        nuevas.append(x)
        x = os.path.dirname(x)
    os.makedirs(d, exist_ok=True)
    for x in nuevas:
        try:
            os.chown(x, 99, 100)
            os.chmod(x, 0o775)
        except OSError:
            pass


def cleanup_empty_parents(path, mount):
    """
    Remove empty parent directories up to the mount point.
    os.rmdir only removes if directory is completely empty; stops at first non-empty directory.
    """
    curr = os.path.dirname(os.path.abspath(path))
    mount_abs = os.path.abspath(mount)
    while curr and curr != mount_abs and curr.startswith(mount_abs + os.sep):
        try:
            os.rmdir(curr)
            curr = os.path.dirname(curr)
        except OSError:
            break


def write_json_atomic(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fmt_kb(kb):
    if kb >= 1024 * 1024 * 1024:
        return f"{kb / (1024*1024*1024):.2f} TB"
    if kb >= 1024 * 1024:
        return f"{kb / (1024*1024):.2f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


# ── Activity guards ──────────────────────────────────────────────────────────

_OPEN_W = {"t": 0.0, "set": set()}


def open_for_write(max_age=15):
    """(dev, ino) of every file currently open for writing by any process.
    Covers shfs (FUSE layer behind /mnt/user), so container writes show up with
    the real /mnt/diskN inode."""
    if time.time() - _OPEN_W["t"] < max_age:
        return _OPEN_W["set"]
    s = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    flags = 0
                    for line in f:
                        if line.startswith("flags:"):
                            flags = int(line.split()[1], 8)
                            break
                if not flags & 3:          # O_WRONLY / O_RDWR
                    continue
                st = os.stat(f"{fd_dir}/{fd}")
                if stat.S_ISREG(st.st_mode):
                    s.add((st.st_dev, st.st_ino))
            except (OSError, ValueError):
                continue
    _OPEN_W["t"] = time.time()
    _OPEN_W["set"] = s
    return s


def array_busy():
    """Return a reason string if Mover / parity / unBALANCE is running, else ''."""
    try:
        with open("/var/local/emhttp/var.ini") as f:
            for line in f:
                if line.startswith("mdResyncPos="):
                    if line.split("=", 1)[1].strip().strip('"') not in ("", "0"):
                        return "parity check/rebuild in progress"
    except OSError:
        pass
    if os.path.exists("/var/run/mover.pid"):
        return "Mover is running"
    r = subprocess.run(["pgrep", "-f", "/usr/local/sbin/mover"], capture_output=True, text=True)
    if r.stdout.strip():
        return "Mover is running"
    # unBALANCE's daemon is always up; it is only busy while one of its rsyncs runs.
    # Same for any other tool copying between array disks.
    r = subprocess.run(["pgrep", "-x", "rsync"], capture_output=True, text=True)
    for pid in r.stdout.split():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(bytes([0]), b" ").decode(errors="replace")
            with open(f"/proc/{pid}/status") as f:
                ppid = next(int(l.split()[1]) for l in f if l.startswith("PPid:"))
        except (OSError, StopIteration):
            continue
        if ppid != os.getpid() and "/mnt/disk" in cmd:
            return "another rsync is moving data between array disks (unBALANCE?)"
    return ""


# ── Disks ────────────────────────────────────────────────────────────────────

def get_disk_fstype(disk_name):
    ini_path = "/var/local/emhttp/disks.ini"
    if not os.path.exists(ini_path):
        return "XFS"
    try:
        with open(ini_path) as f:
            in_section = False
            for line in f:
                line = line.strip()
                if line.startswith(f'["{disk_name}"]') or line.startswith(f'[{disk_name}]'):
                    in_section = True
                    continue
                if in_section and line.startswith("["):
                    break
                if in_section and line.startswith("fsType="):
                    return line.split("=", 1)[1].strip('"').upper()
    except Exception:
        pass
    return "XFS"


def disk_space_kb(name, mount, fstype):
    """(size_kb, free_kb). ZFS: whole pool (statvfs of the root dataset misses
    child datasets like /mnt/diskN/datos)."""
    if "ZFS" in fstype:
        r = subprocess.run(["zfs", "list", "-Hp", "-o", "used,available", name],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            used, avail = (int(x) for x in r.stdout.split()[:2])
            return (used + avail) // 1024, avail // 1024
    st = os.statvfs(mount)
    return (st.f_blocks * st.f_frsize) // 1024, (st.f_bavail * st.f_frsize) // 1024


def discover_disks(mnt_root="/mnt", exclude_disks=None):
    """Return dict: name -> {name, mount, fsType, size_kb, used_kb, free_kb, pct}"""
    if exclude_disks is None:
        exclude_disks = set()
    disks = {}
    for entry in sorted(os.listdir(mnt_root)):
        if not DISK_NAME_RE.match(entry):
            continue
        if entry in exclude_disks:
            continue
        mount = os.path.join(mnt_root, entry)
        if not os.path.ismount(mount):
            continue
        fstype = get_disk_fstype(entry)
        try:
            size_kb, free_kb = disk_space_kb(entry, mount, fstype)
        except (OSError, ValueError):
            continue
        if size_kb == 0:
            continue
        used_kb = size_kb - free_kb
        disks[entry] = {
            "name": entry,
            "mount": mount,
            "fsType": fstype,
            "size_kb": size_kb,
            "used_kb": used_kb,
            "free_kb": free_kb,
            "pct": used_kb * 100 // size_kb,
        }
    return disks


# ── Scan & plan ──────────────────────────────────────────────────────────────

def scan_disk_units(mount, min_size_kb, min_age_s, exclude_dirs=None):
    """
    Walk one disk and build movable folder units.
    A group = all links of one inode. A unit = all groups whose deepest link
    lives in the same folder (the library side: season / movie folder).
    Small files (nfo, srt...) travel with their folder; a unit is only kept if
    its biggest file reaches min_size_kb.
    Returns (units, skipped_counter). unit = {key, size_kb, newest, groups[]}.
    """
    if exclude_dirs is None:
        exclude_dirs = DEFAULT_EXCLUDE_DIRS

    groups = {}
    for root, dirs, files in os.walk(mount, followlinks=False):
        if root == mount:
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
        else:
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if name.endswith(TMP_SUFFIX):
                # Leftover from a crashed run (we hold the lock, nobody else uses this suffix)
                try:
                    os.remove(path)
                    log(f"  removed stale temp file {path}")
                except OSError:
                    pass
                continue
            key = (st.st_dev, st.st_ino)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "dev": st.st_dev, "ino": st.st_ino, "nlink": st.st_nlink,
                    "size": st.st_size, "size_kb": max(1, st.st_size // 1024),
                    "mtime": st.st_mtime, "ctime": st.st_ctime, "paths": [],
                }
            g["paths"].append(path)

    now = time.time()
    open_w = open_for_write(max_age=0)
    units = defaultdict(lambda: {"size_kb": 0, "max_kb": 0, "newest": 0, "groups": [], "bad": ""})
    for g in groups.values():
        g["paths"].sort(key=lambda p: (-p.count(os.sep), p))
        primary = g["paths"][0]
        u = units[os.path.dirname(primary)]
        u["groups"].append(g)
        u["size_kb"] += g["size_kb"]
        u["max_kb"] = max(u["max_kb"], g["size_kb"])
        u["newest"] = max(u["newest"], g["mtime"], g["ctime"])
        if u["bad"]:
            continue
        if g["nlink"] != len(g["paths"]):
            u["bad"] = "links_outside"      # some link lives in an excluded/hidden dir
        elif any(p.lower().endswith(PARTIAL_EXTS) for p in g["paths"]):
            u["bad"] = "partial"
        elif (g["dev"], g["ino"]) in open_w:
            u["bad"] = "open_write"
        elif now - max(g["mtime"], g["ctime"]) < min_age_s:
            u["bad"] = "recent"

    out, skipped = [], defaultdict(int)
    for key, u in units.items():
        if u["max_kb"] < min_size_kb:
            continue
        if u["bad"]:
            skipped[u["bad"]] += 1
            continue
        u["key"] = key
        out.append(u)
    # Oldest folders first: finished, untouched media is the safest to move
    out.sort(key=lambda u: u["newest"])
    return out, dict(skipped)


def clean_stale_temps(mount):
    """Remove .hlb-tmp leftovers of a crashed run anywhere on a disk (we hold the lock)."""
    for root, dirs, files in os.walk(mount):
        dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDE_DIRS]
        for name in files:
            if name.endswith(TMP_SUFFIX):
                try:
                    os.remove(os.path.join(root, name))
                    log(f"  removed stale temp file {os.path.join(root, name)}")
                except OSError:
                    pass


def build_plan(disks, tolerance, min_size_kb, min_age_s, limit_kb=None, run_start0=None):
    total_size = sum(d["size_kb"] for d in disks.values())
    total_used = sum(d["used_kb"] for d in disks.values())
    target_pct = (total_used * 100 // total_size) if total_size else 0
    log(f"Target: {target_pct}%  |  Tolerance: +/-{tolerance}%")

    sources = sorted(
        [name for name, d in disks.items() if d["pct"] > target_pct + tolerance],
        key=lambda n: -disks[n]["pct"],
    )
    if not sources:
        log("All disks within tolerance. Nothing to do.")
        return [], disks, target_pct

    vstate = {name: dict(d) for name, d in disks.items()}

    def pct_with(d, delta_kb):
        return (d["used_kb"] + delta_kb) * 100 // d["size_kb"]

    plan = []
    plan_total_kb = 0
    limit_hit = False
    share_cache = {}

    for src_idx, src in enumerate(sources):
        if limit_hit:
            break
        s = vstate[src]
        if s["pct"] <= target_pct + tolerance:
            continue
        need_kb = (s["pct"] - target_pct) * s["size_kb"] // 100
        log(f"Scanning {src} ({s['pct']}%, need to shed ~{fmt_kb(need_kb)})...")

        if run_start0:
            write_json_atomic(STATUS_FILE, {
                "state": "planning",
                "started_at": run_start0,
                "updated_at": time.time(),
                "target_pct": target_pct,
                "tolerance": tolerance,
                "current_disk": src,
                "source_idx": src_idx + 1,
                "source_total": len(sources),
                "disks": _build_disks_list(vstate, target_pct, tolerance),
                "plan_count": len(plan),
            })

        units, skipped = scan_disk_units(s["mount"], min_size_kb, min_age_s)
        log(f"  {src}: {len(units)} movable folders"
            + (f"; skipped {skipped}" if skipped else ""))

        for u in units:
            if s["pct"] <= target_pct + tolerance:
                break
            if limit_kb is not None and plan_total_kb >= limit_kb:
                limit_hit = True
                break
            size_kb = u["size_kb"]
            # Don't overshoot the source below the band
            if pct_with(s, -size_kb) < target_pct - tolerance:
                continue

            rels = [rel_to_mount(p, s["mount"]) for g in u["groups"] for p in g["paths"]]
            allowed = set(vstate)
            for share in {r.split(os.sep, 1)[0] for r in rels}:
                if share not in share_cache:
                    share_cache[share] = share_allowed_disks(share, vstate)
                allowed &= share_cache[share]
            best = None
            for dname, d in vstate.items():
                if dname == src or dname not in allowed:
                    continue                # never put a share on a disk it excludes
                if any(os.path.lexists(os.path.join(d["mount"], r)) for r in rels):
                    continue        # same path already on that disk: would collide
                if d["free_kb"] - size_kb < MIN_FREE_HEADROOM_KB:
                    continue
                if pct_with(d, size_kb) > target_pct + tolerance:
                    continue
                if best is None or d["pct"] < vstate[best]["pct"]:
                    best = dname
            if best is None:
                continue

            for g in u["groups"]:
                plan.append({
                    "src_disk": src,
                    "dst_disk": best,
                    "size_kb": g["size_kb"],
                    "paths": g["paths"],
                    "dev": g["dev"], "ino": g["ino"],
                    "size": g["size"], "mtime": g["mtime"],
                })
            plan_total_kb += size_kb

            for name, delta in ((src, -size_kb), (best, size_kb)):
                d = vstate[name]
                d["used_kb"] += delta
                d["free_kb"] -= delta
                d["pct"] = d["used_kb"] * 100 // d["size_kb"]

    return plan, disks, target_pct


def _build_disks_list(disks, target_pct, tolerance):
    out = []
    for name in sorted(disks):
        d = disks[name]
        role = "balanced"
        if d["pct"] > target_pct + tolerance:
            role = "source"
        elif d["pct"] < target_pct - tolerance:
            role = "destination"

        target_used_kb = (d["size_kb"] * target_pct) // 100
        delta_kb = d["used_kb"] - target_used_kb

        out.append({
            "name": name,
            "mount": d["mount"],
            "fsType": d.get("fsType", "XFS"),
            "size_kb": d["size_kb"],
            "used_kb": d["used_kb"],
            "free_kb": d["free_kb"],
            "pct": d["pct"],
            "role": role,
            "delta_kb": delta_kb,
        })
    return out


def summarize_plan(plan):
    total_kb = sum(p["size_kb"] for p in plan)
    multi = [p for p in plan if len(p["paths"]) > 1]
    multi_kb = sum(p["size_kb"] for p in multi)
    singles = len(plan) - len(multi)

    by_pair = defaultdict(lambda: [0, 0])
    for p in plan:
        by_pair[(p["src_disk"], p["dst_disk"])][0] += 1
        by_pair[(p["src_disk"], p["dst_disk"])][1] += p["size_kb"]

    log("=" * 70)
    log(f"PLAN: {len(plan)} groups, {fmt_kb(total_kb)} total")
    log(f"  {len(multi)} groups are hardlinked (>1 path) — {fmt_kb(multi_kb)}, links will be preserved")
    log(f"  {singles} groups are single files — moved normally")
    for (src, dst), (cnt, kb) in sorted(by_pair.items()):
        log(f"  {src} -> {dst}: {cnt} groups, {fmt_kb(kb)}")
    log("=" * 70)


def rel_to_mount(path, mount):
    return os.path.relpath(path, mount)


# ── Execute ──────────────────────────────────────────────────────────────────

def _rm(paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _source_unchanged(group, fresh=True):
    """Every source link still is the same inode, with the same link count,
    size and mtime, and nobody has it open for writing."""
    for p in group["paths"]:
        try:
            st = os.lstat(p)
        except OSError as e:
            return f"source vanished {p}: {e}"
        if (st.st_dev, st.st_ino) != (group["dev"], group["ino"]):
            return f"source replaced {p}"
        if st.st_nlink != len(group["paths"]):
            return f"link count changed ({st.st_nlink} != {len(group['paths'])}) {p}"
        if st.st_size != group["size"] or st.st_mtime != group["mtime"]:
            return f"source modified {p}"
    if (group["dev"], group["ino"]) in open_for_write(max_age=0 if fresh else 15):
        return f"source open for writing {group['paths'][0]}"
    return ""


def execute_group(group, disks):
    """
    Returns (status, rel_primary). status: "ok", "skipped" (nothing changed on
    disk) or "partial" (copy published but some source link could not be removed).
      1. Collision guard on every destination path.
      2. Re-check source (inode, nlink, size, mtime, not open for write).
      3. rsync to a hidden temp file (interruptible), verify size.
      4. Re-check source again, publish temp with a no-clobber link, recreate links.
      5. Remove originals, prune empty source folders.
    """
    src_mount = disks[group["src_disk"]]["mount"]
    dst_mount = disks[group["dst_disk"]]["mount"]
    paths = group["paths"]
    primary = paths[0]
    rel_primary = rel_to_mount(primary, src_mount)
    dst_all = [os.path.join(dst_mount, rel_to_mount(p, src_mount)) for p in paths]
    dst_primary = dst_all[0]

    for dp in dst_all:
        if os.path.lexists(dp):
            log(f"  SKIPPED collision: destination already exists: {dp}")
            return "skipped", rel_primary

    why = _source_unchanged(group, fresh=False)
    if why:
        log(f"  SKIPPED {why}")
        return "skipped", rel_primary

    makedirs_nobody(os.path.dirname(dst_primary))
    tmp = os.path.join(os.path.dirname(dst_primary), "." + os.path.basename(dst_primary) + TMP_SUFFIX)

    proc = subprocess.Popen(["rsync", "-aX", primary, tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    while proc.poll() is None:
        if _STOP:
            proc.terminate()
            proc.wait()
            _rm([tmp])
            log(f"  ABORTED copy of {rel_primary} (stop requested, source untouched)")
            return "skipped", rel_primary
        time.sleep(0.2)
    if proc.returncode != 0:
        _rm([tmp])
        log(f"  ERROR rsync {primary}: {proc.stderr.read().strip()}")
        return "skipped", rel_primary

    try:
        copied = os.path.getsize(tmp)
    except OSError as e:
        log(f"  ERROR verify {tmp}: {e}")
        return "skipped", rel_primary
    if copied != group["size"]:
        _rm([tmp])
        log(f"  ERROR size mismatch {tmp}: {copied} != {group['size']}")
        return "skipped", rel_primary

    try:
        fsync_path(tmp)
    except OSError as e:
        _rm([tmp])
        log(f"  ERROR fsync {tmp}: {e}")
        return "skipped", rel_primary

    why = _source_unchanged(group)
    if why:
        _rm([tmp])
        log(f"  SKIPPED {why} (during copy)")
        return "skipped", rel_primary

    made = []
    try:
        for dp in dst_all:
            makedirs_nobody(os.path.dirname(dp))
            os.link(tmp, dp)          # fails if dp appeared meanwhile: never overwrites
            made.append(dp)
    except OSError as e:
        _rm(made + [tmp])
        log(f"  ERROR linking {e.filename}: {e.strerror}")
        return "skipped", rel_primary
    _rm([tmp])
    try:
        for dd in {os.path.dirname(dp) for dp in dst_all}:
            fsync_path(dd, directory=True)   # new names are on disk before the source goes
    except OSError as e:
        log(f"  ERROR fsync dir: {e} — keeping the source, copy stays on {group['dst_disk']}")
        _rm(made)
        return "skipped", rel_primary

    failed_rm = []
    for p in paths:
        try:
            os.remove(p)
            cleanup_empty_parents(p, src_mount)
        except OSError as e:
            failed_rm.append(p)
            log(f"  ERROR could not remove original {p}: {e} — file now exists on both disks")

    tag = f"[{len(paths)} links]" if len(paths) > 1 else ""
    log(f"  MOVED {tag} {rel_primary} ({fmt_kb(group['size'] // 1024)}) "
        f"{group['src_disk']} -> {group['dst_disk']}")
    return ("partial" if failed_rm else "ok"), rel_primary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance", type=int, default=2, help="target +/- percent (default 2)")
    ap.add_argument("--min-size-kb", type=int, default=0, help="skip folders whose biggest file is smaller")
    ap.add_argument("--min-age-min", type=int, default=None, help="skip folders touched in the last N minutes")
    ap.add_argument("--limit-gb", type=float, default=None, help="cap total plan size (for a safe test run)")
    ap.add_argument("--exclude-disks", type=str, default="", help="comma-separated disk names to exclude (e.g. disk8)")
    ap.add_argument("--mnt-root", type=str, default="/mnt", help=argparse.SUPPRESS)  # tests
    ap.add_argument("--shares-cfg-dir", type=str, default=None, help=argparse.SUPPRESS)  # tests
    ap.add_argument("--headroom-mb", type=int, default=None, help=argparse.SUPPRESS)  # tests
    ap.add_argument("--execute", action="store_true", help="actually move data (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="dry-run only (default)")
    ap.add_argument("--stop", action="store_true", help="stop currently running instance")
    args = ap.parse_args()
    load_cfg()
    if args.shares_cfg_dir:
        global SHARES_CFG_DIR
        SHARES_CFG_DIR = args.shares_cfg_dir
    if args.headroom_mb is not None:
        global MIN_FREE_HEADROOM_KB
        MIN_FREE_HEADROOM_KB = args.headroom_mb * 1024

    if args.stop:
        if not os.path.exists(LOCK_FILE):
            print("Not running")
            sys.exit(0)
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to PID {pid}")
            sys.exit(0)
        except Exception as e:
            print(f"Failed to stop: {e}")
            sys.exit(1)

    limit_kb = int(args.limit_gb * 1024 * 1024) if args.limit_gb else None
    exclude_disks = {d.strip() for d in args.exclude_disks.split(",") if d.strip()}
    min_age_min = args.min_age_min if args.min_age_min is not None else int(CFG["MIN_AGE_MIN"])
    min_age_s = max(0, min_age_min) * 60

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            if pid_is_us(pid):
                log(f"Already running (PID {pid}), exiting.")
                sys.exit(1)
        except (OSError, ValueError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        run_start0 = time.time()
        busy = array_busy()
        if busy and args.execute:
            log(f"ABORT: {busy}. Try again when it finishes.")
            write_json_atomic(STATUS_FILE, {"state": "error", "msg": busy, "updated_at": time.time()})
            return
        if busy:
            log(f"WARNING: {busy} — the plan may change before you execute it.")

        disks = discover_disks(args.mnt_root, exclude_disks)
        log(f"Found {len(disks)} active data disks: {', '.join(sorted(disks))}")
        for name in sorted(disks):
            d = disks[name]
            log(f"  {name} ({d['fsType']}): {d['pct']}% used ({fmt_kb(d['used_kb'])} / {fmt_kb(d['size_kb'])})")
        log(f"Skipping folders touched in the last {min_age_min} min, open for writing or partial.")

        plan, disks, target_pct = build_plan(disks, args.tolerance, args.min_size_kb, min_age_s,
                                             limit_kb, run_start0)
        if not plan:
            write_json_atomic(STATUS_FILE, {
                "state": "completed",
                "dry_run": not args.execute,
                "tolerance": args.tolerance,
                "target_pct": target_pct,
                "disks": _build_disks_list(disks, target_pct, args.tolerance),
                "plan_count": 0,
                "plan": [],
                "stats": {
                    "files_total": 0, "files_done": 0, "files_skipped": 0,
                    "bytes_total_kb": 0, "bytes_done_kb": 0, "pct_done": 100,
                    "rate_kbs": 0, "eta_sec": 0, "multi_linked_groups": 0, "multi_linked_kb": 0,
                },
                "note": "nothing movable within tolerance",
                "updated_at": time.time(),
            })
            return

        summarize_plan(plan)

        multi0 = [g for g in plan if len(g["paths"]) > 1]
        multi_kb = sum(g["size_kb"] for g in multi0)
        total_kb = sum(g["size_kb"] for g in plan)

        plan_export = []
        for p in plan[:500]:
            src_m = disks[p["src_disk"]]["mount"]
            plan_export.append({
                "src_disk": p["src_disk"],
                "dst_disk": p["dst_disk"],
                "size_kb": p["size_kb"],
                "links": len(p["paths"]),
                "path": rel_to_mount(p["paths"][0], src_m),
            })
        write_json_atomic(PLAN_FILE, plan_export)

        write_json_atomic(STATUS_FILE, {
            "state": "planned",
            "started_at": run_start0,
            "updated_at": time.time(),
            "dry_run": not args.execute,
            "tolerance": args.tolerance,
            "target_pct": target_pct,
            "disks": _build_disks_list(disks, target_pct, args.tolerance),
            "plan_count": len(plan),
            "plan": plan_export[:100],
            "stats": {
                "files_total": len(plan),
                "files_done": 0,
                "files_skipped": 0,
                "bytes_total_kb": total_kb,
                "bytes_done_kb": 0,
                "pct_done": 0,
                "rate_kbs": 0,
                "eta_sec": 0,
                "multi_linked_groups": len(multi0),
                "multi_linked_kb": multi_kb,
            },
        })

        if not args.execute:
            log("DRY RUN — no data was moved. Re-run with --execute to apply this plan.")
            return

        for name in disks:                  # leftovers of a crash may sit on any disk
            clean_stale_temps(disks[name]["mount"])
        log("EXECUTING plan...")
        done = 0
        failed = 0
        partial = 0
        freed_kb = 0
        results = []
        run_start = time.time()
        stopped_early = False
        stop_reason = ""

        for i, group in enumerate(plan):
            if not _STOP and i % 25 == 0:
                stop_reason = array_busy()
            if _STOP or stop_reason:
                log(f"Halting: {stop_reason or 'stop signal received'} (in-flight copy cleaned, sources untouched).")
                stopped_early = True
                break

            t0 = time.time()
            status, rel_primary = execute_group(group, disks)
            if _STOP and status == "skipped":
                stopped_early = True
            elapsed = max(0.001, time.time() - t0)
            rate_mbs = (group["size_kb"] / 1024) / elapsed
            ok = status == "ok"

            results.append({
                "index": i,
                "src_disk": group["src_disk"],
                "dst_disk": group["dst_disk"],
                "size_kb": group["size_kb"],
                "n_paths": len(group["paths"]),
                "rel_path": rel_primary,
                "ok": ok,
                "status": status,
                "elapsed_s": round(elapsed, 2),
                "rate_mbs": round(rate_mbs, 1),
            })

            if ok:
                done += 1
                freed_kb += group["size_kb"]
            else:
                failed += 1
                if status == "partial":
                    partial += 1

            elapsed_total = time.time() - run_start
            overall_rate_mbs = (freed_kb / 1024) / elapsed_total if elapsed_total > 0 else 0
            remaining_kb = total_kb - freed_kb
            eta_sec = int(remaining_kb / 1024 / overall_rate_mbs) if overall_rate_mbs > 0 else 0

            write_json_atomic(STATUS_FILE, {
                "state": "running",
                "started_at": run_start,
                "updated_at": time.time(),
                "dry_run": False,
                "target_pct": target_pct,
                "tolerance": args.tolerance,
                "elapsed_sec": int(elapsed_total),
                "current_file": rel_primary,
                "disks": _build_disks_list(disks, target_pct, args.tolerance),
                "plan_count": len(plan),
                "plan": plan_export[:100],
                "stats": {
                    "files_total": len(plan),
                    "files_done": done,
                    "files_skipped": failed,
                    "bytes_total_kb": total_kb,
                    "bytes_done_kb": freed_kb,
                    "pct_done": round(freed_kb * 100 / total_kb, 1) if total_kb else 100,
                    "rate_kbs": int(overall_rate_mbs * 1024),
                    "eta_sec": eta_sec,
                    "multi_linked_groups": len(multi0),
                    "multi_linked_kb": multi_kb,
                },
                "current": {
                    "rel_path": rel_primary,
                    "src_disk": group["src_disk"],
                    "dst_disk": group["dst_disk"],
                    "n_paths": len(group["paths"]),
                    "rate_mbs": round(rate_mbs, 1),
                },
                "recent": results[-20:],
            })

            if (i + 1) % 50 == 0:
                log(f"  progress: {i+1}/{len(plan)} groups processed "
                    f"({fmt_kb(freed_kb)} / {fmt_kb(total_kb)}, {overall_rate_mbs:.0f} MB/s avg)")

        elapsed_total = time.time() - run_start
        state = "stopped" if stopped_early else "completed"

        log("=" * 70)
        log(f"{'STOPPED' if stopped_early else 'DONE'}. {done} groups moved ({fmt_kb(freed_kb)}), "
            f"{failed} failed/skipped ({partial} left on both disks), {elapsed_total/60:.1f} min elapsed.")
        log("=" * 70)

        disks_after = discover_disks(args.mnt_root, exclude_disks)
        write_json_atomic(STATUS_FILE, {
            "state": state,
            "started_at": run_start,
            "updated_at": time.time(),
            "dry_run": False,
            "tolerance": args.tolerance,
            "target_pct": target_pct,
            "elapsed_sec": int(elapsed_total),
            "disks": _build_disks_list(disks_after, target_pct, args.tolerance),
            "plan_count": len(plan),
            "plan": plan_export[:100],
            "stats": {
                "files_total": len(plan),
                "files_done": done,
                "files_skipped": failed,
                "bytes_total_kb": total_kb,
                "bytes_done_kb": freed_kb,
                "pct_done": round(freed_kb * 100 / total_kb, 1) if total_kb else 100,
                "rate_kbs": int(freed_kb / elapsed_total) if elapsed_total > 0 else 0,
                "eta_sec": 0,
                "multi_linked_groups": len(multi0),
                "multi_linked_kb": multi_kb,
            },
        })

        runs_dir = CFG["RUNS_DIR"]
        try:
            os.makedirs(runs_dir, exist_ok=True)
            run_id = time.strftime("%Y%m%d_%H%M%S", time.localtime(run_start))
            write_json_atomic(os.path.join(runs_dir, f"run_{run_id}.json"), {
                "run_id": run_id,
                "state": state,
                "tolerance": args.tolerance,
                "min_size_kb": args.min_size_kb,
                "min_age_min": min_age_min,
                "limit_gb": args.limit_gb,
                "target_pct": target_pct,
                "started_at": run_start,
                "finished_at": time.time(),
                "elapsed_s": round(elapsed_total, 1),
                "disks_before": disks,
                "plan_groups": len(plan),
                "plan_multi_linked_groups": len(multi0),
                "plan_multi_linked_kb": multi_kb,
                "plan_total_kb": total_kb,
                "groups_ok": done,
                "groups_failed": failed,
                "groups_partial": partial,
                "bytes_moved_kb": freed_kb,
                "results": results,
            })
            log(f"Run report saved: {runs_dir}/run_{run_id}.json")
        except OSError as e:
            log(f"WARNING could not save run report: {e}")

        after_lines = "\n".join(f"{n}: {disks_after[n]['pct']}%" for n in sorted(disks_after))
        if partial:
            notify(f"EquiLink: {partial} archivos quedaron en DOS discos",
                   f"No se pudo borrar el original tras copiarlo. Revisar {LOG_FILE} "
                   f"(líneas 'could not remove original').\nDiscos:\n{after_lines}",
                   severity="critical")
        elif failed > 0 or stopped_early:
            notify(
                f"EquiLink: {'parado' if stopped_early else 'terminado con saltos'} "
                f"({done} ok, {failed} saltados)",
                f"{fmt_kb(freed_kb)} movidos en {elapsed_total/60:.1f} min."
                + (f" Motivo: {stop_reason}." if stop_reason else "")
                + f"\nDiscos tras la pasada:\n{after_lines}\nDetalle en {LOG_FILE}.",
                severity="warning",
            )
        else:
            avg_mbs = (freed_kb / 1024) / elapsed_total if elapsed_total > 0 else 0
            notify(
                f"EquiLink: terminado OK ({done} grupos, {fmt_kb(freed_kb)})",
                f"{elapsed_total/60:.1f} min, {avg_mbs:.0f} MB/s promedio.\n"
                f"Discos tras la pasada:\n{after_lines}",
                severity="info",
            )
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
