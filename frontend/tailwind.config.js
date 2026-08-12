/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./public/**/*.php", "./public/**/*.js"],
  // Safelist for all sidebar classes to ensure they're included in production build
  safelist: [
    // Sidebar toggle classes
    'min-w-80', 'max-w-80', 'w-0', 'hidden',
    // Position & layout
    'absolute', 'relative', 'fixed', 'left-0', 'right-0', 'inset-0',
    'z-10', 'z-20', 'z-30', 'z-40', 'z-50',
    // Sizing
    'h-screen', 'h-20', 'h-8', 'w-8', 'w-5', 'h-5', 'w-full',
    // Flexbox
    'flex', 'flex-col', 'flex-1', 'flex-shrink-0', 'items-center', 'items-start',
    'justify-between', 'justify-center', 'justify-end',
    'gap-2', 'gap-4', 'space-y-2', 'space-y-3',
    // Spacing
    'p-1', 'p-2', 'p-4', 'px-2', 'px-4', 'px-6', 'py-4', 'pt-2', 'mx-4', 'mb-2', 'mt-1',
    // Typography
    'text-white', 'text-xs', 'text-sm', 'font-bold', 'font-semibold', 'text-button',
    'text-text-chart', 'text-slate-300', 'text-slate-400',
    // Semantic text tokens (assembled dynamically in JS — keep safelisted)
    'text-on-dark-strong', 'text-on-dark-muted', 'text-on-dark-subtle',
    'text-on-light-primary', 'text-on-light-secondary', 'text-on-light-muted',
    'text-status-success', 'text-status-success-on-dark',
    'text-status-error', 'text-status-error-on-dark',
    'hover:text-on-dark-strong', 'hover:text-on-dark-muted',
    // Colors & backgrounds  
    'bg-white', 'bg-black/50', 'bg-[#0F223A]', 'bg-[#0F223D]',
    'text-[#003057]', 'border-[#004a7c]',
    // Borders & effects
    'border-t', 'shadow-lg', 'overflow-hidden', 'overflow-y-auto',
    // Transitions
    'transition-all', 'duration-300',
    // Interactive
    'cursor-pointer', 'block',
    // Responsive
    'sm:shadow-none', 'sm:hidden', 'md:hidden', 'md:block',
    // Hover states
    'hover:bg-slate-100',
    // Rate limit error styling
    'text-text-accent', 'rounded-lg', 'bg-[#1B3862]',
    // Embed mode
    'embed-mode',
  ],
  theme: {
    extend: {
      colors: {
        primary: "#182F50",
        /* Text */
        text: {
          primary: "#FFFFFF",
          chart: "#0F223D",
          placeholder: "#7A91B2",
          accent: "#F68E1E",
        },

        /* Semantic text tokens — WCAG AA on the named surface.
           Use `on-dark-*` on the navy chat (#182F50). Use `on-light-*` on
           white/paper surfaces. Values mirror the dashboard's theme.py. */
        "on-dark": {
          strong: "#E8EEF7",   // 11.3:1 on navy — high-priority secondary
          muted: "#B6C5DD",    // 7.5:1 on navy — hints, helpers, metadata
          subtle: "#9DB0CC",   // 6.0:1 on navy — disabled / tertiary
        },
        "on-light": {
          primary: "#191C1D",   // DESIGN.md on_surface
          secondary: "#414754", // DESIGN.md on_surface_variant
          muted: "#64748B",     // matches dashboard --text-muted
        },

        /* Status colors. `*-on-dark` variants are brighter so they pass
           AA on the navy chat surface; the unsuffixed values pass AA on
           white/paper. */
        status: {
          success: "#0B8A00",
          "success-on-dark": "#5EE6A8",
          error: "#C62828",
          "error-on-dark": "#FF8787",
        },

        /* Form & UI outlines */
        outline: {
          normal: "#9DA4AE",
          focus: "#FFFFFF",
          dark: "#1B3862",
          light: "#9DA4AE",
        },

        /* Error states */
        error: {
          DEFAULT: "#FB6262",
          text: "#c5bebeff",
        },
      },
      fontFamily: {
        serif: ['"Leitura News"', "serif"],
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      fontSize: {
        // Hero headline ("Hello." / "What can I help you with?") — dialled
        // back from 48px after a side-by-side comparison felt too heavy
        // against the prompt + description rhythm.
        headline: ["40px", { lineHeight: "1.2" }],
        "headline-sm": ["26px", { lineHeight: "1.2" }],

        body: ["18px", { lineHeight: "1.6" }],
        paragraph: ["18px", { lineHeight: "1.4" }],

        // "Ask Compass" submit button stays 18px so it reads close to the
        // prompt input weight. Sample-question copy is 16px per the NCTQ
        // mobile-QA review (2026-06-22) — 18px crowded the cards on mobile.
        button: ["18px", { lineHeight: "1.2", letterSpacing: "1px" }],
        sample: ["16px", { lineHeight: "1.5" }],

        answer: ["24px", { lineHeight: "1.2" }],
      },
      fontWeight: {
        regular: 400,
        medium: 500,
        semibold: 600,
      },
      spacing: {
        xs: "4px",
        sm: "8px",
        md: "16px",
        lg: "24px",

        xl: "40px",
        "2xl": "64px",
        "3xl": "80px",
        "4xl": "120px",
      },
      maxWidth: {
        prompt: "888px",
        body: "800px",
      },
    },
    screens: {
      sm: "375px",
      md: "768px",
      lg: "1150px",
      xl: "1440px",
    },
  },
  plugins: [],
}

