<?php
require __DIR__ . '/../vendor/autoload.php';

// Load .env file only if it exists (optional for Container Apps which inject env vars directly)
$envPath = __DIR__ . '/../';
if (file_exists($envPath . '.env')) {
    $dotenv = Dotenv\Dotenv::createImmutable($envPath);
    $dotenv->safeLoad();
}

// Get environment variables (works with both .env file and Container Apps env vars)
$exportCsvUrl = $_ENV['FASTAPI_EXPORT_CSV_URL'] ?? getenv('FASTAPI_EXPORT_CSV_URL') ?: '';
$isEmbed = isset($_GET['embed']) && $_GET['embed'] === 'true';
$maxMessageChars = (int)($_ENV['COMPASS_CHAT_MESSAGE_MAX_CHARS'] ?? getenv('COMPASS_CHAT_MESSAGE_MAX_CHARS') ?: 2000);
?>
<!DOCTYPE html>
<html lang="en">

<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <!--
    Markdown / sanitiser / charts — vendored same-origin so a school-district
    firewall blocking jsdelivr can't strand DOMPurify (raw markdown would render
    unsanitised) or break chart rendering. Update versions in lockstep with the
    files in /assets/vendor/.
  -->
  <script src="/assets/vendor/marked-12.0.2.min.js"></script>
  <script src="/assets/vendor/purify-3.2.4.min.js"></script>
  <script src="/assets/vendor/chart-4.4.1.umd.min.js"></script>

  <title>NCTQ Compass</title>
  <!-- For .ico file -->
  <link rel="icon" href="/assets/images/favicon.ico" type="image/x-icon" />

  <!-- For .png file -->
  <link rel="icon" href="/assets/images/favicon.png" type="image/png" />

  <!-- Optional: For Apple devices -->
  <link rel="apple-touch-icon" href="/assets/images/apple-touch-icon.png" />
  <link href="/assets/css/index.css?v=20260622a" rel="stylesheet" />

  <!-- Umami analytics (SSN-180) — anonymous traffic counts only, no cookies, IP+UA hashed daily. -->
  <script defer
          src="https://umami.nctq.ai/script.js"
          data-website-id="90a06372-ed2b-42b1-b8a6-b0d922650521"
          data-domains="staging-compass.nctq.ai,compass.nctq.ai"></script>
  <script>
    // Capture embed_host so traffic can be broken down by where Compass is hosted.
    // Direct visit → 'direct'. Inside an iframe (Compass uses ?embed=true on partner
    // pages) → parent page hostname via document.referrer. Falls back to
    // 'unknown-iframe' if document.referrer is empty (e.g., partner sets
    // referrer-policy: no-referrer).
    window.addEventListener('load', function () {
      if (typeof umami === 'undefined' || typeof umami.identify !== 'function') return;
      var inIframe = window.self !== window.top;
      var embedHost = inIframe ? 'unknown-iframe' : 'direct';
      if (inIframe && document.referrer) {
        try { embedHost = new URL(document.referrer).hostname; } catch (e) { /* keep fallback */ }
      }
      umami.identify({ embed_host: embedHost });
    });
  </script>

  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-0VB06N7L8X"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());

    gtag('config', 'G-0VB06N7L8X');
  </script>

  <!-- Expose environment variables to JavaScript -->
  <script>
    window.APP_CONFIG = {
      exportCsvUrl: <?php echo json_encode($exportCsvUrl, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT); ?>,
      logfireBaseUrl: <?php echo json_encode(getenv('LOGFIRE_BASE_URL') ?: 'https://logfire-us.pydantic.dev/murmuration/nctqai', JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT); ?>,
      apiBaseUrl: <?php echo json_encode(getenv('PUBLIC_API_URL') ?: '', JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT); ?>,
      dashboardBaseUrl: <?php echo json_encode(getenv('DASHBOARD_BASE_URL') ?: '', JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT); ?>,
      embedMode: <?php echo $isEmbed ? 'true' : 'false'; ?>,
      messageMaxChars: <?php echo $maxMessageChars; ?>
    };
    window.COMPASS_CHAT_MESSAGE_MAX_CHARS = <?php echo $maxMessageChars; ?>;
  </script>
</head>

