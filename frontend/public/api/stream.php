<?php
// Allow the script to run indefinitely (recommended for SSE / streaming)
set_time_limit(0);

// Disable gzip compression to prevent output buffering (critical for streaming)
ini_set('zlib.output_compression', 0);

// Do not enforce a frontend/PHP request throttle here. The previous file-based
// limiter was unreliable across multiple Coolify replicas because the JSON
// state file lived in sys_get_temp_dir(), which is replica-local.

/**
 * ===============================
 * Server-Sent Events (SSE) Headers
 * ===============================
 */
header("Content-Type: text/event-stream");
header("Cache-Control: no-cache");
header("Connection: keep-alive");
header("X-Accel-Buffering: no");

/**
 * ===============================
 * Output Buffer Handling
 * ===============================
 */
while (ob_get_level() > 0) {
    ob_end_flush();
}
ob_implicit_flush(true);

/**
 * ===============================
 * Environment Setup
 * ===============================
 */
require __DIR__ . '/../../vendor/autoload.php';

// Load .env file only if it exists (optional for Container Apps which inject env vars directly)
$envPath = __DIR__ . '/../../';
if (file_exists($envPath . '.env')) {
    $dotenv = Dotenv\Dotenv::createImmutable($envPath);
    $dotenv->safeLoad();
}

/**
 * ===============================
 * Input Validation
 * ===============================
 */
$payload_raw = file_get_contents("php://input");
$payload = json_decode($payload_raw, true);

if (!$payload || !isset($payload['message'])) {
    echo "event: error\n";
    echo "data: " . json_encode(["error" => "Invalid payload"]) . "\n\n";
    flush();
    exit;
}

$maxMessageChars = (int)($_ENV['COMPASS_CHAT_MESSAGE_MAX_CHARS'] ?? getenv('COMPASS_CHAT_MESSAGE_MAX_CHARS') ?: 2000);
if (!is_string($payload['message']) || strlen($payload['message']) > $maxMessageChars) {
    sendSSE('error', ['error' => "Message must be {$maxMessageChars} characters or fewer"]);
    exit;
}

$upstreamPayload = ['message' => $payload['message']];
if (array_key_exists('session_id', $payload)) {
    if ($payload['session_id'] === null || $payload['session_id'] === '') {
        $upstreamPayload['session_id'] = null;
    } elseif (is_string($payload['session_id'])) {
        $upstreamPayload['session_id'] = $payload['session_id'];
    } else {
        sendSSE('error', ['error' => 'Invalid session_id']);
        exit;
    }
}
// #1348: thread a clicked clarification option's machine handle upstream so
// the backend can resume deterministically. Only a non-empty string is
// forwarded; anything else is ignored (the normal typed turn sends nothing).
if (array_key_exists('selected_option', $payload)
    && is_string($payload['selected_option'])
    && $payload['selected_option'] !== '') {
    $upstreamPayload['selected_option'] = $payload['selected_option'];
}
// WS-5 / G5: forward the pseudonymous visitor id so the backend can persist it
// on the session row for repeat-user counting. Only a non-empty string is
// forwarded, capped to the contract's 128-char max; anything else is dropped.
if (array_key_exists('visitor_id', $payload)
    && is_string($payload['visitor_id'])
    && $payload['visitor_id'] !== '') {
    $upstreamPayload['visitor_id'] = substr($payload['visitor_id'], 0, 128);
}

/**
 * ===============================
 * FastAPI Configuration
 * ===============================
 */
$fastApiToken = $_ENV["FASTAPI_API_TOKEN"] ?? getenv('FASTAPI_API_TOKEN') ?: '';
$fastChatUrl = $_ENV["FASTAPI_CHAT_URL"] ?? getenv('FASTAPI_CHAT_URL') ?: '';

if (empty($fastChatUrl)) {
    sendSSE('error', ['error' => 'Backend not configured']);
    exit;
}

/**
 * ===============================
 * Helper Function: Send SSE Event
 * ===============================
 */
function sendSSE($event, $data) {
    echo "event: $event\n";
    echo "data: " . (is_string($data) ? $data : json_encode($data)) . "\n\n";
    flush();
}

/**
 * ===============================
 * cURL Streaming Request
 * ===============================
 */
