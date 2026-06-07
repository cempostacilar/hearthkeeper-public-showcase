import { useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";

/**
 * BuildingBasket -- a tiny lively animation for the cart-building
 * state. A basket icon bobs while colored dots (one per processed
 * item) drop in from above. Green = added, red = failed. Stays in
 * sync with the actual progress so it feels like the agent is
 * really doing the work, not a generic spinner.
 */
function BuildingBasket({ ok, fail, total }) {
  // Render up to 5 visible dots so we don't overflow on big carts.
  const processed = ok + fail;
  const maxDots = 5;
  const dotCount = total > 0 ? Math.min(processed, maxDots) : Math.min(processed, maxDots);
  return (
    <div className="relative w-9 h-9 flex-shrink-0" aria-hidden="true">
      {/* Basket -- gentle bob */}
      <motion.svg
        viewBox="0 0 24 24"
        className="absolute inset-0 w-full h-full text-accent"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        animate={{ y: [0, -1, 0], rotate: [0, -2, 2, 0] }}
        transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut" }}
      >
        {/* Cart body */}
        <path d="M3 7h2.5l2.2 9.5h11l2.3-7.5H7" strokeLinecap="round" strokeLinejoin="round" />
        {/* Wheels */}
        <circle cx="10" cy="20" r="1.3" />
        <circle cx="17" cy="20" r="1.3" />
      </motion.svg>

      {/* Items dropping in. Indexes 0..maxDots-1; each shows up only
          once `processed` reaches its index. Color picks failure vs.
          success based on order observed. */}
      <AnimatePresence>
        {Array.from({ length: dotCount }).map((_, i) => {
          // Mark the i-th dot as failure if i is within the "fail" range
          // (failures show as the rightmost reds). Simple approximation;
          // matches the actual counts within ~1 item.
          const isFail = i >= ok;
          return (
            <motion.span
              key={i}
              initial={{ y: -14, opacity: 0, scale: 0.6 }}
              animate={{ y: 8, opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.5, type: "spring", damping: 14, delay: i * 0.05 }}
              className="absolute top-0 w-1.5 h-1.5 rounded-full"
              style={{
                left: 9 + (i - 2) * 3,
                background: isFail ? "#ef4444" : "#10b981",
              }}
            />
          );
        })}
      </AnimatePresence>
    </div>
  );
}

/**
 * Build a human-readable status line from the latest cart progress step.
 */
function progressLabel(steps) {
  if (!steps || steps.length === 0) return "Starting cart builder...";

  const last = steps[steps.length - 1];

  switch (last.step) {
    case "starting":
      return "Launching browser...";

    case "login_check":
      if (last.status === "checking") return `Checking ${last.store} login...`;
      if (last.status === "ok") return `${last.store} logged in`;
      return `${last.store}: not logged in`;

    case "login_required":
      return last.message || `Not logged in to ${(last.stores || []).join(", ")}`;

    case "item_start":
      return `Adding ${last.item}... (${last.index}/${last.total})`;

    case "item_ok":
      return `Added ${last.item}`;

    case "item_fail":
      return `Failed: ${last.item} — ${last.reason || "error"}`;

    case "verify":
      return `Verifying ${last.store} cart...`;

    case "done":
      if (last.total_added === 0 && last.total_failed === 0)
        return last.summary || "Cart building failed";
      return `Done! ${last.total_added} added, ${last.total_failed} failed`;

    default:
      return last.message || "Building...";
  }
}

/**
 * Count successes and failures from progress steps.
 */
function progressCounts(steps) {
  let ok = 0;
  let fail = 0;
  let total = 0;
  for (const s of steps) {
    if (s.step === "item_ok") ok++;
    if (s.step === "item_fail") fail++;
    if (s.step === "item_start" && s.total) total = s.total;
  }
  return { ok, fail, total };
}

export default function StickyActionBar({
  onOrderAction,
  onOpenChat,
  agentState,
  lastMessage,
  cartProgress = [],
  diff = null,
  listState = null,
  activeView = "suggestions",
  hasCart = false,
  hasItems = false,
  itemCount = 0,
  onDismissCart,
  onCreateCart,
  whatsappEnabled = false,
  feedbackRound = null,
  onRequestFeedback,
}) {
  const isBuilding = agentState === "building";
  const hasChanges = diff?.has_changes;
  const isAllCaughtUp = listState === "all_caught_up";
  const label = useMemo(() => progressLabel(cartProgress), [cartProgress]);
  const counts = useMemo(() => progressCounts(cartProgress), [cartProgress]);
  const pct = counts.total > 0 ? ((counts.ok + counts.fail) / counts.total) * 100 : 0;

  // Hide entire sticky bar when all caught up — body has the primary CTA
  if (isAllCaughtUp) return null;

  // Verb + scope: tells the user what will happen AND how big the action
  // is before they commit. "Build carts" was ambiguous because the cart
  // tab itself is called Cart -- people read "build cart from cart" and
  // got confused. "Send to stores" is concrete about the destination.
  const isCartView = activeView === "cart";
  const itemWord = itemCount === 1 ? "item" : "items";
  const ctaLabel = itemCount > 0
    ? `Send to stores (${itemCount} ${itemWord})`
    : "Send to stores";
  // The primary CTA is the only filled button -- everything else is
  // visually quieter so the hierarchy is unambiguous.
  const ctaClass = "bg-accent text-white hover:bg-accent/90 shadow-sm";

  return (
    <div className="fixed bottom-0 left-0 right-0 z-30 bg-surface-card border-t border-border">
      <div className="max-w-lg mx-auto px-4 py-3">
        {isBuilding ? (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className="mb-2.5 px-1"
            >
              {/* Current step label + cute basket fill animation.
                  The basket bobs while items pop in from above. Items
                  are green when added OK, red when failed -- the
                  little visualisation reads at a glance even when the
                  text label is mid-word. */}
              <div className="flex items-center gap-2.5 mb-2">
                <BuildingBasket
                  ok={counts.ok}
                  fail={counts.fail}
                  total={counts.total}
                />
                <span className="text-sm font-medium text-text-primary truncate">
                  {label}
                </span>
              </div>

              {/* Progress bar — only show when we know the total */}
              {counts.total > 0 && (
                <div className="w-full h-1.5 rounded-full bg-chip-bg overflow-hidden">
                  <motion.div
                    className="h-full rounded-full bg-accent"
                    initial={{ width: 0 }}
                    animate={{ width: `${pct}%` }}
                    transition={{ duration: 0.3, ease: "easeOut" }}
                  />
                </div>
              )}

              {/* Counter */}
              {counts.total > 0 && (
                <div className="flex items-center gap-3 mt-1.5 text-xs text-text-tertiary">
                  <span className="tabular-nums">
                    {counts.ok + counts.fail}/{counts.total} items
                  </span>
                  {counts.ok > 0 && (
                    <span className="text-emerald-600 tabular-nums">{counts.ok} added</span>
                  )}
                  {counts.fail > 0 && (
                    <span className="text-status-error tabular-nums">{counts.fail} failed</span>
                  )}
                </div>
              )}
            </motion.div>
          ) : (
            <div className="flex items-center gap-2 mb-2.5">
              {hasItems ? (
                <>
                  {/* Primary CTA — only filled button on the bar. */}
                  <motion.button
                    type="button"
                    whileTap={{ scale: 0.95 }}
                    onClick={() => {
                      onOrderAction("ready");
                      trackEvent("order_action", { action: "ready", view: activeView });
                    }}
                    aria-label={`Send ${itemCount} ${itemWord} to Carrefour and Kibsons`}
                    className={`px-4 py-2 rounded-full text-sm font-medium transition-colors cursor-pointer min-h-[44px] ${ctaClass}`}
                  >
                    {ctaLabel}
                  </motion.button>

                  {/* Send to family for review.
                      Kept emerald to read as "external action" -- this
                      message LEAVES the app and goes to other people's
                      phones, which is meaningful. Border + bg toned a
                      step down so the primary CTA above stays dominant.
                  */}
                  {whatsappEnabled && !feedbackRound && onRequestFeedback && (
                    <motion.button
                      type="button"
                      whileTap={{ scale: 0.95 }}
                      initial={{ opacity: 0, scale: 0.9 }}
                      animate={{ opacity: 1, scale: 1 }}
                      onClick={() => {
                        onRequestFeedback();
                        trackEvent("order_action", { action: "request_feedback" });
                      }}
                      aria-label="Send the current list to family for review via WhatsApp"
                      className="px-3 py-2 rounded-full text-sm font-medium transition-colors cursor-pointer bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200 flex items-center gap-1.5 min-h-[44px]"
                    >
                      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                        <path d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                      Ask family
                    </motion.button>
                  )}

                  {/* Undo changes / restart from suggestions.
                      Dropped the amber treatment -- amber reads as a
                      warning/destructive action, but reset is a quiet
                      escape hatch (data isn't lost). Now a subtle
                      text-style button so the primary CTA stays
                      unambiguous. Label depends on intent.
                  */}
                  {(hasChanges || isCartView) && (
                    <motion.button
                      type="button"
                      whileTap={{ scale: 0.95 }}
                      initial={{ opacity: 0, scale: 0.9 }}
                      animate={{ opacity: 1, scale: 1 }}
                      onClick={() => {
                        onOrderAction("reset");
                        trackEvent("order_action", { action: "reset", view: activeView });
                      }}
                      aria-label={hasChanges ? "Undo my changes and restore the suggested list" : "Restart this list from the original suggestions"}
                      className="px-3 py-2 rounded-full text-sm font-medium transition-colors cursor-pointer text-text-secondary hover:text-text-primary hover:bg-chip-bg min-h-[44px]"
                    >
                      {hasChanges ? "Undo changes" : "Restart"}
                    </motion.button>
                  )}

                  {/* Dismiss removed from curated tab per user request */}
                </>
              ) : isCartView && !hasCart && onCreateCart ? (
                /* Cart tab with no cart — show New Cart */
                <motion.button
                  type="button"
                  whileTap={{ scale: 0.95 }}
                  onClick={() => {
                    onCreateCart();
                    trackEvent("order_action", { action: "create_cart" });
                  }}
                  className="px-4 py-2 rounded-full text-sm font-medium transition-colors cursor-pointer bg-accent text-white hover:bg-accent/90"
                >
                  New Cart
                </motion.button>
              ) : null}
            </div>
          )}
        <button
          type="button"
          onClick={() => {
            onOpenChat();
            trackEvent("chat_open", { source: "sticky_bar" });
          }}
          className="flex items-center gap-2 w-full bg-chip-bg rounded-xl px-3.5 py-2.5 text-left cursor-pointer hover:bg-border transition-colors group"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="text-text-tertiary flex-shrink-0 group-hover:text-accent transition-colors">
            <path
              d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          {lastMessage ? (
            <span className="flex-1 text-sm text-text-secondary truncate">
              {lastMessage}
            </span>
          ) : (
            <span className="flex-1 text-sm text-text-tertiary">
              Message Hearthkeeper...
            </span>
          )}
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="text-text-tertiary flex-shrink-0">
            <path
              d="M4 6L8 10L12 6"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ transform: "rotate(180deg)", transformOrigin: "center" }}
            />
          </svg>
        </button>
      </div>
    </div>
  );
}
