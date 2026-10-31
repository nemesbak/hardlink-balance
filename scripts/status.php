<?php
/**
 * EquiLink (hardlink-balance) — AJAX status, settings & control endpoint
 * Accessible at: /plugins/hardlink-balance/scripts/status.php
 */

ob_start();
ini_set('display_errors', 0);
error_reporting(0);

header('Content-Type: application/json');
header('Cache-Control: no-store, no-cache');

define('STATUS_FILE', '/tmp/hardlink_balance_status.json');
define('PLAN_FILE',   '/tmp/hardlink_balance_plan.json');
define('LOCK_FILE',   '/tmp/hardlink_balance.lock');
define('LOG_FILE',    '/var/log/hardlink_balance.log');
define('SCRIPT',      '/usr/local/emhttp/plugins/hardlink-balance/scripts/hardlink_balance.py');
define('BOOT_DIR',    '/boot/config/plugins/hardlink-balance');
define('CFG_FILE',    BOOT_DIR . '/hardlink-balance.cfg');
define('CRON_FILE',   BOOT_DIR . '/hardlink-balance.cron');
define('GUARD_CACHE', '/tmp/hardlink_balance_guards.json');
define('HIST_CACHE',  '/tmp/hardlink_balance_history.json');

// Shares that hold the OS, VMs or containers: never offered by default
const SYSTEM_SHARES = ['system', 'appdata', 'domains', 'isos'];

const DEFAULTS = [
    'WEBHOOK' => '', 'UNRAID_NOTIFY' => 'yes', 'RUNS_DIR' => BOOT_DIR . '/runs', 'MIN_AGE_MIN' => '60',
    'SHARES' => '', 'EXCLUDE_DISKS' => '', 'TOLERANCE' => '2', 'MIN_SIZE_MB' => '100', 'LIMIT_GB' => '0',
    'HEADROOM_GB' => '10', 'PARALLEL' => '1', 'LANGUAGE' => 'auto', 'SCHEDULE' => 'off', 'SCHEDULE_HOUR' => '3', 'SCHEDULE_DOW' => '0',
];

