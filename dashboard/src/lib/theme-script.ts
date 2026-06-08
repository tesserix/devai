/**
 * The inline, render-blocking theme-init script.
 *
 * It is kept in one place so two consumers stay in lockstep:
 *   1. app/layout.tsx injects it via dangerouslySetInnerHTML, and
 *   2. next.config.ts hashes this exact string for the CSP script-src allowlist
 *      (DASH-3). If the script text changes, the hash recomputes from the same
 *      source — they can never drift.
 *
 * It reads the 'devai-theme' localStorage key (the single standardized key) and
 * falls back to the OS preference, applying `.dark` before first paint to avoid
 * a flash.
 */
export const THEME_INIT_SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem('devai-theme');
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var useDark = stored ? stored === 'dark' : prefersDark;
    if (useDark) document.documentElement.classList.add('dark');
  } catch (e) {}
})();
`.trim();
