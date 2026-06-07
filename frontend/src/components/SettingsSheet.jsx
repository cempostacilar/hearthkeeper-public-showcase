import { motion, AnimatePresence } from "framer-motion";
import { useEffect } from "react";
import ThemeToggle from "./ThemeToggle";

/**
 * Settings bottom sheet.
 *
 * Reached via a settings gear in the ConversationHeader. Hosts:
 *   - Appearance (theme switcher)
 *   - Preferences (A13 — full user_item_prefs memory view)
 *
 * The Preferences section lazy-loads when the sheet opens (App.jsx
 * triggers getPreferences on open). Each row has a one-tap
 * Deactivate that flips is_active=0 on the underlying row; the
 * backend re-pushes the snapshot so the row vanishes from the list.
 *
 * Stop_suggesting rows are co-surfaced in the Recently-learned
 * panel on the home view; appearing here too is intentional --
 * Settings is the "full memory" view, A12 is the "active learning"
 * view, and there's no harm in showing the same row in both.
 */
export default function SettingsSheet({
  open,
  onClose,
  preferences,
  onDeactivatePreference,
}) {
  // Esc to dismiss + lock background scroll while open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-surface-overlay"
            aria-hidden="true"
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Settings"
            initial={{ y: "100%" }}
            animate={{ y: 0 }}
            exit={{ y: "100%" }}
            transition={{ type: "spring", damping: 28, stiffness: 280 }}
            className="fixed bottom-0 left-0 right-0 z-50 max-w-lg mx-auto bg-surface-card rounded-t-2xl border-t border-border shadow-2xl pb-safe max-h-[90vh] overflow-y-auto"
          >
            {/* Grab handle */}
            <div className="flex justify-center pt-3 pb-2">
              <div className="w-10 h-1 rounded-full bg-border" />
            </div>

            <div className="px-5 pb-6">
              <div className="flex items-center justify-between mb-5">
                <h2 className="text-lg font-semibold text-text-primary">
                  Settings
                </h2>
                <button
                  type="button"
                  onClick={onClose}
                  aria-label="Close settings"
                  className="w-9 h-9 rounded-full flex items-center justify-center text-text-secondary hover:text-text-primary hover:bg-chip-bg transition-colors cursor-pointer"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                       strokeLinejoin="round" aria-hidden="true">
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
                </button>
              </div>

              {/* Theme */}
              <section className="mb-6">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-text-tertiary mb-3">
                  Appearance
                </h3>
                <div className="flex items-center justify-between rounded-xl bg-surface-elevated border border-border px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-text-primary">Theme</p>
                    <p className="text-xs text-text-secondary mt-0.5">
                      Light, dark, or follow your device.
                    </p>
                  </div>
                  <ThemeToggle />
                </div>
              </section>

              {/* Preferences (A13) */}
              <PreferencesSection
                preferences={preferences}
                onDeactivate={onDeactivatePreference}
              />
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

const PREF_TYPE_META = {
  stop_suggesting: {
    label: "Stopped suggesting",
    blurb: "Items the agent has been told to keep off lists.",
  },
  block_brand: {
    label: "Blocked brands",
    blurb: "Brands the agent will score down to -100.",
  },
  pin_product: {
    label: "Pinned products",
    blurb: "Specific catalog products the agent always picks.",
  },
  prefer_store: {
    label: "Preferred store",
    blurb: "Per-item store override.",
  },
  wrong_item: {
    label: "Wrong-item flags",
    blurb: "Products the agent picked that you flagged as wrong.",
  },
  tier2_asked: {
    label: "Tier-2 asks (informational)",
    blurb: "Items the agent has already asked about over WhatsApp.",
  },
};

const ORDERED_TYPES = [
  "stop_suggesting",
  "block_brand",
  "pin_product",
  "prefer_store",
  "wrong_item",
  "tier2_asked",
];

function PreferencesSection({ preferences, onDeactivate }) {
  const loading = preferences === null;
  const groups = preferences || {};
  // Order canonical types first, then anything else the backend sent
  // (defensive: a new pref_type added server-side will show up).
  const knownTypes = new Set(ORDERED_TYPES);
  const extraTypes = Object.keys(groups).filter((k) => !knownTypes.has(k));
  const sectionTypes = [...ORDERED_TYPES, ...extraTypes];

  const totalActive = Object.values(groups).reduce(
    (n, arr) => n + (Array.isArray(arr) ? arr.length : 0),
    0,
  );

  return (
    <section className="mb-2">
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
          Preferences
        </h3>
        {!loading && (
          <span className="text-[10px] text-text-tertiary">
            {totalActive} active
          </span>
        )}
      </div>

      {loading ? (
        <p className="text-xs text-text-tertiary py-4 text-center">
          Loading preferences...
        </p>
      ) : totalActive === 0 ? (
        <p className="text-xs text-text-tertiary py-3 text-center rounded-xl bg-surface-elevated border border-border">
          Nothing in memory yet — the agent learns as you use it.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {sectionTypes.map((t) => {
            const rows = groups[t] || [];
            if (rows.length === 0) return null;
            const meta = PREF_TYPE_META[t] || {
              label: t,
              blurb: "",
            };
            return (
              <PrefGroup
                key={t}
                label={meta.label}
                blurb={meta.blurb}
                rows={rows}
                prefType={t}
                onDeactivate={onDeactivate}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

function PrefGroup({ label, blurb, rows, prefType, onDeactivate }) {
  return (
    <div className="rounded-xl bg-surface-elevated border border-border overflow-hidden">
      <div className="px-4 py-2.5 border-b border-border">
        <p className="text-sm font-medium text-text-primary">
          {label}{" "}
          <span className="text-[10px] text-text-tertiary font-normal">
            ({rows.length})
          </span>
        </p>
        {blurb && (
          <p className="text-[11px] text-text-tertiary mt-0.5">{blurb}</p>
        )}
      </div>
      <div className="divide-y divide-border">
        {rows.map((r) => (
          <PrefRow
            key={r.id}
            row={r}
            prefType={prefType}
            onDeactivate={onDeactivate}
          />
        ))}
      </div>
    </div>
  );
}

function PrefRow({ row, prefType, onDeactivate }) {
  const tagLabel =
    prefType === "stop_suggesting" && row.source
      ? row.source === "user"
        ? "you confirmed"
        : row.source === "auto"
        ? "auto (repeated drops)"
        : "manual"
      : null;

  return (
    <div className="flex items-center justify-between gap-2 px-4 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium text-text-primary truncate">
          {row.item_name}
        </p>
        <p className="text-[10px] text-text-tertiary truncate">
          {tagLabel ? <span>{tagLabel} · </span> : null}
          {row.value || "—"}
        </p>
      </div>
      <button
        type="button"
        onClick={() => onDeactivate(row.id)}
        className="text-[10px] text-text-secondary hover:text-text-primary border border-border rounded-full px-2.5 py-0.5 flex-shrink-0"
      >
        Forget
      </button>
    </div>
  );
}
