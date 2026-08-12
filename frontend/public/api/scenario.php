<?php
/**
 * Scenario case GET proxy.
 *
 * Used by debug links to fetch a B-spine case's details, including all input
 * steps, so the debug launch can run the case in one session. The backend
 * case route remains admin-only; this proxy
 * injects FASTAPI_ADMIN_API_TOKEN only after verifying the launch URL.
 *
 * Query: ?case_id=<int>[&case_exp=<unix>&case_sig=<hmac>]
 * Response: application/json — the case as returned by
 *   GET /api/v1/scenario-cases/{case_id}, or { "error": "..." }.
 */

$envPath = __DIR__ . '/../../';
require_once __DIR__ . '/scenario_auth.php';
compass_load_env($envPath);

header('Content-Type: application/json');

$rawId = compass_case_id_from_request();

if (!ctype_digit((string) $rawId)) {
    http_response_code(400);
    echo json_encode(['error' => 'B-spine case_id required']);
    exit;
}

[$signatureOk] = compass_validate_case_signature((string) $rawId);
if (!$signatureOk) {
    compass_fail_forbidden();
}

$caseId = (int) $rawId;

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

$scenarioUrl = preg_replace(
    '#/chat(?:/simple)?/?$#',
    '/scenario-cases/' . $caseId,
    $fastChatUrl
);

$ch = curl_init($scenarioUrl);
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
    error_log("[scenario.php] upstream cURL failure: http=$httpCode errno=$errno error=$err");
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 200);
echo $body;
