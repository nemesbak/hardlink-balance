<?php
/**
 * Hardlink Balance / EquiLink — AJAX status & control endpoint
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

function pid_alive(int $pid): bool {
    if ($pid <= 0) return false;
    if (function_exists('posix_kill')) return posix_kill($pid, 0);
    return file_exists("/proc/$pid");
}

function is_running(): bool {
    if (!file_exists(LOCK_FILE)) return false;
    $pid = (int) @file_get_contents(LOCK_FILE);
    return pid_alive($pid);
}

function _check_disk_path(string $mount): string {
    $datos = $mount . '/datos';
    if (is_dir($datos)) {
        $st = @disk_total_space($datos);
        if ($st && $st > 1073741824) return $datos;
    }
    return $mount;
}

function get_disk_meta(): array {
    $meta = [];
    $ini = @parse_ini_file('/var/local/emhttp/disks.ini', true);
    if ($ini) {
        foreach ($ini as $sec => $d) {
            $name = trim($sec, '[]"');
            if (strpos($name, 'disk') === 0) {
                $meta[$name] = [
                    'fsType'    => strtoupper($d['fsType'] ?? 'XFS'),
                    'device'    => $d['device'] ?? '',
                    'temp'      => ($d['temp'] ?? '*') !== '*' ? $d['temp'] . '°C' : '',
                    'rotational'=> ($d['rotational'] ?? '1') === '1',
                ];
            }
        }
    }
    return $meta;
}

function live_disk_stats(int $tolerance): array {
    $mounts = glob('/mnt/disk[0-9]*') ?: [];
    natsort($mounts);

    $disk_meta = get_disk_meta();
    $total_size = 0;
    $total_used = 0;
    $raw = [];

    foreach ($mounts as $mount) {
        if (!is_dir($mount)) continue;
        $name = basename($mount);
        $chk_path = _check_disk_path($mount);
        $total  = @disk_total_space($chk_path);
        $free   = @disk_free_space($chk_path);
        if ($total === false || $total == 0) continue;

        $used        = $total - $free;
        $total_size += $total;
        $total_used += $used;

        $pct = ($total > 0) ? (int)round($used * 100 / $total) : 0;
        $meta = $disk_meta[$name] ?? ['fsType' => 'XFS', 'temp' => '', 'device' => ''];

        $raw[] = [
            'name'    => $name,
            'mount'   => $mount,
            'fsType'  => $meta['fsType'],
            'temp'    => $meta['temp'],
            'size_kb' => (int)($total / 1024),
            'used_kb' => (int)($used  / 1024),
            'free_kb' => (int)($free  / 1024),
            'pct'     => $pct,
            'role'    => 'balanced',
            'delta_kb'=> 0,
        ];
    }

    $target_pct = ($total_size > 0) ? (int)round($total_used * 100 / $total_size) : 0;

    foreach ($raw as &$d) {
        $target_used_kb = (int)($d['size_kb'] * $target_pct / 100);
        $d['delta_kb'] = $d['used_kb'] - $target_used_kb;

        if ($d['pct'] > $target_pct + $tolerance) {
            $d['role'] = 'source';
        } elseif ($d['pct'] < $target_pct - $tolerance) {
            $d['role'] = 'destination';
        }
    }
    unset($d);

    return ['disks' => array_values($raw), 'target_pct' => $target_pct];
}

function get_recent_log(int $lines = 80): array {
    if (!file_exists(LOG_FILE)) return [];
    $data = @file(LOG_FILE, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!$data) return [];
    return array_values(array_slice($data, -$lines));
}

function get_plan_items(int $max = 500): array {
    if (!file_exists(PLAN_FILE)) return [];
    $data = json_decode(@file_get_contents(PLAN_FILE), true);
    if (!is_array($data)) return [];
    return array_slice($data, 0, $max);
}

function idle_payload(int $tolerance, ?array $existing = null): array {
    $stats = live_disk_stats($tolerance);

    $base = [
        'state'        => 'idle',
        'dry_run'      => true,
        'tolerance'    => $tolerance,
        'target_pct'   => $stats['target_pct'],
        'elapsed_sec'  => 0,
        'current_file' => '',
        'stats' => [
            'files_total'         => 0,
            'files_done'          => 0,
            'files_skipped'       => 0,
            'bytes_total_kb'      => 0,
            'bytes_done_kb'       => 0,
            'pct_done'            => 0,
            'rate_kbs'            => 0,
            'eta_sec'             => 0,
            'multi_linked_groups' => 0,
            'multi_linked_kb'     => 0,
        ],
        'plan_count' => 0,
        'disks'      => $stats['disks'],
        'plan'       => [],
        'log'        => get_recent_log(),
    ];

    if ($existing && in_array($existing['state'] ?? '', ['completed', 'stopped', 'planned', 'error', 'done'])) {
        $base['state'] = ($existing['state'] === 'done') ? 'completed' : $existing['state'];

        $st = $existing['stats'] ?? [];
        $base['stats'] = [
            'files_total'         => $st['files_total'] ?? ($existing['groups_total'] ?? 0),
            'files_done'          => $st['files_done'] ?? ($existing['groups_done'] ?? 0),
            'files_skipped'       => $st['files_skipped'] ?? ($existing['groups_failed'] ?? 0),
            'bytes_total_kb'      => $st['bytes_total_kb'] ?? ($existing['bytes_total_kb'] ?? 0),
            'bytes_done_kb'       => $st['bytes_done_kb'] ?? ($existing['bytes_done_kb'] ?? 0),
            'pct_done'            => $st['pct_done'] ?? ($existing['pct_done'] ?? 0),
            'rate_kbs'            => $st['rate_kbs'] ?? ($existing['overall_rate_mbs'] ? (int)($existing['overall_rate_mbs'] * 1024) : 0),
            'eta_sec'             => $st['eta_sec'] ?? ($existing['eta_sec'] ?? 0),
            'multi_linked_groups' => $st['multi_linked_groups'] ?? ($existing['multi_linked_groups'] ?? 0),
            'multi_linked_kb'     => $st['multi_linked_kb'] ?? ($existing['multi_linked_kb'] ?? 0),
        ];

        $base['plan_count'] = $existing['plan_count'] ?? ($existing['groups_total'] ?? count($existing['plan'] ?? []));
        $base['plan']       = !empty($existing['plan']) ? $existing['plan'] : get_plan_items();
        $base['log']        = get_recent_log();
        $base['dry_run']    = $existing['dry_run'] ?? false;
        $base['tolerance']  = $existing['tolerance'] ?? $tolerance;
        $base['disks']      = !empty($existing['disks']) ? $existing['disks'] : $stats['disks'];

        $meta = get_disk_meta();
        foreach ($base['disks'] as &$d) {
            if (empty($d['fsType']) && isset($meta[$d['name']])) {
                $d['fsType'] = $meta[$d['name']]['fsType'];
            }
            if (!isset($d['delta_kb'])) {
                $t_kb = (int)($d['size_kb'] * $base['target_pct'] / 100);
                $d['delta_kb'] = $d['used_kb'] - $t_kb;
            }
        }
        unset($d);
    }

    return $base;
}

function launch_script(string $cmd): bool {
    $shell_cmd = 'nohup ' . $cmd . ' </dev/null >/dev/null 2>&1 &';
    if (function_exists('exec')) {
        exec($shell_cmd);
        return true;
    }
    if (function_exists('shell_exec')) {
        shell_exec($shell_cmd);
        return true;
    }
    return false;
}

function json_out($data): void {
    ob_clean();
    echo json_encode($data);
}

try {

$var = @parse_ini_file('/var/local/emhttp/var.ini') ?: [];
$csrf_expected = $var['csrf_token'] ?? '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!empty($csrf_expected) && isset($_POST['csrf_token']) && $_POST['csrf_token'] !== $csrf_expected) {
        json_out(['success' => false, 'msg' => 'Invalid CSRF token']);
        exit;
    }

    $action      = $_POST['action']       ?? '';
    $tolerance   = max(1, min(20, (int)($_POST['tolerance'] ?? 2)));
    $dry_run     = !empty($_POST['dry_run']);
    $min_file_kb = max(0, (int)($_POST['min_file_kb'] ?? 102400));
    $limit_gb    = max(0, (float)($_POST['limit_gb'] ?? 0));

    if ($action === 'start') {
        if (is_running()) {
            json_out(['success' => false, 'msg' => 'Already running']);
            exit;
        }

        if (!file_exists(SCRIPT)) {
            json_out(['success' => false, 'msg' => 'Script not found: ' . SCRIPT]);
            exit;
        }

        $args = '--tolerance ' . escapeshellarg((string)$tolerance);
        if ($dry_run) {
            $args .= ' --dry-run';
        } else {
            $args .= ' --execute';
        }

        if ($min_file_kb > 0) {
            $args .= ' --min-size-kb ' . escapeshellarg((string)$min_file_kb);
        }
        if ($limit_gb > 0) {
            $args .= ' --limit-gb ' . escapeshellarg((string)$limit_gb);
        }

        $cmd = 'python3 ' . escapeshellarg(SCRIPT) . ' ' . $args;
        $ok = launch_script($cmd);

        if (!$ok) {
            json_out(['success' => false, 'msg' => 'Unable to launch script']);
            exit;
        }

        usleep(300000);
        json_out(['success' => true]);
        exit;
    }

    if ($action === 'stop') {
        $stop_cmd = 'python3 ' . escapeshellarg(SCRIPT) . ' --stop';
        if (function_exists('exec')) {
            exec($stop_cmd);
        } else {
            shell_exec($stop_cmd);
        }
        json_out(['success' => true, 'msg' => 'Stop signal sent']);
        exit;
    }

    if ($action === 'clear') {
        @unlink(STATUS_FILE);
        @unlink(PLAN_FILE);
        json_out(idle_payload($tolerance));
        exit;
    }

    json_out(['success' => false, 'msg' => 'Unknown action: ' . $action]);
    exit;
}

$tolerance = max(1, min(20, (int)($_GET['tolerance'] ?? 2)));

if (is_running()) {
    if (file_exists(STATUS_FILE)) {
        $curr = json_decode(@file_get_contents(STATUS_FILE), true) ?: [];
        if (empty($curr['plan'])) {
            $curr['plan'] = get_plan_items(200);
        }
        $curr['log'] = get_recent_log(50);
        json_out($curr);
    } else {
        $live = live_disk_stats($tolerance);
        json_out([
            'state'        => 'starting',
            'dry_run'      => false,
            'tolerance'    => $tolerance,
            'target_pct'   => $live['target_pct'],
            'elapsed_sec'  => 0,
            'current_file' => '',
            'stats' => [
                'files_total' => 0, 'files_done' => 0, 'files_skipped' => 0,
                'bytes_total_kb' => 0, 'bytes_done_kb' => 0, 'pct_done' => 0,
                'rate_kbs' => 0, 'eta_sec' => 0, 'multi_linked_groups' => 0, 'multi_linked_kb' => 0,
            ],
            'plan_count' => 0,
            'plan'       => [],
            'log'        => get_recent_log(30),
            'disks'      => $live['disks'],
        ]);
    }
    exit;
}

$existing = null;
if (file_exists(STATUS_FILE)) {
    $existing = json_decode(@file_get_contents(STATUS_FILE), true);
}
json_out(idle_payload($tolerance, $existing));

} catch (\Throwable $e) {
    json_out(['state' => 'error', 'msg' => $e->getMessage()]);
}
