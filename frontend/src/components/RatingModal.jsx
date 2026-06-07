import { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";

/**
 * RatingModal — Full-screen overlay for rating a delivered order.
 *
 * Shows:
 *   - Order summary (store, delivery date, item count + total)
 *   - Per-item feedback with product image, brand, size, tags + thumbs up/down
 *   - Free text input ("How can I improve?")
 *   - "Send Feedback" button to send text to LLM
 *   - LLM response replaces textarea (collapsible, tap to re-edit)
 *   - "Finalize Rating" button to submit everything
 *
 * Closing (X) preserves state — user can come back anytime.
 *
 * Props:
 *   open              – boolean
 *   order             – the delivered order object from orderTracking
 *   itemFeedback      – {orderItemId: "thumbs_up"|"thumbs_down"} (persistent state from parent)
 *   feedbackText      – string (persistent state from parent)
 *   llmResponse       – string or null (from WebSocket)
 *   onItemFeedback    – (orderItemId, feedback) => void
 *   onFeedbackText    – (text) => void
 *   onGetLlmFeedback  – () => void  (triggers LLM call)
 *   onFinalize        – () => void
 *   onClose           – () => void  (preserves state, user can return)
 */
export default function RatingModal({
  open,
  order,
  itemFeedback,
  feedbackText,
  llmResponse,
  onItemFeedback,
  onFeedbackText,
  onGetLlmFeedback,
  onFinalize,
  onClose,
}) {
  const textareaRef = useRef(null);
  const [isRequestingLlm, setIsRequestingLlm] = useState(false);
  const [feedbackCollapsed, setFeedbackCollapsed] = useState(false);

  // Reset LLM request state when response arrives, collapse textarea
  useEffect(() => {
    if (llmResponse) {
      setIsRequestingLlm(false);
      setFeedbackCollapsed(true);
    }
  }, [llmResponse]);

  if (!open || !order) return null;

  const { order_id, store_name, order_ref, delivery_date, items = [], total_amount, item_count } = order;

  let deliveryLabel = "";
  if (delivery_date) {
    try {
      const d = new Date(delivery_date);
      deliveryLabel = d.toLocaleDateString("en-AE", {
        weekday: "long",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      deliveryLabel = delivery_date;
    }
  }

  const handleGetFeedback = () => {
    if (!feedbackText.trim()) return;
    setIsRequestingLlm(true);
    onGetLlmFeedback();
    trackEvent("rating_send_feedback", { orderId: order_id, textLength: feedbackText.length });
  };

  const handleFinalize = () => {
    onFinalize();
    trackEvent("rating_finalize", {
      orderId: order_id,
      itemsRated: Object.keys(itemFeedback).length,
      hasText: feedbackText.trim().length > 0,
      hasLlmResponse: !!llmResponse,
    });
  };

  // Color palette for item avatars (fallback when no image)
  const avatarColors = [
    "bg-amber-100 text-amber-700",
    "bg-sky-100 text-sky-700",
    "bg-emerald-100 text-emerald-700",
    "bg-violet-100 text-violet-700",
    "bg-rose-100 text-rose-700",
    "bg-cyan-100 text-cyan-700",
  ];

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 bg-surface flex flex-col"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface-card">
            <button
              type="button"
              onClick={onClose}
              className="w-10 h-10 flex items-center justify-center rounded-xl bg-chip-bg text-text-primary hover:bg-border transition-colors cursor-pointer"
            >
              <svg width="20" height="20" viewBox="0 0 16 16" fill="none">
                <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
            <h1 className="text-base font-semibold text-text-primary">Rate Your Order</h1>
            <div className="w-10" /> {/* Spacer for centering */}
          </div>

          {/* Scrollable content */}
          <div className="flex-1 overflow-y-auto">
            <div className="max-w-lg mx-auto px-4 py-5">
              {/* Order summary */}
              <div className="mb-6">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-semibold text-text-primary">{store_name}</span>
                </div>
                {deliveryLabel && (
                  <p className="text-xs text-text-secondary">Delivered {deliveryLabel}</p>
                )}
                <p className="text-xs text-text-tertiary mt-0.5">
                  {item_count || items.length} items
                  {total_amount > 0 && <> &middot; AED {Number(total_amount).toFixed(2)}</>}
                </p>
              </div>

              {/* Per-item feedback */}
              <div className="mb-6">
                <h2 className="text-sm font-semibold text-text-primary mb-3">Items</h2>
                <div className="flex flex-col gap-1.5">
                  {items.map((item, idx) => {
                    const feedback = itemFeedback[item.order_item_id] || null;
                    const colorClass = avatarColors[idx % avatarColors.length];
                    const subtitle = [item.brand, item.size].filter(Boolean).join(" \u00b7 ");

                    return (
                      <div
                        key={item.order_item_id}
                        className="flex items-start gap-3 py-3 px-3 rounded-xl bg-surface-card border border-border"
                      >
                        {/* Product image / avatar */}
                        <div className={`w-11 h-11 rounded-lg flex-shrink-0 overflow-hidden flex items-center justify-center ${
                          item.image_url ? "bg-white dark:bg-surface-elevated" : colorClass
                        }`}>
                          {item.image_url ? (
                            <img
                              src={item.image_url}
                              alt={item.name}
                              className="w-full h-full object-cover"
                              onError={(e) => {
                                e.target.style.display = "none";
                                e.target.parentElement.classList.add(...colorClass.split(" "));
                                const span = document.createElement("span");
                                span.className = "text-sm font-semibold";
                                span.textContent = item.name.charAt(0).toUpperCase();
                                e.target.parentElement.appendChild(span);
                              }}
                            />
                          ) : (
                            <span className="text-sm font-semibold">
                              {item.name.charAt(0).toUpperCase()}
                            </span>
                          )}
                        </div>

                        {/* Item details */}
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-text-primary leading-snug">
                            {item.name}
                            {item.quantity > 1 && (
                              <span className="text-text-tertiary font-normal"> x{item.quantity}</span>
                            )}
                          </p>
                          {subtitle && (
                            <p className="text-xs text-text-tertiary mt-0.5">{subtitle}</p>
                          )}
                          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                            {item.origin_country && (
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium leading-none border bg-blue-50 text-blue-600 border-blue-200">
                                {item.origin_country}
                              </span>
                            )}
                            {item.is_organic && (
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium leading-none border bg-emerald-50 text-emerald-700 border-emerald-200">
                                Organic
                              </span>
                            )}
                            {item.price > 0 && (
                              <span className="text-[11px] text-text-tertiary tabular-nums ml-auto">
                                AED {Number(item.price).toFixed(2)}
                              </span>
                            )}
                          </div>
                        </div>

                        {/* Thumbs */}
                        <div className="flex flex-col items-center gap-1 flex-shrink-0 mt-0.5">
                          {/* Thumbs up */}
                          <button
                            type="button"
                            onClick={() => {
                              const newFeedback = feedback === "thumbs_up" ? null : "thumbs_up";
                              onItemFeedback(item.order_item_id, newFeedback);
                            }}
                            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-colors cursor-pointer ${
                              feedback === "thumbs_up"
                                ? "bg-emerald-100 text-emerald-600"
                                : "text-text-tertiary hover:text-emerald-600 hover:bg-emerald-50"
                            }`}
                          >
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                              <path
                                d="M4.5 7V14H2.5C1.94772 14 1.5 13.5523 1.5 13V8C1.5 7.44772 1.94772 7 2.5 7H4.5ZM4.5 7L7 2C7.55228 2 8.5 2.44772 8.5 3V5.5H12.382C13.1267 5.5 13.7 6.18564 13.5694 6.91589L12.6194 12.4159C12.5175 12.9965 12.0123 13.4286 11.4222 13.4286H4.5"
                                stroke="currentColor"
                                strokeWidth="1.25"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                fill={feedback === "thumbs_up" ? "currentColor" : "none"}
                              />
                            </svg>
                          </button>
                          {/* Thumbs down */}
                          <button
                            type="button"
                            onClick={() => {
                              const newFeedback = feedback === "thumbs_down" ? null : "thumbs_down";
                              onItemFeedback(item.order_item_id, newFeedback);
                            }}
                            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-colors cursor-pointer ${
                              feedback === "thumbs_down"
                                ? "bg-red-100 text-red-500"
                                : "text-text-tertiary hover:text-red-500 hover:bg-red-50"
                            }`}
                          >
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="rotate-180">
                              <path
                                d="M4.5 7V14H2.5C1.94772 14 1.5 13.5523 1.5 13V8C1.5 7.44772 1.94772 7 2.5 7H4.5ZM4.5 7L7 2C7.55228 2 8.5 2.44772 8.5 3V5.5H12.382C13.1267 5.5 13.7 6.18564 13.5694 6.91589L12.6194 12.4159C12.5175 12.9965 12.0123 13.4286 11.4222 13.4286H4.5"
                                stroke="currentColor"
                                strokeWidth="1.25"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                fill={feedback === "thumbs_down" ? "currentColor" : "none"}
                              />
                            </svg>
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Feedback section — collapsible */}
              {feedbackCollapsed && llmResponse ? (
                /* Collapsed: show LLM response, tap to re-edit */
                <div
                  onClick={() => setFeedbackCollapsed(false)}
                  className="mb-5 cursor-pointer"
                >
                  <h2 className="text-sm font-semibold text-text-primary mb-2">
                    Your feedback
                  </h2>
                  <div className="p-3 rounded-xl bg-accent/5 border border-accent/15">
                    <div className="flex items-start gap-2">
                      <div className="w-5 h-5 rounded-full bg-accent/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                        <svg width="10" height="10" viewBox="0 0 16 16" fill="none" className="text-accent">
                          <path d="M8 1L10 5.5L15 6.5L11.5 10L12.5 15L8 12.5L3.5 15L4.5 10L1 6.5L6 5.5L8 1Z"
                            stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />
                        </svg>
                      </div>
                      <p className="text-sm text-text-primary leading-relaxed flex-1">{llmResponse}</p>
                    </div>
                    <p className="text-[11px] text-text-tertiary mt-2 text-center">Tap to edit your feedback</p>
                  </div>
                </div>
              ) : (
                /* Expanded: show textarea + Send Feedback */
                <div className="mb-5">
                  <h2 className="text-sm font-semibold text-text-primary mb-2">
                    How can I improve next order?
                  </h2>
                  <textarea
                    ref={textareaRef}
                    value={feedbackText}
                    onChange={(e) => onFeedbackText(e.target.value)}
                    placeholder="e.g. Get organic eggs next time, skip the Kraft cheese..."
                    rows={3}
                    name="hk-rating-note"
                    autoComplete="off"
                    autoCapitalize="sentences"
                    spellCheck={true}
                    data-1p-ignore="true"
                    data-lpignore="true"
                    data-form-type="other"
                    // text-base (16px) avoids mobile auto-zoom on focus.
                    className="w-full px-3 py-2.5 rounded-xl border border-border bg-surface-card text-base text-text-primary placeholder-text-tertiary resize-none focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent"
                  />
                  {feedbackText.trim() && (
                    <button
                      type="button"
                      onClick={handleGetFeedback}
                      disabled={isRequestingLlm}
                      className="mt-2 flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium bg-chip-bg text-text-primary hover:bg-border transition-colors cursor-pointer disabled:opacity-50"
                    >
                      {isRequestingLlm ? (
                        <>
                          <span className="w-3 h-3 border-2 border-text-tertiary border-t-transparent rounded-full animate-spin" />
                          Sending...
                        </>
                      ) : (
                        <>
                          <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                            <path d="M14 2L7 9M14 2L9.5 14L7 9M14 2L2 6.5L7 9" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                          {llmResponse ? "Resend Feedback" : "Send Feedback"}
                        </>
                      )}
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Bottom action bar — just Finalize, full width */}
          <div className="border-t border-border bg-surface-card px-4 py-3">
            <div className="max-w-lg mx-auto">
              <button
                type="button"
                onClick={handleFinalize}
                className="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl text-sm font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <path d="M3 8L6.5 11.5L13 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                Finalize Rating
              </button>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