// Detect local development (PHP built-in server or localhost)
$isLocalDev = (php_sapi_name() === 'cli-server') || 
              (isset($_SERVER['HTTP_HOST']) && strpos($_SERVER['HTTP_HOST'], 'localhost') !== false);

$ch = curl_init($fastChatUrl);
$upstreamBodyPreview = '';
$sawDone = false;
$streamStart = microtime(true);
$graceS = (float)(getenv('COMPASS_SSE_DISCONNECT_GRACE_S') ?: 2.0);

if (!$ch) {
    sendSSE('error', ['error' => 'Failed to initialize connection']);
    exit;
}

curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_HTTPHEADER => [
        "Content-Type: application/json",
        "Authorization: Bearer $fastApiToken"
    ],
    CURLOPT_POSTFIELDS => json_encode($upstreamPayload),
    
    // SSL options - disable verification for local development only
    // In production (Container Apps), SSL verification remains enabled
    CURLOPT_SSL_VERIFYPEER => !$isLocalDev,
    CURLOPT_SSL_VERIFYHOST => $isLocalDev ? 0 : 2,
    
    /**
     * Process incoming SSE stream from FastAPI
     */
    CURLOPT_WRITEFUNCTION => function($ch, $data) use (&$upstreamBodyPreview, &$sawDone, $streamStart, $graceS) {
        static $buffer = '';
        static $currentEvent = null;
        static $currentData = '';

        if (strlen($upstreamBodyPreview) < 4096) {
            $upstreamBodyPreview .= substr($data, 0, 4096 - strlen($upstreamBodyPreview));
        }

        // Accumulate incoming data
        $buffer .= $data;

        // Process complete lines
        while (($pos = strpos($buffer, "\n")) !== false) {
            $line = substr($buffer, 0, $pos);
            $buffer = substr($buffer, $pos + 1);

            $line = rtrim($line, "\r");

            // Empty line signals end of SSE message
            if ($line === '') {
                if ($currentEvent && $currentData) {
                    processEvent($currentEvent, $currentData);
                    if ($currentEvent === 'done') { $sawDone = true; }
                    $currentEvent = null;
                    $currentData = '';
                }
                continue;
            }

            // Parse SSE format: "event: eventName" or "data: payload"
            if (strpos($line, 'event: ') === 0) {
                $currentEvent = substr($line, 7);
            } elseif (strpos($line, 'data: ') === 0) {
                $currentData .= substr($line, 6);
            }
        }

        // Propagate a browser disconnect upstream (issue #1097). sendSSE() above
        // flushes to the client; if the browser has navigated away, that flush
        // marks the connection aborted. Returning a byte count != strlen($data)
        // makes cURL abort this transfer, which closes the upstream FastAPI
        // connection. The backend's SSE drain then receives the disconnect and
        // cancels the in-flight turn (compass_backend/api/chat_stream.py, #1081)
        // instead of running the full planner->render pipeline to completion and
        // burning gateway tokens + holding a pooled DB connection. Without this,
        // cURL keeps draining the backend to completion and the backend never
        // sees the client leave.
        //
        // Grace window: ignore connection_aborted() for the first $graceS seconds
        // to avoid a spurious abort on a cold path (issue #1383 / A2 mitigation).
        if (connection_aborted() && (microtime(true) - $streamStart) >= $graceS) {
            return 0;
        }

        return strlen($data);
    },

    /**
     * Detect a browser disconnect during QUIET stages (issue #1097).
     *
     * The write callback above only runs when the backend sends data, so a
     * disconnect that lands in a silent stage (planner ~6s, or execute/render
     * up to ~25s for some queries) isn't noticed until the backend's next
     * event -- which, for the expensive stages, is AFTER the answer has already
     * been rendered and persisted. By then cancelling is pointless.
     *
     * cURL calls this progress callback periodically (~once/second) even when
     * no data is flowing. Once per second we write + flush a no-op SSE comment;
     * if the browser is gone, that flush makes PHP mark the connection aborted.
     * Returning non-zero then aborts the transfer (CURLE_ABORTED_BY_CALLBACK),
     * closing the upstream FastAPI connection so the backend cancels the turn
     * (#1081) promptly -- before the costly render+persist completes. The
     * keepalive is a spec SSE comment (": ..."), which every consumer ignores
     * (see assets/js/agentSSE.js: a frame with no `data:` dispatches nothing).
     */
    CURLOPT_NOPROGRESS => false,
    CURLOPT_PROGRESSFUNCTION => function($ch, $downTotal, $downNow, $upTotal, $upNow) use ($streamStart, $graceS) {
        static $lastBeat = 0.0;
        $now = microtime(true);
        if ($now - $lastBeat >= 1.0) {
            $lastBeat = $now;
            echo ": keepalive\n\n";
            flush();
        }
        // Non-zero return aborts the transfer (CURLE_ABORTED_BY_CALLBACK = 42).
        // Grace window: ignore connection_aborted() for the first $graceS seconds
        // to avoid a spurious abort on a cold path (issue #1383 / A2 mitigation).
        if (connection_aborted() && ($now - $streamStart) >= $graceS) {
            return 1;
        }
        return 0;
    },

    CURLOPT_RETURNTRANSFER => false,
    CURLOPT_TIMEOUT => (int)(getenv('COMPASS_SSE_PROXY_TIMEOUT') ?: 300),
    CURLOPT_CONNECTTIMEOUT => 10,
]);

