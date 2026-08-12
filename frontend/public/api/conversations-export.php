<?php
/**
 * Conversation export proxy.
 *
 * Forwards POST {session_id, exported_at, turns} to the FastAPI
 * /api/v1/conversations/export endpoint and streams the ZIP response back.
 *
 * Browsers on nctq.org do not hold the server-side API token, so this
 * proxy injects FASTAPI_API_TOKEN — mirroring conversation.php / stream.php.
 *
 * Body:    application/json (forwarded verbatim)
 * Response: application/zip (with Content-Disposition from upstream)
 *           OR upstream JSON error body with matching HTTP status.
 */

require __DIR__ . '/../../vendor/autoload.php';

$envPath = __DIR__ . '/../../';
if (file_exists($envPath . '.env')) {
    $dotenv = Dotenv\Dotenv::createImmutable($envPath);
    $dotenv->safeLoad();
}

$fastApiToken = $_ENV['FASTAPI_API_TOKEN'] ?? getenv('FASTAPI_API_TOKEN') ?: '';
$fastChatUrl = $_ENV['FASTAPI_CHAT_URL'] ?? getenv('FASTAPI_CHAT_URL') ?: '';

if (empty($fastChatUrl)) {
    header('Content-Type: application/json');
    http_response_code(500);
    echo json_encode(['error' => 'Backend not configured']);
    exit;
}

// Derive the export URL from the chat URL: replace /chat with /conversations/export
$exportUrl = preg_replace(
    '#/chat(?:/simple)?/?$#',
    '/conversations/export',
    $fastChatUrl
);

$requestBody = file_get_contents('php://input');
if ($requestBody === false) {
    header('Content-Type: application/json');
    http_response_code(400);
    echo json_encode(['error' => 'Empty request body']);
    exit;
}

$isLocalDev = (php_sapi_name() === 'cli-server') ||
    (isset($_SERVER['HTTP_HOST']) && strpos($_SERVER['HTTP_HOST'], 'localhost') !== false);

// Capture upstream Content-Disposition (and upstream Content-Type for error bodies)
$capturedDisposition = null;
$capturedContentType = null;

$ch = curl_init($exportUrl);
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_POSTFIELDS => $requestBody,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . $fastApiToken,
        'Content-Type: application/json',
        'Accept: application/zip, application/json',
    ],
    CURLOPT_SSL_VERIFYPEER => !$isLocalDev,
    CURLOPT_SSL_VERIFYHOST => $isLocalDev ? 0 : 2,
    CURLOPT_TIMEOUT => 60,
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_HEADERFUNCTION => function ($ch, $headerLine) use (&$capturedDisposition, &$capturedContentType) {
        $trimmed = trim($headerLine);
        if ($trimmed === '') {
            return strlen($headerLine);
        }
        if (stripos($trimmed, 'Content-Disposition:') === 0) {
            $capturedDisposition = trim(substr($trimmed, strlen('Content-Disposition:')));
        } elseif (stripos($trimmed, 'Content-Type:') === 0) {
            $capturedContentType = trim(substr($trimmed, strlen('Content-Type:')));
        }
        return strlen($headerLine);
    },
]);

$body = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err = curl_error($ch);
$errno = curl_errno($ch);

if ($body === false) {
    error_log("[conversations-export.php] upstream cURL failure: http=$httpCode errno=$errno error=$err");
    header('Content-Type: application/json');
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 200);

if ($httpCode >= 200 && $httpCode < 300) {
    // Success: ZIP response — forward content-type + content-disposition
    header('Content-Type: application/zip');
    if ($capturedDisposition !== null) {
        header('Content-Disposition: ' . $capturedDisposition);
    }
    echo $body;
} else {
    // Error: forward upstream content-type (typically application/json) + body
    if ($capturedContentType !== null) {
        header('Content-Type: ' . $capturedContentType);
    } else {
        header('Content-Type: application/json');
    }
    echo $body;
}
