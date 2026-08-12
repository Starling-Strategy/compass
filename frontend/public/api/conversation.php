<?php
/**
 * Conversation GET proxy.
 *
 * Used by the client-side resync fallback in agentSSE.js when an SSE
 * stream drops before emitting `done`. Browsers on nctq.org do not hold
 * the server-side API token, so this thin proxy forwards the request to
 * the FastAPI backend using FASTAPI_API_TOKEN — mirroring stream.php.
 *
 * Query: ?session_id=<uuid>
 * Response: application/json — the ChatSession as returned by
 *   GET /api/v1/conversations/{session_id}, or { "error": "..." }.
 */

require __DIR__ . '/../../vendor/autoload.php';

$envPath = __DIR__ . '/../../';
if (file_exists($envPath . '.env')) {
    $dotenv = Dotenv\Dotenv::createImmutable($envPath);
    $dotenv->safeLoad();
}

header('Content-Type: application/json');

$sessionId = $_GET['session_id'] ?? '';

// Guard: UUID-only. Blocks path injection and anything surprising.
if (!preg_match('/^[0-9a-fA-F-]{32,36}$/', $sessionId)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid session_id']);
    exit;
}

$fastApiToken = $_ENV['FASTAPI_API_TOKEN'] ?? getenv('FASTAPI_API_TOKEN') ?: '';
$fastChatUrl = $_ENV['FASTAPI_CHAT_URL'] ?? getenv('FASTAPI_CHAT_URL') ?: '';

if (empty($fastChatUrl)) {
    http_response_code(500);
    echo json_encode(['error' => 'Backend not configured']);
    exit;
}

// Derive the conversations URL from the chat URL: replace /chat with /conversations/{id}
$conversationUrl = preg_replace(
    '#/chat(?:/simple)?/?$#',
    '/conversations/' . rawurlencode($sessionId),
    $fastChatUrl
);

$isLocalDev = (php_sapi_name() === 'cli-server') ||
    (isset($_SERVER['HTTP_HOST']) && strpos($_SERVER['HTTP_HOST'], 'localhost') !== false);

$ch = curl_init($conversationUrl);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . $fastApiToken,
        'Accept: application/json',
    ],
    CURLOPT_SSL_VERIFYPEER => !$isLocalDev,
    CURLOPT_SSL_VERIFYHOST => $isLocalDev ? 0 : 2,
    CURLOPT_TIMEOUT => 10,
    CURLOPT_CONNECTTIMEOUT => 5,
]);

$body = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err = curl_error($ch);
$errno = curl_errno($ch);

if ($body === false) {
    error_log("[conversation.php] upstream cURL failure: http=$httpCode errno=$errno error=$err");
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 200);
echo $body;