/**
 * ===============================
 * Event Processing Function
 * ===============================
 *
 * SSE event taxonomy (canonical contract: docs/chat-frontend-api.md):
 *   - Streaming text:     text, thinking, status, glossary, citations,
 *                         quotes, chart, data, done, error
 *   - Pipeline progress:  trace, steps, step_complete,
 *                         stage_start, stage_end,
 *                         brief_submitted, context_updated, compliance_retry
 *   - Tool diagnostics:   tool_call_start, tool_call_end
 *   - Critic gate:        critic_start, critic_approved, critic_revision,
 *                         critic_override, critic_verdict
 *
 * All allowlisted events forward unchanged to the client. Unknown events
 * are logged but not relayed — protects the frontend from malformed SSE
 * the proxy can't validate.
 */
const ALLOWED_SSE_EVENTS = [
    'trace', 'text', 'thinking', 'status', 'glossary',
    'ephemera', 'ephemera_batch',
    'steps', 'step_complete',
    'stage_start', 'stage_end',
    'tool_call_start', 'tool_call_end',
    'brief_submitted', 'context_updated', 'compliance_retry',
    'critic_start', 'critic_approved', 'critic_revision',
    'critic_override', 'critic_verdict',
    'citations', 'quotes', 'chart', 'data', 'options',
    'done', 'error',
];

function processEvent($event, $data) {
    if (in_array($event, ALLOWED_SSE_EVENTS, true)) {
        sendSSE($event, $data);
        return;
    }
    error_log("Unknown SSE event: $event");
}

/**
 * ===============================
 * Execute cURL Request
 * ===============================
 */
if (curl_exec($ch) === false) {
    $errno = curl_errno($ch);
    // CURLE_WRITE_ERROR (23, from the write callback) and CURLE_ABORTED_BY_CALLBACK
    // (42, from the progress callback) are how we deliberately abort the upstream
    // when the browser has disconnected (#1097). Both are clean client-side
    // disconnects — the backend will cancel the turn (#1081) — not upstream
    // failures, so don't log them or try to emit an error event to a connection
    // that's already gone.
    if (
        $errno !== CURLE_WRITE_ERROR
        && $errno !== CURLE_ABORTED_BY_CALLBACK
        && !connection_aborted()
    ) {
        $err = curl_error($ch);
        error_log("[stream.php] upstream cURL failure: errno=$errno error=$err");
        sendSSE('error', ["error" => "Upstream request failed"]);
    } elseif (
        ($errno === CURLE_WRITE_ERROR || $errno === CURLE_ABORTED_BY_CALLBACK)
        && !$sawDone
        && !connection_aborted()
    ) {
        error_log("[stream.php] upstream aborted before 'done' with client still connected; emitting terminal error");
        sendSSE('error', ["error" => "stream_truncated", "message" => "This turn was interrupted before completing. Please ask again."]);
    }
} else {
    $statusCode = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    if ($statusCode >= 400) {
        $detail = json_decode($upstreamBodyPreview, true);
        error_log("[stream.php] upstream HTTP $statusCode: " . substr($upstreamBodyPreview, 0, 1000));
        sendSSE('error', [
            'error' => 'Upstream request failed',
            'status' => $statusCode,
            'detail' => $detail ?: trim($upstreamBodyPreview),
        ]);
    }
}
curl_close($ch);
