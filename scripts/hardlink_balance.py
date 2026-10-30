#!/usr/bin/env python3
"""
hardlink_balance.py / EquiLink — Unraid array disk balancer with hardlink protection.

Features & Guarantees:
  1. Inode-Aware Grouping: groups by (st_dev, st_ino) on each source disk.
  2. Single-Pass Transfer: copies primary file via rsync, recreates secondary
     paths via native hardlinks (os.link) on destination.
  3. Collision Prevention: never overwrites files if destination paths already exist.
  4. Concurrent Modification Guard: detects if files are modified mid-transfer.
  5. Nobody:Users (99:100) 775 Permissions: prevents permission errors in qBittorrent/media servers.
  6. Empty Parent Directory Cleanup: automatically prunes empty folder trees on source disks.
  7. ZFS & XFS child dataset support: accurate capacity reporting for ZFS datasets.
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
RUNS_DIR = "/mnt/user/appdata/scripts/hardlink_balance_runs"

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

# Minimum safety headroom on destination disks (10 GB)
MIN_FREE_HEADROOM_KB = 10 * 1024 * 1024

_STOP = False


def _handle_sigterm(signum, frame):
    global _STOP
    _STOP = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


ALERT_WEBHOOK = "http://127.0.0.1:5678/webhook/alertas"


def notify(summary, description, severity="info"):
    payload = {
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "EquiLink",
                "severity": severity,
                "container": "hardlink_balance",
                "instance": "Unraid · Odin",
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
             "--data-binary", "@-", ALERT_WEBHOOK],
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
    while curr and curr != mount_abs and curr.startswith(mount_abs):
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


def _statvfs_path_for(mount):
    """
    Pick the path whose statvfs() actually reflects where the content lives.
    Some array disks (e.g. ZFS members) mount child datasets like
    /mnt/diskN/datos as SEPARATE filesystems from the disk root.
    """
    datos = os.path.join(mount, "datos")
    if os.path.ismount(datos) or os.path.isdir(datos):
        try:
            st = os.statvfs(datos)
            if (st.f_blocks * st.f_frsize) > 1024 * 1024 * 1024:
                return datos
        except OSError:
            pass
    return mount


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


def discover_disks(exclude_disks=None):
    """Return dict: name -> {name, mount, fsType, size_kb, used_kb, free_kb, pct}"""
    if exclude_disks is None:
        exclude_disks = set()
    disks = {}
    for entry in sorted(os.listdir("/mnt")):
        if not DISK_NAME_RE.match(entry):
            continue
        if entry in exclude_disks:
            continue
        mount = os.path.join("/mnt", entry)
        if not (os.path.ismount(mount) or os.path.isdir(mount)):
            continue
        try:
            st = os.statvfs(_statvfs_path_for(mount))
        except OSError:
            continue
        size_kb = (st.f_blocks * st.f_frsize) // 1024
        free_kb = (st.f_bavail * st.f_frsize) // 1024
        if size_kb == 0:
            continue
        used_kb = size_kb - free_kb
        pct = used_kb * 100 // size_kb
        disks[entry] = {
            "name": entry,
            "mount": mount,
            "fsType": get_disk_fstype(entry),
            "size_kb": size_kb,
            "used_kb": used_kb,
            "free_kb": free_kb,
            "pct": pct,
        }
    return disks


def scan_disk_groups(mount, min_size_kb, exclude_dirs=None):
    """
    Walk one disk mount and group regular files by (dev, ino).
    Returns dict: (dev, ino) -> {"size_kb": int, "paths": [abs paths]}
    Symlinks and non-regular files are skipped entirely.
    """
    if exclude_dirs is None:
        exclude_dirs = DEFAULT_EXCLUDE_DIRS

    groups = defaultdict(lambda: {"size_kb": 0, "paths": []})
    for root, dirs, files in os.walk(mount, followlinks=False):
        # Exclude system and hidden shares at the root of the mount
        if root == mount:
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
        else:
            dirs[:] = [d for d in dirs if not d.startswith(".rebalance_stage")]

        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            size_kb = max(1, st.st_size // 1024)
            if size_kb < min_size_kb:
                continue
            key = (st.st_dev, st.st_ino)
            g = groups[key]
            g["size_kb"] = size_kb
            g["paths"].append(path)
    return groups


def build_plan(disks, tolerance, min_size_kb, limit_kb=None, run_start0=None):
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

    plan = []
    plan_total_kb = 0
    limit_hit = False

    for src_idx, src in enumerate(sources):
        if limit_hit:
            break
        if vstate[src]["pct"] <= target_pct + tolerance:
            continue
        need_kb = (vstate[src]["pct"] - target_pct) * vstate[src]["size_kb"] // 100
        log(f"Scanning {src} ({vstate[src]['pct']}%, need to shed ~{fmt_kb(need_kb)})...")

        # Report scanning progress for WebUI
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

        groups = scan_disk_groups(vstate[src]["mount"], min_size_kb)
        log(f"  {src}: {len(groups)} hardlink-groups found "
            f"({sum(1 for g in groups.values() if len(g['paths']) > 1)} are multi-linked)")

        for key, g in groups.items():
            if vstate[src]["pct"] <= target_pct + tolerance:
                break
            if limit_kb is not None and plan_total_kb >= limit_kb:
                limit_hit = True
                break
            size_kb = g["size_kb"]

            best = None
            best_pct = None
            for dname, d in vstate.items():
                if dname == src:
                    continue
                if d["pct"] >= target_pct + tolerance:
                    continue
                # Ensure destination has space PLUS safety headroom
                if d["free_kb"] - size_kb < MIN_FREE_HEADROOM_KB:
                    continue
                new_pct = (d["used_kb"] + size_kb) * 100 // d["size_kb"]
                if new_pct > target_pct + tolerance:
                    continue
                if best is None or d["pct"] < best_pct:
                    best = dname
                    best_pct = d["pct"]

            if best is None:
                continue

            plan.append({
                "src_disk": src,
                "dst_disk": best,
                "size_kb": size_kb,
                "paths": g["paths"],
            })
            plan_total_kb += size_kb

            vstate[src]["used_kb"] -= size_kb
            vstate[src]["free_kb"] += size_kb
            vstate[src]["pct"] = vstate[src]["used_kb"] * 100 // vstate[src]["size_kb"]
            vstate[best]["used_kb"] += size_kb
            vstate[best]["free_kb"] -= size_kb
            vstate[best]["pct"] = vstate[best]["used_kb"] * 100 // vstate[best]["size_kb"]

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


def execute_group(group, disks):
    """
    Executes a move for an entire hardlink group:
      1. Verifies no collision exists at destination.
      2. Records source stat (size, mtime) to detect concurrent modifications.
      3. Rsyncs the primary file.
      4. Verifies size and checks that source wasn't modified mid-copy.
      5. Recreates hardlinks for all other group members on destination disk.
      6. Verifies all links and removes originals.
      7. Prunes empty parent directories on source disk.
    """
    src_mount = disks[group["src_disk"]]["mount"]
    dst_mount = disks[group["dst_disk"]]["mount"]
    paths = group["paths"]
    primary = paths[0]

    rel_primary = rel_to_mount(primary, src_mount)
    dst_primary = os.path.join(dst_mount, rel_primary)

    # Collision guard
    if os.path.exists(dst_primary):
        log(f"  SKIPPED collision: destination file already exists: {dst_primary}")
        return False, None

    for p in paths[1:]:
        rel = rel_to_mount(p, src_mount)
        dp = os.path.join(dst_mount, rel)
        if os.path.exists(dp):
            log(f"  SKIPPED collision: destination link already exists: {dp}")
            return False, None

    makedirs_nobody(os.path.dirname(dst_primary))

    try:
        orig_stat = os.stat(primary)
        orig_size = orig_stat.st_size
        orig_mtime = orig_stat.st_mtime
    except OSError as e:
        log(f"  ERROR stat {primary}: {e}")
        return False, None

    r = subprocess.run(
        ["rsync", "-aX", primary, dst_primary],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log(f"  ERROR rsync {primary} -> {dst_primary}: {r.stderr.strip()}")
        return False, None

    try:
        copied_size = os.path.getsize(dst_primary)
    except OSError as e:
        log(f"  ERROR verify {dst_primary}: {e}")
        return False, None

    if copied_size != orig_size:
        log(f"  ERROR size mismatch {dst_primary}: {copied_size} != {orig_size}")
        try:
            os.remove(dst_primary)
        except OSError:
            pass
        return False, None

    # Concurrent modification check
    try:
        check_stat = os.stat(primary)
        if check_stat.st_size != orig_size or check_stat.st_mtime != orig_mtime:
            log(f"  SKIPPED concurrent modification: file was modified mid-transfer: {primary}")
            try:
                os.remove(dst_primary)
            except OSError:
                pass
            return False, None
    except OSError:
        pass

    dst_paths = [dst_primary]
    ok = True
    for p in paths[1:]:
        rel = rel_to_mount(p, src_mount)
        dst_p = os.path.join(dst_mount, rel)
        makedirs_nobody(os.path.dirname(dst_p))
        try:
            if os.path.exists(dst_p):
                os.remove(dst_p)
            os.link(dst_primary, dst_p)
            dst_paths.append(dst_p)
        except OSError as e:
            log(f"  ERROR hardlink {dst_p}: {e}")
            ok = False
            break

    if not ok:
        for dp in dst_paths:
            try:
                os.remove(dp)
            except OSError:
                pass
        return False, None

    # Everything verified — remove originals and prune empty directories
    for p in paths:
        try:
            os.remove(p)
            cleanup_empty_parents(p, src_mount)
        except OSError as e:
            log(f"  WARNING could not remove original {p}: {e}")

    tag = f"[{len(paths)} links]" if len(paths) > 1 else ""
    log(f"  MOVED {tag} {rel_primary} ({fmt_kb(orig_size // 1024)}) "
        f"{group['src_disk']} -> {group['dst_disk']}")
    return True, rel_primary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance", type=int, default=2, help="target +/- percent (default 2)")
    ap.add_argument("--min-size-kb", type=int, default=0, help="skip files smaller than this")
    ap.add_argument("--limit-gb", type=float, default=None, help="cap total plan size (for a safe test run)")
    ap.add_argument("--exclude-disks", type=str, default="", help="comma-separated disk names to exclude (e.g. disk8)")
    ap.add_argument("--execute", action="store_true", help="actually move data (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="dry-run only (default)")
    ap.add_argument("--stop", action="store_true", help="stop currently running instance")
    args = ap.parse_args()

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

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            log(f"Already running (PID {pid}), exiting.")
            sys.exit(1)
        except (OSError, ValueError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        run_start0 = time.time()
        disks = discover_disks(exclude_disks)
        log(f"Found {len(disks)} active data disks: {', '.join(sorted(disks))}")
        for name in sorted(disks):
            d = disks[name]
            log(f"  {name} ({d['fsType']}): {d['pct']}% used ({fmt_kb(d['used_kb'])} / {fmt_kb(d['size_kb'])})")

        plan, disks, target_pct = build_plan(disks, args.tolerance, args.min_size_kb, limit_kb, run_start0)
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
                "note": "all disks within tolerance",
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

        planned_payload = {
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
        }
        write_json_atomic(STATUS_FILE, planned_payload)

        if not args.execute:
            log("DRY RUN — no data was moved. Re-run with --execute to apply this plan.")
            return

        log("EXECUTING plan...")
        done = 0
        failed = 0
        freed_kb = 0
        results = []
        run_start = time.time()
        stopped_early = False

        for i, group in enumerate(plan):
            if _STOP:
                log("Received stop signal — halting before next group (nothing mid-flight touched).")
                stopped_early = True
                break

            t0 = time.time()
            ok, rel_primary = execute_group(group, disks)
            elapsed = max(0.001, time.time() - t0)
            rate_mbs = (group["size_kb"] / 1024) / elapsed

            results.append({
                "index": i,
                "src_disk": group["src_disk"],
                "dst_disk": group["dst_disk"],
                "size_kb": group["size_kb"],
                "n_paths": len(group["paths"]),
                "rel_path": rel_primary,
                "ok": ok,
                "elapsed_s": round(elapsed, 2),
                "rate_mbs": round(rate_mbs, 1),
            })

            if ok:
                done += 1
                freed_kb += group["size_kb"]
            else:
                failed += 1

            done_kb = sum(r["size_kb"] for r in results if r["ok"])
            elapsed_total = time.time() - run_start
            overall_rate_mbs = (done_kb / 1024) / elapsed_total if elapsed_total > 0 else 0
            remaining_kb = total_kb - done_kb
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
                    "bytes_done_kb": done_kb,
                    "pct_done": round(done_kb * 100 / total_kb, 1) if total_kb else 100,
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
                    f"({fmt_kb(done_kb)} / {fmt_kb(total_kb)}, {overall_rate_mbs:.0f} MB/s avg)")

        elapsed_total = time.time() - run_start
        state = "stopped" if stopped_early else "completed"

        log("=" * 70)
        log(f"{'STOPPED' if stopped_early else 'DONE'}. {done} groups moved ({fmt_kb(freed_kb)}), "
            f"{failed} failed/skipped, {elapsed_total/60:.1f} min elapsed.")
        log("=" * 70)

        write_json_atomic(STATUS_FILE, {
            "state": state,
            "started_at": run_start,
            "updated_at": time.time(),
            "dry_run": False,
            "tolerance": args.tolerance,
            "target_pct": target_pct,
            "elapsed_sec": int(elapsed_total),
            "disks": _build_disks_list(discover_disks(exclude_disks), target_pct, args.tolerance),
            "plan_count": len(plan),
            "plan": plan_export[:100],
            "stats": {
                "files_total": len(plan),
                "files_done": done,
                "files_skipped": failed,
                "bytes_total_kb": total_kb,
                "bytes_done_kb": freed_kb,
                "pct_done": round(freed_kb * 100 / total_kb, 1) if total_kb else 100,
                "rate_kbs": int(((freed_kb / elapsed_total)) if elapsed_total > 0 else 0),
                "eta_sec": 0,
                "multi_linked_groups": len(multi0),
                "multi_linked_kb": multi_kb,
            },
        })

        try:
            os.makedirs(RUNS_DIR, exist_ok=True)
            run_id = time.strftime("%Y%m%d_%H%M%S", time.localtime(run_start))
            multi = [g for g in plan if len(g["paths"]) > 1]
            write_json_atomic(os.path.join(RUNS_DIR, f"run_{run_id}.json"), {
                "run_id": run_id,
                "state": state,
                "tolerance": args.tolerance,
                "min_size_kb": args.min_size_kb,
                "limit_gb": args.limit_gb,
                "target_pct": target_pct,
                "started_at": run_start,
                "finished_at": time.time(),
                "elapsed_s": round(elapsed_total, 1),
                "disks_before": disks,
                "plan_groups": len(plan),
                "plan_multi_linked_groups": len(multi),
                "plan_multi_linked_kb": sum(g["size_kb"] for g in multi),
                "plan_total_kb": total_kb,
                "groups_ok": done,
                "groups_failed": failed,
                "bytes_moved_kb": freed_kb,
                "results": results,
            })
            log(f"Run report saved: {RUNS_DIR}/run_{run_id}.json")
        except OSError as e:
            log(f"WARNING could not save run report: {e}")

        if args.execute:
            disks_after = discover_disks(exclude_disks)
            after_lines = "\n".join(
                f"{n}: {disks_after[n]['pct']}%" for n in sorted(disks_after)
            )
            if failed > 0 or stopped_early:
                notify(
                    f"EquiLink: {'parado' if stopped_early else 'terminado con fallos'} "
                    f"({done} ok, {failed} fallidos)",
                    f"{fmt_kb(freed_kb)} movidos en {elapsed_total/60:.1f} min.\n"
                    f"Discos tras la pasada:\n{after_lines}\n"
                    f"Revisar {LOG_FILE} para el detalle de los fallos.",
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
