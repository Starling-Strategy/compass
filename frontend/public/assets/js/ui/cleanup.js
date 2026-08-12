import { resetChatSize } from "./chat.js";

// Get DOM elements directly to avoid circular import with app.js
const form = document.getElementById("form");
const input = document.getElementById("input");

// Placeholder copy in one place — it was scattered across two functions and had
// drifted (the HTML default said "contracts", the reset said "policies").
// Desktop keeps the descriptive prompt; mobile collapses to the short brand
// prompt so it fits the narrow input (NCTQ mobile-QA review, 2026-06-22).
const PLACEHOLDER = {
  landing: {
    desktop: "Ask anything about district teacher contracts",
    mobile: "Ask Compass",
  },
  followup: {
    desktop: "Ask a followup question",
    mobile: "Ask Compass",
  },
};

// Guard `window` so importing this module never throws under the Node test
// runner (non-browser env); falls back to the desktop variant + a no-op
// listener there.
const mobileQuery =
  typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(max-width: 767px)")
    : { matches: false, addEventListener() {} };

// Landing until the first message lands; tracked so a viewport change
// (resize / phone rotation) re-applies the correct long/short variant.
let placeholderState = "landing";

function applyPlaceholder() {
  if (!input) return;
  const copy = PLACEHOLDER[placeholderState];
  input.placeholder = mobileQuery.matches ? copy.mobile : copy.desktop;
}

// Swap the long/short variant live when the viewport crosses the breakpoint.
mobileQuery.addEventListener("change", applyPlaceholder);

// Apply the correct landing variant immediately, so mobile shows "Ask Compass"
// instead of the HTML's desktop default before the first interaction.
applyPlaceholder();

export function hideSampleQuestions() {
  document.getElementById("sampleQuestions")?.classList.add("hidden");
}
export function showSampleQuestions() {
  document.getElementById("sampleQuestions")?.classList.remove("hidden");
}

export function hideHeadline() {
  document.getElementById("headline")?.classList.add("hidden");
}

export function showHeadline() {
  document.getElementById("headline")?.classList.remove("hidden");
}

export function hideBtnLabel(submitBtns) {
  Array.from(submitBtns).forEach((btn) => {
    // Clear existing content
    btn.textContent = "";
    btn.classList.remove("px-6");
    btn.classList.remove("py-4");

    btn.classList.add("p-2");

    form.classList.remove("border-white/20");
    form.classList.add("border-[#9DA4AE]");

        // Create image
    const iconDiv = document.createElement("div");
    iconDiv.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="1.04907e-06" y="24" width="24" height="24" transform="rotate(-90 1.04907e-06 24)"/>
<path d="M12 4L19 11.7246L17.4277 13L13 8.11328L13 20L11 20L11 8.11328L6.57227 13L5 11.7246L12 4Z" fill="#0F223D"/>
</svg>
`;
    btn.appendChild(iconDiv);

  });
}

export function resetBtnLabel(submitBtns) {
  Array.from(submitBtns).forEach((btn) => {
    // Clear existing content
    btn.textContent = "Ask Compass";

    btn.classList.remove("p-2");
    btn.classList.add("px-6");
    btn.classList.add("py-4");

    form.classList.remove("border-[#9DA4AE]");
    form.classList.add("border-white/20");

    // Remove any child images (like the arrow)
    const img = btn.querySelector("img");
    if (img) btn.removeChild(img);
  });
}


function updateInputPlaceholder() {
  placeholderState = "followup";
  applyPlaceholder();
}

function resetInputPlaceholder() {
  placeholderState = "landing";
  applyPlaceholder();
}

export function resetUIafterFirstMessage(submitBtns) {
  hideSampleQuestions();
  hideHeadline();
  hideBtnLabel(submitBtns);
  updateInputPlaceholder();
}

export function resetChatUi(submitBtns) {
  showSampleQuestions();
  showHeadline();
  resetBtnLabel(submitBtns);
  resetInputPlaceholder();
  resetChatSize();
}
