<div
  id="leftSidebar"
  role="complementary"
  aria-label="Conversation history"
  class=" fixed top-0 left-0 md:sticky md:self-start h-screen z-40
         transition-all duration-300
         bg-[#0F223A] text-white
         flex flex-col overflow-hidden
         shadow-lg sm:shadow-none
         w-0 max-w-80">

  <!-- Header -->
  <div class="flex gap-4 p-4 h-20 items-center justify-between">
    <button
      id="newConversationBtn"
      class="flex-shrink-0 bg-white text-text-chart font-semibold text-button flex justify-center px-6 py-4 items-center cursor-pointer">
      <span class="font-semibold">+ New Conversation</span>
    </button>
    <button class="p-1 w-8 h-8 block toggleLeftSidebarBtn" aria-label="Close sidebar">
                <!-- X Icon -->
                <svg class="w-5 h-5 text-[#003057]" xmlns="http://www.w3.org/2000/svg" fill="white"
                    viewBox="0 0 24 24" stroke="white" aria-hidden="true" focusable="false">
                    <path stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                        d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
  </div>

  <!-- Conversation list -->
  <div class="flex-1 overflow-y-auto p-4 space-y-2">
    <div class="text-xs font-semibold text-on-dark-strong mb-2 px-2">RECENT</div>
    <div id="conversationList"></div>
  </div>

  <!-- Footer -->
  <div class="py-4 mx-4 border-t border-[#004a7c] space-y-3">
    <div class="pt-2">
      <div class="text-sm font-bold">NCTQ Compass</div>
      <div class="text-xs mt-1 text-on-dark-muted">Powered by NCTQ</div>
    </div>
  </div>
</div>

<!-- Backdrop for left sidebar -->
<div
  id="leftBackdrop"
  class="fixed inset-0 bg-black/50 z-30 hidden md:hidden">
</div>