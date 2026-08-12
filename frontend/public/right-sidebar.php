<div id="activityLogSidebar"
    role="complementary"
    aria-label="Activity log"
    class="w-0 fixed top-0 md:sticky md:self-start right-0 h-screen z-40 transition-all duration-300 bg-white border-l border-slate-200 flex flex-col overflow-hidden shadow-xl max-w-80">

    <div class="p-4 border-b border-slate-200 h-20 flex items-center">
        <div class="flex justify-between w-full text-primary">
            <h2 class="font-semibold text-xl">Activity Log</h2>
            <button class="p-1 hover:bg-slate-100 w-8 h-8 block toggleActivityLogBtn" aria-label="Close activity log">
                <svg class="w-5 h-5 text-[#003057]" xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true" focusable="false">
                    <path stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                        d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
        </div>
    </div>

    <div class="flex-1 overflow-y-auto p-4 space-y-4">

       <!-- Progress Card -->
<div id="progressCard" class="border border-slate-200 p-4 bg-white shadow-sm space-y-3">
    <h3 class="font-extrabold text-lg text-[#003057] border-b pb-2">
        Progress
    </h3>

    <div class="flex justify-between text-sm">
        <span id="statusLabel">Not started</span>
        <span id="progressPercent" class="font-semibold text-[#0089b3]">
            0%
        </span>
    </div>

    <div class="w-full bg-slate-100 rounded-full h-2.5" role="progressbar" aria-label="Answer progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div
            id="progressBar"
            class="h-2.5 rounded-full transition-all duration-500 bg-[#0089b3]"
            style="width: 0%">
        </div>
    </div>
</div>


        <!-- Activity Logs -->
        <div class="border border-slate-200 p-4 bg-white shadow-sm">
            <h3 class="font-extrabold text-lg text-[#003057] border-b pb-2 mb-3">
                Activity Logs
            </h3>

            <div id="activityLog" class="space-y-3 text-black" role="log" aria-live="polite" aria-relevant="additions"></div>
        </div>
    </div>
</div>

<div id="rightBackdrop"
     class="hidden fixed inset-0 bg-black/50 z-10 md:hidden"></div>
