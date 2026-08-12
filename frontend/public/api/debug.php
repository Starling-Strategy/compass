<?php
/**
 * Conversation debug GET proxy.
 *
 * Used by the debug view's historical cost-card backfill in debug.js.
 * The backend route is admin-only; this proxy injects FASTAPI_ADMIN_API_TOKEN
 * only after verifying the case launch URL. Staging and local launch URLs are
 * intentionally durable; non-staging hosts can require signed case params.
 *
 * Query: ?session_id=<uuid>&case_id=<int>[&case_exp=<unix>&case_sig=<hmac>]
 * Response: application/json — the assembled debug payload as returned by
 *   GET /api/v1/conversations/{session_id}/debug, or { "error": "..." }.
 */

$envPath = __DIR__ . '/../../';
require_once __DIR__ . '/scenario_auth.php';
compass_load_env($envPath);

header('Content-Type: application/json');

$sessionId = $_GET['session_id'] ?? '';

if (!preg_match('/^[0-9a-fA-F-]{32,36}$/', $sessionId)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid session_id']);
    exit;
}

$caseId = compass_case_id_from_request();
if ($caseId === '') {
    if (!compass_is_local_dev()) {
        error_log('debug.php: missing case_id on non-local host');
        compass_fail_forbidden();
    }
} else {
    [$signatureOk] = compass_validate_case_signature($caseId);
    if (!$signatureOk) {
        compass_fail_forbidden();
    }
}

$fastApiToken = compass_env('FASTAPI_ADMIN_API_TOKEN');
$fastChatUrl = compass_env('FASTAPI_CHAT_URL');

if (empty($fastChatUrl)) {
    http_response_code(500);
    echo json_encode(['error' => 'Backend not configured']);
    exit;
}
if (empty($fastApiToken)) {
    http_response_code(500);
    echo json_encode(['error' => 'Admin backend token not configured']);
    exit;
}

$debugUrl = preg_replace(
    '#/chat(?:/simple)?/?$#',
    '/conversations/' . rawurlencode($sessionId) . '/debug',
    $fastChatUrl
);

$ch = curl_init($debugUrl);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . $fastApiToken,
        'Accept: application/json',
    ],
    CURLOPT_SSL_VERIFYPEER => !compass_is_local_dev(),
    CURLOPT_SSL_VERIFYHOST => compass_is_local_dev() ? 0 : 2,
    CURLOPT_TIMEOUT => 10,
    CURLOPT_CONNECTTIMEOUT => 5,
]);

$body = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err = curl_error($ch);
$errno = curl_errno($ch);

if ($body === false) {
    error_log("[debug.php] upstream cURL failure: http=$httpCode errno=$errno error=$err");
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 200);
echo $body;
