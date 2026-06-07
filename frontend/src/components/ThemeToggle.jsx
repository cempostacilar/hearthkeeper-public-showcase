// build-trigger: 1779726588
import { useEffect, useState } from "react";

/**
 * Theme toggle button.
 *
 * Light/dark mode toggle. The actual theme is a `dark` class on
 * <html>; CSS variables in styles.css under html.dark provide the dark
 * palette. Preference is persisted to localStorage as "hk-theme".
 *
 * The initial class is applied by an inline script in index.html
 * BEFORE React mounts, so first paint is correct (no light-mode flash).
 * This component just keeps the class in sync with user clicks.
 */
export default function ThemeToggle({ className = "" }) {
  // We read the current state from the DOM rather than re-deriving from
  // localStorage, so we agree with whatever the index.html script did.
  const [isDark, setIsDark] = useState(() =>
    typeof document !== "undefined" &&
    document.documentElement.classList.contains("dark")
  );

  // Keep DOM and localStorage in sync whenever the state toggles.
  useEffect(() => {
    const root = document.documentElement;
    if (isDark) {
      root.classList.add("dark");
    } else {
      root.classList.remove("dark");
    }
    try {
      localStorage.setItem("hk-theme", isDark ? "dark" : "light");
    } catch (e) {
      // localStorage disabled (e.g. Safari private mode) -- fall back
      // to the per-tab state only.
    }
  }, [isDark]);

  // Optional: live-update if the user changes the OS preference while
  // they haven't explicitly chosen a theme yet. We respect the saved
  // preference once they've toggled.
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const saved = (() => {
      try { return localStorage.getItem("hk-theme"); } catch { return null; }
    })();
    if (saved !== null) return; // user has a saved preference; don't override

    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e) => setIsDark(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  return (
    <button
      type="button"
      onClick={() => setIsDark((v) => !v)}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      className={
        "inline-flex items-center justify-center w-9 h-9 rounded-full " +
        "bg-surface-card border border-border text-text-secondary " +
        "hover:text-text-primary hover:border-accent/40 transition-colors " +
        "shadow-sm " +
        className
      }
    >
      {isDark ? (
        // Sun icon -- click to switch to light
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      ) : (
        // Moon icon -- click to switch to dark
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}
