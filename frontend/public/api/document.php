<?php
/**
 * Retired full-document proxy.
 *
 * Fresh Compass citations do not expose a stable full-text document id, so the
 * frontend should not offer a full-document viewer. Keep this endpoint local
 * and explicit so stale browser code degrades without a backend 404/502.
 */

header('Content-Type: application/json');
http_response_code(410);
echo json_encode([
    'error' => 'Full-document text viewing is not available in fresh Compass.',
]);