<body class="bg-primary text-text-primary min-h-screen flex items-center justify-center<?php echo $isEmbed ? ' embed-mode' : ''; ?>">
  <?php if (!$isEmbed): ?>
    <?php include __DIR__ . '/left-sidebar.php'; ?>
  <?php endif; ?>

  <main
    id="main"
    class="max-w-[1440px] mx-auto px-[20px] md:px-[32px] lg:px-[44px]
         min-w-0 w-full min-h-screen flex flex-col items-center justify-start">
    <!-- Top-corner nav: fixed at 20px from top + left/right corners per Figma.
         pointer-events-none on the bar so the center gap doesn't block clicks
         to content beneath; each interactive child opts back in.
         The `top-corner-nav` class is used by the activity-log shift rule in
         input.css — when body.activity-log-open is set, the right edge slides
         left so icons don't sit on top of the open sidebar. -->
    <nav class="top-corner-nav fixed top-0 left-0 right-0 z-20 flex justify-between p-[20px] pointer-events-none">
      <?php if (!$isEmbed): ?>
        <button class="pointer-events-auto p-2 h-12 w-12 flex items-center justify-center toggleLeftSidebarBtn" aria-label="Toggle sidebar" aria-controls="leftSidebar" aria-expanded="false">
          <?php include __DIR__ . '/assets/icons/menu.svg'; ?>
        </button>
        <div class="pointer-events-auto flex gap-2">
          <button class="p-2 h-12 w-12 flex items-center justify-center downloadConversationBtn disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
                  aria-label="Download conversation as ZIP"
                  title="Download conversation (.zip)"
                  disabled>
            <?php include __DIR__ . '/assets/icons/download.svg'; ?>
          </button>
          <button class="p-2 h-12 w-12 flex items-center justify-center toggleActivityLogBtn" aria-label="Toggle activity log" aria-controls="activityLogSidebar" aria-expanded="false">
            <?php include __DIR__ . '/assets/icons/history.svg'; ?>
          </button>
          <button onclick="toggleFullscreen()" class="p-2 h-12 w-12 flex items-center justify-center fullscreenBtn" aria-label="Toggle fullscreen">
            <span class="fullscreen-icon-expand"><?php include __DIR__ . '/assets/icons/expand.svg'; ?></span>
            <span class="fullscreen-icon-minimize hidden"><?php include __DIR__ . '/assets/icons/minimize.svg'; ?></span>
          </button>
        </div>
      <?php else: ?>
        <div></div>
        <a href="/" target="_blank" rel="noopener noreferrer"
           class="pointer-events-auto p-2 h-12 flex items-center gap-1.5 text-white/60 hover:text-white text-sm transition-colors"
           aria-label="Open in new tab">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
            <polyline points="15 3 21 3 21 9"/>
            <line x1="10" y1="14" x2="21" y2="3"/>
          </svg>
          <span class="hidden md:inline">Open full version</span>
        </a>
      <?php endif; ?>
    </nav>
    <!-- Top spacing: 98px = 20 (nav top offset) + 48 (icon button height) + 30 (gap to headline).
         Dialled back from 148→108→98 to bring the headline closer to the corner icons. -->
    <div id="chatWrapper" class="flex flex-col w-full min-h-0 mt-[98px]">

      <div class="flex w-full">
        <div id="promptSection">

        </div>
      </div>

      <!-- Headline -->
      <section class="text-center max-w-prompt mx-auto space-y-md mb-xl" id="headline">
        <h3 class="font-serif font-regular text-headline-sm md:text-headline">
          <span class="text-text-accent">Hello.</span><br />
          What can I help you with?
        </h3>
        <p class="text-base text-white max-w-[656px] mx-auto">
          Compass answers questions about teacher policies in NCTQ's covered
          districts using 2024-2025 academic year data. Ask about specific
          districts, compare peers, or reference NCTQ research.
        </p>
      </section>

      <!-- Chat messages — NOTE: keep the opening + closing tags on a single
           line with NO whitespace between them so `#chat:empty` matches on
           landing (any newline/indent in between would create a text node
           that defeats :empty). -->
      <div id="chat" role="log" aria-live="polite" aria-relevant="additions" aria-label="Conversation" class=" min-h-0 space-y-[32px] md:space-y-xl max-w-prompt w-full overflow-y-auto mx-auto"></div>

      <!-- Prompt -->
      <section class="max-w-prompt w-full mx-auto">
        <form
          id="form"
          class="flex items-center w-full border border-white/20 p-lg font-sans gap-2 transition-colors">

          <div class="flex-shrink-0" id="logoWrapper">
            <img src="/assets/icons/compass.svg" alt="Compass" class="w-9 h-9">
          </div>


          <input
            type="text"
            id="input"
            placeholder="Ask anything about district teacher contracts"
            aria-label="Ask anything about district teacher contracts"
            class="flex-1 min-w-0 bg-transparent text-body placeholder-on-dark-subtle focus:outline-none"
            autocomplete="off"
            maxlength="<?php echo $maxMessageChars; ?>" />

          <button
            type="submit"
            aria-label="Send message"
            class="flex-shrink-0 bg-white text-text-chart font-semibold text-button w-[50px] h-[50px] flex justify-center items-center md:hidden submit-btn transition-opacity duration-200 ease-in-out disabled:opacity-50 disabled:cursor-not-allowed">
            <div>
              <svg width="9" height="14" viewBox="0 0 9 14" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M1.27504 14L9 7L1.27504 3.37669e-07L-5.43219e-07 1.5726L5.99008 7L-6.87404e-08 12.4274L1.27504 14Z" fill="#151515"/>
