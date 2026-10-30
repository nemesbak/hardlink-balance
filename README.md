# Hardlink Balance for Unraid ⚖️🔗

**The Unraid array disk-usage balancer that never breaks hardlinks.**

[![Unraid Version](https://img.shields.io/badge/Unraid-6.12%2B%20%7C%207.x-orange.svg)](https://unraid.net)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## The Problem with Traditional Balancers

If you use **qBittorrent** alongside media managers like **Radarr**, **Sonarr**, **Plex**, or **Jellyfin**, your files almost certainly share hardlinks. Hardlinks allow a downloaded video file in your torrent folder to exist simultaneously in your media library without using double disk space.

Existing Unraid plugins (such as *unBALANCE* or *ReBalance*) move files one-by-one with standard `rsync`:
* **They silently break every hardlinked pair across disks.**
* When a movie or episode is moved, its hardlink to the active torrent is severed, resulting in **two distinct copies** on different drives.
* Space usage doubles, and when qBittorrent attempts to manage or clean up completed torrents, it runs into `Permission denied` or orphan files.

---

## How Hardlink Balance Solves It

**Hardlink Balance** treats hardlinks as first-class citizens:

1. **Inode-Aware Grouping**: Discovers files on each source disk and groups them strictly by physical inode `(st_dev, st_ino)`. Files sharing an inode are bound to a single physical drive and are always planned as a single unit.
2. **Atomic Inode Transfer**: Copies **one** file of the group to the destination disk using `rsync -aX`. Once verified, it recreates every other member of the group on the destination using native `os.link`.
3. **Safe Two-Phase Deletion**: Only after every link is successfully verified at the destination does it remove the originals from the source drive. If anything fails, changes are cleanly rolled back.
4. **Nobody:Users Ownership**: Ensures newly created directories and files maintain `nobody:users` (99:100) with `775` permissions so qBittorrent and media servers never suffer permission issues.
5. **Interactive WebGUI**: Sits right inside Unraid's **Tools (Herramientas)** menu with real-time capacity bars, live progress, transfer speeds, ETA, and a dedicated **Hardlinks Preserved** counter.
6. **ZFS & XFS Support**: Fully compatible with mixed XFS arrays and ZFS dataset child mounts (e.g. `/mnt/diskN/datos`).

---

## Installation

### Via Unraid WebGUI:
1. In your Unraid WebGUI, navigate to **Plugins** → **Install Plugin**.
2. Paste the following URL:
   ```text
   https://raw.githubusercontent.com/nemesbak/hardlink-balance/main/hardlink_balance.plg
   ```
3. Click **Install**.
4. The plugin will appear under **Tools** → **Hardlink Balance**.

---

## WebGUI Features

* **Real-time Array Visualization**: View every array disk with colored badges (*Source*, *Destination*, *Balanced*) and a target utilization marker line.
* **Analysis Mode (Dry Run)**: Calculates the target percentage and provides a preview table of all proposed transfers without moving any data.
* **Tolerance Slider**: Configure acceptable utilization variance (±1% to ±10%, default ±2%).
* **Min File Size Filter**: Skip tiny metadata files (NFOs, subtitles) and focus rebalancing on large media files.
* **Batch Transfer Cap**: Set an optional GB limit (e.g. 50 GB) for safe phased runs.
* **Live Execution Console**: Real-time terminal output with automatic scrolling and graceful **Stop** controls.

---

## CLI Usage (Optional)

You can also run the balancer directly from the Unraid terminal:

```bash
# Dry run simulation (safe, does not move anything)
python3 /usr/local/emhttp/plugins/hardlink-balance/scripts/hardlink_balance.py --dry-run

# Execute balancing with a 50 GB safety cap
python3 /usr/local/emhttp/plugins/hardlink-balance/scripts/hardlink_balance.py --execute --limit-gb 50

# Full balancing with 2% tolerance
python3 /usr/local/emhttp/plugins/hardlink-balance/scripts/hardlink_balance.py --execute --tolerance 2
```

---

## License

MIT License. Designed and maintained for Unraid storage systems.
