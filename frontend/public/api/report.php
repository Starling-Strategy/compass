<?php
/**
 * Debug-view review-loop proxy.
 *
 * Forwards reviewer pass/partial/fail reports to the FastAPI
 * /api/v1/debug/report endpoints. The backend routes are UNAUTHENTICATED
 * (possession of the debug link is the access control), but we still mirror
 * feedback.php's server-side token injection so the proxy behaves identically
 * to the other api/*.php forwarders and keeps working if the backend later
 * gates the routes.
 *
 * Routing:
 *   POST /api/report.php                                  → POST /api/v1/debug/report (JSON body forwarded verbatim)
 *   GET  /api/report.php?session_id=<uuid>[&case_id=<int>] → GET  /api/v1/debug/report (query forwarded)
 */

require __DIR__ . '/../../vendor/autoload.php';

$envPath = __DIR__ . '/../../';
if (file_exists($envPath . '.env')) {
    $dotenv = Dotenv\Dotenv::createImmutable($envPath);
    $dotenv->safeLoad();
}

header('Content-Type: application/json');

$fastApiToken = $_ENV['FASTAPI_API_TOKEN'] ?? getenv('FASTAPI_API_TOKEN') ?: '';
$fastChatUrl = $_ENV['FASTAPI_CHAT_URL'] ?? getenv('FASTAPI_CHAT_URL') ?: '';

if (empty($fastChatUrl)) {
    http_response_code(500);
    echo json_encode(['error' => 'Backend not configured']);
    exit;
}

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$baseReportUrl = preg_replace('#/chat(?:/simple)?/?$#', '/debug/report', $fastChatUrl);

$upstreamUrl = $baseReportUrl;

if ($method === 'GET') {
    $sessionId = $_GET['session_id'] ?? '';
    if (!preg_match('/^[0-9a-fA-F-]{32,36}$/', $sessionId)) {
        http_response_code(400);
        echo json_encode(['error' => 'Invalid session_id']);
        exit;
    }
    $query = ['session_id' => $sessionId];

    $caseId = $_GET['case_id'] ?? '';
    if ($caseId !== '') {
        if (!ctype_digit((string) $caseId)) {
            http_response_code(400);
            echo json_encode(['error' => 'Invalid case_id']);
            exit;
        }
        $query['case_id'] = $caseId;
    }
    $upstreamUrl .= '?' . http_build_query($query);
}

$isLocalDev = (php_sapi_name() === 'cli-server') ||
    (isset($_SERVER['HTTP_HOST']) && strpos($_SERVER['HTTP_HOST'], 'localhost') !== false);

$ch = curl_init($upstreamUrl);
$curlOpts = [
    CURLOPT_CUSTOMREQUEST => $method,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . $fastApiToken,
        'Content-Type: application/json',
        'Accept: application/json',
    ],
    CURLOPT_SSL_VERIFYPEER => !$isLocalDev,
    CURLOPT_SSL_VERIFYHOST => $isLocalDev ? 0 : 2,
    CURLOPT_TIMEOUT => 10,
    CURLOPT_CONNECTTIMEOUT => 5,
];

if ($method === 'POST') {
    $requestBody = file_get_contents('php://input');
    if ($requestBody === false || $requestBody === '') {
        http_response_code(400);
        echo json_encode(['error' => 'Empty request body']);
        exit;
    }
    $curlOpts[CURLOPT_POSTFIELDS] = $requestBody;
}

curl_setopt_array($ch, $curlOpts);

$body = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err = curl_error($ch);
$errno = curl_errno($ch);

if ($body === false) {
    error_log("[report.php] upstream cURL failure: http=$httpCode errno=$errno error=$err");
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 200);
echo $body;