</svg>
            </div>
          </button>

          <button
            type="submit"
            class="flex-shrink-0 bg-white text-text-chart font-semibold text-button flex justify-center px-6 py-4 items-center hidden md:block submit-btn transition-opacity duration-200 ease-in-out disabled:opacity-50 disabled:cursor-not-allowed">
            Ask Compass
          </button>

          <button
            type="button"
            data-role="stop"
            aria-label="Stop generating"
            class="flex-shrink-0 bg-white text-text-chart font-semibold text-button w-[50px] h-[50px] flex justify-center items-center stop-btn transition-opacity duration-200 ease-in-out hidden">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect x="2" y="2" width="10" height="10" rx="1" fill="#151515"/>
            </svg>
          </button>
        </form>
        
        <!-- Character Counter -->
        <div class="flex justify-end mt-2 pr-1">
          <span id="charCounter" class="text-xs text-on-dark-muted transition-all duration-200">
            <span id="charCount">0</span><span class="text-on-dark-subtle">/<?php echo $maxMessageChars; ?></span>
          </span>
        </div>
      </section>
    </div>
    <?php include __DIR__ . '/sample-questions.php'; ?>

    <!-- Beta Disclaimer — kept visually close to the prompt form so it reads
         as part of the chat module rather than floating disclaimer copy at
         the bottom of the page. mt-lg = 24px gap above so it sits just under
         the 0/2000 counter (chat-active) or just under the sample-question
         cards (landing). mb-[40px] keeps a clear gap below to the viewport
         bottom. Applied as margins on the disclaimer itself so it travels
         with whatever's above it through both layout states.
         Text uses on-dark-subtle (#9DB0CC, ~6:1 on navy) — the dimmest brand
         token that still clears WCAG AA for this legal small-print. (The Figma's
         #7A91B2 was only 4.18:1 and failed AA — see audit 2026-06-22.) -->
    <p class="text-xs text-on-dark-subtle text-center mt-lg mb-[40px] max-w-prompt mx-auto leading-relaxed">
      Compass is currently in beta and, like all AI tools, may occasionally generate responses that are incomplete or imprecise. We encourage users to consult the cited sources to verify information. If you notice any issues, please contact us at <a href="mailto:policypathfinder@nctq.org" class="underline hover:text-on-dark-strong">policypathfinder@nctq.org</a>.
    </p>
  </main>
  <?php if (!$isEmbed): ?>
    <?php include __DIR__ . '/right-sidebar.php'; ?>
  <?php endif; ?>
  <script type="module" src="./assets/js/app.js?v=20260622a"></script>
  <?php if (!$isEmbed): ?>
  <script>
    function toggleFullscreen() {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen();
      } else {
        document.exitFullscreen();
      }
    }
    // Swap the expand / minimize glyphs on fullscreen state change so the
    // single button reads as the inverse action while in fullscreen.
    document.addEventListener('fullscreenchange', () => {
      const expand = document.querySelector('.fullscreen-icon-expand');
      const minimize = document.querySelector('.fullscreen-icon-minimize');
      if (!expand || !minimize) return;
      const inFs = !!document.fullscreenElement;
      expand.classList.toggle('hidden', inFs);
      minimize.classList.toggle('hidden', !inFs);
    });
  </script>
  <?php endif; ?>
</body>

</html>
