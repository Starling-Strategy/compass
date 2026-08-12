<?php
/**
 * Feedback proxy.
 *
 * Forwards thumbs-up/down feedback operations to the FastAPI
 * /api/v1/feedback endpoints. Browsers do not hold the server-side API
 * token, so this proxy injects FASTAPI_API_TOKEN — mirroring
 * conversation.php / conversations-export.php / stream.php.
 *
 * Routing:
 *   POST   /api/feedback.php                                 → POST   /api/v1/feedback             (JSON body forwarded verbatim)
 *   GET    /api/feedback.php?session_id=<uuid>               → GET    /api/v1/feedback/{sid}
 *   DELETE /api/feedback.php?session_id=<uuid>&message_id=N  → DELETE /api/v1/feedback/{sid}/{mid}
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
$baseFeedbackUrl = preg_replace('#/chat(?:/simple)?/?$#', '/feedback', $fastChatUrl);

$sessionId = $_GET['session_id'] ?? '';
$messageId = $_GET['message_id'] ?? '';

$upstreamUrl = $baseFeedbackUrl;

if ($method === 'GET' || $method === 'DELETE') {
    if (!preg_match('/^[0-9a-fA-F-]{32,36}$/', $sessionId)) {
        http_response_code(400);
        echo json_encode(['error' => 'Invalid session_id']);
        exit;
    }
    $upstreamUrl .= '/' . rawurlencode($sessionId);

    if ($method === 'DELETE') {
        if (!ctype_digit((string) $messageId)) {
            http_response_code(400);
            echo json_encode(['error' => 'Invalid message_id']);
            exit;
        }
        $upstreamUrl .= '/' . rawurlencode($messageId);
    }
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
    error_log("[feedback.php] upstream cURL failure: http=$httpCode errno=$errno error=$err");
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 200);
echo $body;
