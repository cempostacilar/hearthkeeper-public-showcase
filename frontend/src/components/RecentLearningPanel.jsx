import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";

/**
 * RecentLearningPanel — surfaces the Tier-1/Tier-3 ladder state.
 *
 * Two row types:
 *   - cadence: items where pattern_detector pushed
 *     restock_rules.cadence_adjustment > 1.0 (a silent nudge)
 *   - stops:   items with an active user_item_prefs.stop_suggesting row
 *
 * Tapping "Reset" calls back into onReset(itemId, kind), which the
 * App wires to sendAction("reset_preference", ...). The backend:
 *   - resets cadence_adjustment to 1.0
 *   - deactivates BOTH auto and user-confirmed stop_suggesting rows
 *   - re-broadcasts the updated learning_items + list data
 *
 * Collapsed by default so it doesn't dominate the home view. The
 * header always shows the count so the user knows it's there.
 */
export default function RecentLearningPanel({ learningItems, onReset }) {
  const [expanded, setExpanded] = useState(false);

  const cadence = learningItems?.cadence_adjustments || [];
  const stops = learningItems?.stops || [];
  const total = cadence.length + stops.length;

  if (total === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-surface-card rounded-xl border border-border mb-3"
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left"
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2 text-xs">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="text-text-secondary flex-shrink-0">
            <path d="M2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8C14 11.3137 11.3137 14 8 14"
                  stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
            <path d="M8 5V8L10 9.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="text-text-primary font-medium">
            I've been learning ({total})
          </span>
        </div>
        <svg
          width="12"
          height="12"
          viewBox="0 0 16 16"
          fill="none"
          className={`text-text-tertiary transition-transform ${expanded ? "rotate-180" : ""}`}
        >
          <path d="M4 6L8 10L12 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-3 pt-1 flex flex-col gap-1.5">
              {stops.map((s) => (
                <LearningRow
                  key={`stop-${s.item_id}`}
                  itemName={s.item_name}
                  description={
                    s.source === "user"
                      ? "Stopped (you confirmed)"
                      : s.source === "auto"
                      ? "Stopped (repeated drops)"
                      : "Stopped (manual)"
                  }
                  onReset={() => onReset(s.item_id, "stop")}
                  tone={s.source === "user" ? "stop-user" : "stop-auto"}
                />
              ))}
              {cadence.map((c) => (
                <LearningRow
                  key={`cad-${c.item_id}`}
                  itemName={c.item_name}
                  description={`Cadence pushed ${formatMultiplier(c.multiplier)}`}
                  onReset={() => onReset(c.item_id, "cadence")}
                  tone="cadence"
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function LearningRow({ itemName, description, onReset, tone }) {
  const toneStyles = {
    "stop-user": "border-l-2 border-l-amber-400",
    "stop-auto": "border-l-2 border-l-orange-300",
    cadence: "border-l-2 border-l-blue-300",
  };
  return (
    <div className={`flex items-center justify-between gap-2 py-1.5 pl-2 pr-1 ${toneStyles[tone] || ""}`}>
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium text-text-primary truncate">{itemName}</p>
        <p className="text-[10px] text-text-tertiary">{description}</p>
      </div>
      <button
        type="button"
        onClick={onReset}
        className="text-[10px] text-text-secondary hover:text-text-primary border border-border rounded-full px-2 py-0.5 flex-shrink-0"
      >
        Reset
      </button>
    </div>
  );
}

function formatMultiplier(m) {
  if (!m && m !== 0) return "";
  const n = Number(m);
  if (Number.isNaN(n)) return "";
  // 1.25 -> "1.25x", 1.5 -> "1.5x"
  return `${n.toFixed(n === Math.floor(n) ? 0 : 2).replace(/0+$/, "").replace(/\.$/, "")}x`;
}
