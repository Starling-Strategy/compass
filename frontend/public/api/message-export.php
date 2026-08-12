<?php
/**
 * Retired lazy message CSV export proxy.
 *
 * Fresh Compass SSE sends structured table data with the assistant response, so
 * CSV downloads are generated client-side from that payload. Keep this endpoint
 * as a clear 410 for stale clients instead of forwarding to a missing backend.
 */

header('Content-Type: application/json');
http_response_code(410);
echo json_encode([
    'error' => 'Lazy message CSV export is retired; use client-side table export.',
]);
