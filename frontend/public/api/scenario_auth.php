<?php
/**
 * Shared signed case-link guard for debug-only PHP proxies.
 */

function compass_load_env(string $envPath): void
{
    $autoload = $envPath . 'vendor/autoload.php';
    if (file_exists($autoload)) {
        require_once $autoload;
    }

    if (!file_exists($envPath . '.env')) {
        return;
    }

    if (class_exists('Dotenv\\Dotenv')) {
        $dotenv = Dotenv\Dotenv::createImmutable($envPath);
        $dotenv->safeLoad();
        return;
    }

    foreach (file($envPath . '.env', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#') || !str_contains($line, '=')) {
            continue;
        }
        [$key, $value] = explode('=', $line, 2);
        $key = trim($key);
        $value = trim($value, " \t\n\r\0\x0B\"'");
        if ($key !== '' && getenv($key) === false) {
            $_ENV[$key] = $value;
            putenv($key . '=' . $value);
        }
    }
}

function compass_env(string $name, string $default = ''): string
{
    $value = $_ENV[$name] ?? getenv($name);
    if ($value === false || $value === null) {
        return $default;
    }
    return (string) $value;
}

function compass_is_local_dev(): bool
{
    return (php_sapi_name() === 'cli-server') ||
        (isset($_SERVER['HTTP_HOST']) && (
            str_contains($_SERVER['HTTP_HOST'], 'localhost') ||
            str_contains($_SERVER['HTTP_HOST'], '127.0.0.1')
        ));
}

function compass_request_host(): string
{
    $host = strtolower((string) ($_SERVER['HTTP_HOST'] ?? ''));
    if (str_contains($host, ':')) {
        $host = explode(':', $host, 2)[0];
    }
    return $host;
}

function compass_is_staging_frontend(): bool
{
    return compass_request_host() === 'staging-compass.nctq.ai';
}

function compass_env_flag(string $name): ?bool
{
    $raw = compass_env($name, '');
    if ($raw === '') {
        return null;
    }

    $normalized = strtolower(trim($raw));
    if (in_array($normalized, ['1', 'true', 'yes', 'on'], true)) {
        return true;
    }
    if (in_array($normalized, ['0', 'false', 'no', 'off'], true)) {
        return false;
    }

    return null;
}

function compass_unsigned_scenarios_allowed(): bool
{
    $configured = compass_env_flag('COMPASS_ALLOW_UNSIGNED_SCENARIOS');
    if ($configured !== null) {
        return $configured;
    }

    // Staging and local debug links are shared in docs, sheets, and PRs; keep
    // those URLs durable. Production still requires signed case links unless
    // an operator explicitly opts a host into unsigned launches.
    return compass_is_local_dev() || compass_is_staging_frontend();
}

function compass_case_id_from_request(): string
{
    $fromCase = $_GET['case_id'] ?? '';
    $fromId = $_GET['id'] ?? '';
    if ($fromCase !== '' && $fromId !== '' && $fromCase !== $fromId) {
        return '';
    }
    return (string) ($fromCase !== '' ? $fromCase : $fromId);
}

function compass_validate_case_signature(string $caseId): array
{
    // All failure modes collapse to a single opaque response. The specific
    // reason is logged server-side via error_log() for operator forensics so
    // probes can't distinguish "invalid case_id" from "missing signature"
    // from "expired" from "secret not configured". See audit finding #22
    // (docs/audit/2026-05-25-audit-report.md).
    if (!ctype_digit($caseId)) {
        error_log('compass_validate_case_signature: invalid case_id');
        return [false, null];
    }

    $exp = (string) ($_GET['case_exp'] ?? '');
    $sig = (string) ($_GET['case_sig'] ?? '');
    if ($exp === '' && $sig === '' && compass_unsigned_scenarios_allowed()) {
        return [true, null];
    }

    if ($exp === '' || $sig === '') {
        error_log('compass_validate_case_signature: missing signature for case_id=' . $caseId);
        return [false, null];
    }
    if (!ctype_digit($exp)) {
        error_log('compass_validate_case_signature: non-numeric exp for case_id=' . $caseId);
        return [false, null];
    }
    if ((int) $exp < time()) {
        error_log('compass_validate_case_signature: expired signature for case_id=' . $caseId);
        return [false, null];
    }

    $secret = compass_env('COMPASS_SCENARIO_LINK_SECRET');
    if ($secret === '') {
        error_log('compass_validate_case_signature: COMPASS_SCENARIO_LINK_SECRET not configured');
        return [false, null];
    }

    $expected = hash_hmac('sha256', $caseId . '.' . $exp, $secret);
    if (!hash_equals($expected, $sig)) {
        error_log('compass_validate_case_signature: hmac mismatch for case_id=' . $caseId);
        return [false, null];
    }

    return [true, null];
}

function compass_fail_json(int $statusCode, string $message): void
{
    header('Content-Type: application/json');
    http_response_code($statusCode);
    echo json_encode(['error' => $message]);
    exit;
}

function compass_fail_forbidden(): void
{
    compass_fail_json(403, 'Forbidden');
}