function read_cfg(): array {
    $cfg = DEFAULTS;
    foreach (@file(CFG_FILE, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#' || strpos($line, '=') === false) continue;
        [$k, $v] = explode('=', $line, 2);
        $cfg[trim($k)] = trim(trim($v), "\"'");
    }
    return $cfg;
}

function write_cfg(array $cfg): bool {
    $notes = [
        'WEBHOOK' => 'Alertmanager-style webhook for end-of-run alerts (empty = off)',
        'UNRAID_NOTIFY' => 'End-of-run alert in the Unraid notification bell (yes/no)',
        'RUNS_DIR' => 'Where run reports are stored',
        'MIN_AGE_MIN' => 'Skip folders modified or hardlinked in the last N minutes',
        'SHARES' => 'Top-level shares to balance, comma separated (empty = all except system shares)',
        'EXCLUDE_DISKS' => 'Disks never used as source or destination, comma separated',
        'TOLERANCE' => 'Allowed distance from the target fill, in percent (1-10)',
        'MIN_SIZE_MB' => 'Ignore folders whose biggest file is smaller',
        'LIMIT_GB' => 'Maximum data moved per run (0 = no cap)',
        'HEADROOM_GB' => 'Free space always left on a destination disk',
        'PARALLEL' => 'Copies at once (1-4); each disk takes part in one copy at a time',
        'LANGUAGE' => 'WebGUI language: auto (same as Unraid), en or es',
        'SCHEDULE' => 'Automatic runs: off, daily, weekly or monthly',
        'SCHEDULE_HOUR' => 'Hour of the automatic run (0-23)',
        'SCHEDULE_DOW' => 'Day of the week for weekly runs (0 = Sunday)',
    ];
    $out = '';
    foreach ($cfg as $k => $v) {
        if (isset($notes[$k])) $out .= '# ' . $notes[$k] . "\n";
        $out .= $k . '="' . str_replace(['"', "\n", "\r"], '', (string)$v) . "\"\n";
    }
    @mkdir(BOOT_DIR, 0755, true);
    $tmp = CFG_FILE . '.tmp';
    return @file_put_contents($tmp, $out) !== false && @rename($tmp, CFG_FILE);
}

function apply_schedule(array $cfg): void {
    $h = max(0, min(23, (int)$cfg['SCHEDULE_HOUR']));
    $dow = max(0, min(6, (int)$cfg['SCHEDULE_DOW']));
    $when = ['daily' => "0 $h * * *", 'weekly' => "0 $h * * $dow", 'monthly' => "0 $h 1 * *"][$cfg['SCHEDULE']] ?? '';
    if ($when === '') {
        @unlink(CRON_FILE);
    } else {
        @file_put_contents(CRON_FILE, "# EquiLink automatic run (managed from the WebGUI)\n"
            . "$when HLB_TRIGGER=scheduled /usr/bin/python3 " . SCRIPT . " --execute >/dev/null 2>&1\n");
    }
    @exec('/usr/local/sbin/update_cron 2>/dev/null');
}

function pid_alive(int $pid): bool {
    if ($pid <= 0) return false;
    $cmd = @file_get_contents("/proc/$pid/cmdline");
    return $cmd !== false && strpos($cmd, 'hardlink_balance') !== false;
}

function is_running(): bool {
    return file_exists(LOCK_FILE) && pid_alive((int)@file_get_contents(LOCK_FILE));
}

/** [total, free] in bytes. ZFS: whole pool, so child datasets (/mnt/diskN/share) count. */
function disk_space(string $name, string $mount, string $fsType): array {
    if (stripos($fsType, 'zfs') !== false) {
        $out = trim((string)@shell_exec('zfs list -Hp -o used,available ' . escapeshellarg($name) . ' 2>/dev/null'));
        $p = preg_split('/\s+/', $out);
        if (count($p) >= 2 && is_numeric($p[0]) && is_numeric($p[1])) {
            return [(float)$p[0] + (float)$p[1], (float)$p[1]];
        }
    }
    return [@disk_total_space($mount), @disk_free_space($mount)];
}

function get_disk_meta(): array {
    $meta = [];
    foreach (@parse_ini_file('/var/local/emhttp/disks.ini', true) ?: [] as $sec => $d) {
        $name = trim($sec, '[]"');
        if (strpos($name, 'disk') !== 0) continue;
        $meta[$name] = [
            'fsType' => strtoupper($d['fsType'] ?? 'XFS'),
            'temp'   => (isset($d['temp']) && $d['temp'] !== '*' && $d['temp'] !== '') ? $d['temp'] . ' °C' : '',
            'spun_down' => ($d['spundown'] ?? '0') === '1',
        ];
    }
    return $meta;
}

function live_disk_stats(int $tolerance, array $exclude): array {
    $mounts = array_filter(glob('/mnt/disk[0-9]*') ?: [], fn($m) => preg_match('#^/mnt/disk\d+$#', $m));
    natsort($mounts);
    $meta = get_disk_meta();
    $raw = []; $total_size = 0; $total_used = 0;
    foreach ($mounts as $mount) {
        if (!is_dir($mount)) continue;
        $name = basename($mount);
        $m = $meta[$name] ?? ['fsType' => 'XFS', 'temp' => '', 'spun_down' => false];
        [$total, $free] = disk_space($name, $mount, $m['fsType']);
        if (!$total) continue;
        $used = $total - $free;
        $excluded = in_array($name, $exclude, true);
        if (!$excluded) { $total_size += $total; $total_used += $used; }
        $raw[] = [
            'name' => $name, 'mount' => $mount, 'fsType' => $m['fsType'], 'temp' => $m['temp'],
            'spun_down' => $m['spun_down'], 'excluded' => $excluded,
            'size_kb' => (int)($total / 1024), 'used_kb' => (int)($used / 1024), 'free_kb' => (int)($free / 1024),
            'pct' => (int)round($used * 100 / $total), 'role' => 'balanced', 'delta_kb' => 0,
        ];
    }
    $target = $total_size > 0 ? (int)round($total_used * 100 / $total_size) : 0;
    foreach ($raw as &$d) {
        $d['delta_kb'] = $d['used_kb'] - (int)($d['size_kb'] * $target / 100);
        if ($d['excluded']) $d['role'] = 'excluded';
        elseif ($d['pct'] > $target + $tolerance) $d['role'] = 'source';
        elseif ($d['pct'] < $target - $tolerance) $d['role'] = 'destination';
    }
    unset($d);
    return ['disks' => array_values($raw), 'target_pct' => $target];
}

function glob_escape(string $s): string {
    return preg_replace('/([*?\[\]])/', '[$1]', $s);
}

function list_shares(): array {
    $out = [];
    foreach (glob('/mnt/user/*', GLOB_ONLYDIR) ?: [] as $p) {
        $n = basename($p);
        if ($n[0] === '.' || !glob('/mnt/disk[0-9]*/' . glob_escape($n), GLOB_ONLYDIR)) continue;   // pool-only shares never live on the array
        $out[] = ['name' => $n, 'system' => in_array($n, SYSTEM_SHARES, true)];
    }
    return $out;
}

/** Safety checks from the engine itself, cached a few seconds (the page polls every 2 s). */
function get_guards(): array {
    $c = @json_decode(@file_get_contents(GUARD_CACHE), true);
    if (is_array($c) && time() - ($c['at'] ?? 0) < 10) return $c;
    $g = @json_decode((string)@shell_exec('python3 ' . escapeshellarg(SCRIPT) . ' --guards 2>/dev/null'), true) ?: [];
    $g['at'] = time();
    @file_put_contents(GUARD_CACHE, json_encode($g));
    return $g;
}

/** Last run reports, summarised; cached until the runs folder changes. */
function get_history(string $dir, int $max = 8): array {
    $files = glob(rtrim($dir, '/') . '/run_*.json') ?: [];
    rsort($files);
    $files = array_slice($files, 0, $max);
    $sig = md5(implode('|', array_map(fn($f) => $f . @filemtime($f), $files)));
    $c = @json_decode(@file_get_contents(HIST_CACHE), true);
    if (is_array($c) && ($c['sig'] ?? '') === $sig) return $c['runs'];
    $runs = [];
    foreach ($files as $f) {
        $r = @json_decode(@file_get_contents($f), true);
        if (!is_array($r)) continue;
        $runs[] = [
            'started_at' => $r['started_at'] ?? @filemtime($f), 'state' => $r['state'] ?? '?',
            'trigger' => $r['trigger'] ?? 'manual', 'tolerance' => $r['tolerance'] ?? null,
            'moved_kb' => $r['bytes_moved_kb'] ?? 0, 'ok' => $r['groups_ok'] ?? 0,
            'failed' => $r['groups_failed'] ?? 0, 'partial' => $r['groups_partial'] ?? 0,
            'elapsed_s' => $r['elapsed_s'] ?? 0, 'out_after' => $r['out_of_band_after'] ?? null,
        ];
    }
    @file_put_contents(HIST_CACHE, json_encode(['sig' => $sig, 'runs' => $runs]));
    return $runs;
}

function get_recent_log(int $lines = 80): array {
    $data = @file(LOG_FILE, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    return $data ? array_values(array_slice($data, -$lines)) : [];
}

function get_plan_items(int $max = 500): array {
    $data = @json_decode(@file_get_contents(PLAN_FILE), true);
    return is_array($data) ? array_slice($data, 0, $max) : [];
}

function csv_list(string $v): array {
    return array_values(array_filter(array_map('trim', explode(',', $v)), 'strlen'));
}

function launch_script(string $cmd): void {
    exec('setsid nohup ' . $cmd . ' </dev/null >/dev/null 2>&1 &');
}

function json_out($data): void {
    ob_clean();
    echo json_encode($data);
}

try {

$cfg = read_cfg();
$exclude = csv_list($cfg['EXCLUDE_DISKS']);
$var = @parse_ini_file('/var/local/emhttp/var.ini') ?: [];
$csrf_expected = $var['csrf_token'] ?? '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if ($csrf_expected === '' || !hash_equals($csrf_expected, (string)($_POST['csrf_token'] ?? ''))) {
        json_out(['success' => false, 'msg' => 'Invalid CSRF token']);
        exit;
    }
    $action = $_POST['action'] ?? '';

    if ($action === 'start') {
        if (is_running()) { json_out(['success' => false, 'msg' => 'A run is already in progress']); exit; }
        $tolerance   = max(1, min(10, (int)($_POST['tolerance'] ?? $cfg['TOLERANCE'])));
        $min_file_kb = max(0, (int)($_POST['min_file_kb'] ?? ((float)$cfg['MIN_SIZE_MB'] * 1024)));
        $limit_gb    = max(0, (float)($_POST['limit_gb'] ?? $cfg['LIMIT_GB']));
        $args = '--tolerance ' . escapeshellarg((string)$tolerance)
              . (!empty($_POST['dry_run']) ? ' --dry-run' : ' --execute')
              . ' --min-size-kb ' . escapeshellarg((string)$min_file_kb);
        if ($limit_gb > 0) $args .= ' --limit-gb ' . escapeshellarg((string)$limit_gb);
        @unlink(STATUS_FILE);
        launch_script('HLB_TRIGGER=manual python3 ' . escapeshellarg(SCRIPT) . ' ' . $args);
        usleep(300000);
        json_out(['success' => true]);
        exit;
    }

    if ($action === 'stop') {
        exec('python3 ' . escapeshellarg(SCRIPT) . ' --stop');
        json_out(['success' => true, 'msg' => 'Stop signal sent']);
        exit;
    }

    if ($action === 'clear') {
        @unlink(STATUS_FILE);
        @unlink(PLAN_FILE);
        json_out(['success' => true]);
        exit;
    }

    if ($action === 'save_settings') {
        $known_disks = array_map(fn($d) => $d['name'], live_disk_stats(2, [])['disks']);
        $known_shares = array_map(fn($s) => $s['name'], list_shares());
        $shares = array_values(array_intersect(csv_list($_POST['shares'] ?? ''), $known_shares));
        $disks  = array_values(array_intersect(csv_list($_POST['exclude_disks'] ?? ''), $known_disks));
        $hook = trim((string)($_POST['webhook'] ?? ''));
        if ($hook !== '' && !preg_match('#^https?://[^\s"\']+$#', $hook)) {
            json_out(['success' => false, 'msg' => 'The webhook must be an http(s) URL']);
            exit;
        }
        $sched = in_array($_POST['schedule'] ?? 'off', ['off', 'daily', 'weekly', 'monthly'], true) ? $_POST['schedule'] : 'off';
        $cfg = array_merge($cfg, [
            'SHARES'        => implode(',', $shares),
            'EXCLUDE_DISKS' => implode(',', $disks),
            'TOLERANCE'     => (string)max(1, min(10, (int)($_POST['tolerance'] ?? 2))),
            'MIN_SIZE_MB'   => (string)max(0, (int)($_POST['min_size_mb'] ?? 100)),
            'LIMIT_GB'      => (string)max(0, (int)($_POST['limit_gb'] ?? 0)),
            'MIN_AGE_MIN'   => (string)max(0, min(10080, (int)($_POST['min_age_min'] ?? 60))),
            'HEADROOM_GB'   => (string)max(1, min(1000, (int)($_POST['headroom_gb'] ?? 10))),
            'UNRAID_NOTIFY' => !empty($_POST['unraid_notify']) ? 'yes' : 'no',
            'WEBHOOK'       => $hook,
            'PARALLEL'      => (string)max(1, min(4, (int)($_POST['parallel'] ?? 1))),
            'LANGUAGE'      => in_array($_POST['language'] ?? 'auto', ['auto', 'en', 'es'], true) ? $_POST['language'] : 'auto',
            'SCHEDULE'      => $sched,
            'SCHEDULE_HOUR' => (string)max(0, min(23, (int)($_POST['schedule_hour'] ?? 3))),
            'SCHEDULE_DOW'  => (string)max(0, min(6, (int)($_POST['schedule_dow'] ?? 0))),
        ]);
        if (!write_cfg($cfg)) { json_out(['success' => false, 'msg' => 'Could not write ' . CFG_FILE]); exit; }
        apply_schedule($cfg);
        json_out(['success' => true, 'msg' => 'Settings saved']);
        exit;
    }

    json_out(['success' => false, 'msg' => 'Unknown action: ' . $action]);
    exit;
}

$tolerance = max(1, min(10, (int)($_GET['tolerance'] ?? $cfg['TOLERANCE'])));
$live = live_disk_stats($tolerance, $exclude);
$running = is_running();
$status = @json_decode(@file_get_contents(STATUS_FILE), true);
if (!is_array($status)) $status = [];
if (!$running && in_array($status['state'] ?? '', ['running', 'planning', 'starting'], true)) {
    $status['state'] = 'stopped';      // engine died without writing a final state
}
if ($running && empty($status['state'])) $status['state'] = 'starting';
$state = $status['state'] ?? 'idle';
if ($state === 'done') $state = 'completed';

$out = $status;
$out['state']      = $state;
$out['running']    = $running;
$out['live_disks'] = $live['disks'];
$out['live_target_pct'] = $live['target_pct'];
if (empty($out['disks'])) $out['disks'] = $live['disks'];
if (!isset($out['target_pct'])) $out['target_pct'] = $live['target_pct'];
if (!isset($out['tolerance'])) $out['tolerance'] = $tolerance;
if (empty($out['plan'])) $out['plan'] = get_plan_items($running ? 200 : 500);
$out['log']      = get_recent_log($running ? 50 : 80);
$out['settings'] = $cfg;
$out['shares']   = list_shares();
$out['guards']   = get_guards();
$out['history']  = get_history($cfg['RUNS_DIR']);
$out['server_time'] = time();
json_out($out);

} catch (\Throwable $e) {
    json_out(['state' => 'error', 'msg' => $e->getMessage()]);
}
