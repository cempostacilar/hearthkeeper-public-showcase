import { useRef, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";
import VendorLogo from "./VendorLogo";

/**
 * useDragScroll — enables mouse drag scrolling on desktop.
 * Returns handlers to spread on the scroll container.
 */
function useDragScroll(scrollRef) {
  const [isDragging, setIsDragging] = useState(false);
  const startX = useRef(0);
  const scrollLeft = useRef(0);

  const onMouseDown = useCallback((e) => {
    if (!scrollRef.current) return;
    setIsDragging(true);
    startX.current = e.pageX - scrollRef.current.offsetLeft;
    scrollLeft.current = scrollRef.current.scrollLeft;
    scrollRef.current.style.cursor = "grabbing";
    scrollRef.current.style.userSelect = "none";
  }, [scrollRef]);

  const onMouseMove = useCallback((e) => {
    if (!isDragging || !scrollRef.current) return;
    e.preventDefault();
    const x = e.pageX - scrollRef.current.offsetLeft;
    const walk = (x - startX.current) * 1.5;
    scrollRef.current.scrollLeft = scrollLeft.current - walk;
  }, [isDragging, scrollRef]);

  const onMouseUp = useCallback(() => {
    setIsDragging(false);
    if (scrollRef.current) {
      scrollRef.current.style.cursor = "grab";
      scrollRef.current.style.userSelect = "";
    }
  }, [scrollRef]);

  return { onMouseDown, onMouseMove, onMouseUp, onMouseLeave: onMouseUp, isDragging };
}

/* ─── Helpers ─────────────────────────────────────────────────── */

/** Converts 24h "07:00" / "13:00" → "7 AM – 1 PM" */
function formatTimeWindow(start, end) {
  const fmt = (t) => {
    const [h, m] = t.split(":").map(Number);
    const suffix = h >= 12 ? "PM" : "AM";
    const h12 = h % 12 || 12;
    return m > 0 ? `${h12}:${String(m).padStart(2, "0")} ${suffix}` : `${h12} ${suffix}`;
  };
  return `${fmt(start)} – ${fmt(end)}`;
}

/**
 * buildDeliveryLabel — Smart relative delivery label.
 *   "Today, 7 AM – 1 PM"  /  "Tomorrow, 9 AM – 11 PM"  /  "Saturday, 7 AM – 1 PM"
 * Falls back to estimated next-day when no confirmed expected_by.
 */
function buildDeliveryLabel(expected_by, order_date, delivery_window) {
  let targetDate = null;
  let isEstimated = false;

  if (expected_by) {
    targetDate = new Date(expected_by);
  } else if (order_date) {
    const d = new Date(order_date);
    d.setDate(d.getDate() + 1); // next-day estimate
    targetDate = d;
    isEstimated = true;
  }

  if (!targetDate || isNaN(targetDate.getTime())) return { label: null, isEstimated: false };

  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(targetDate.getFullYear(), targetDate.getMonth(), targetDate.getDate());
  const diffDays = Math.round((target - today) / (86400000));

  let dayLabel;
  if (diffDays === 0) dayLabel = "Today";
  else if (diffDays === 1) dayLabel = "Tomorrow";
  else if (diffDays === -1) dayLabel = "Yesterday";
  else if (diffDays > 1 && diffDays <= 6) {
    dayLabel = target.toLocaleDateString("en-AE", { weekday: "long" });
  } else {
    dayLabel = target.toLocaleDateString("en-AE", { weekday: "long", day: "numeric", month: "short" });
  }

  // Append store-specific time window if available
  if (delivery_window?.start && delivery_window?.end) {
    return { label: `${dayLabel}, ${formatTimeWindow(delivery_window.start, delivery_window.end)}`, isEstimated };
  }

  return { label: dayLabel, isEstimated };
}

/* ─── Dismiss × button (shared) ────────────────────────────── */

function DismissButton({ onClick, className = "" }) {
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className={`w-6 h-6 flex items-center justify-center rounded-full hover:bg-black/5 transition-colors cursor-pointer ${className}`}
      aria-label="Dismiss"
    >
      <svg width="10" height="10" viewBox="0 0 16 16" fill="none" className="text-current opacity-40">
        <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>
    </button>
  );
}

/* ─── Main component ──────────────────────────────────────── */

/**
 * OrderTrackingCard — Shows order status in the list view.
 *
 * Two visual states:
 *   A) In transit: emerald card with truck icon, smart delivery label, expandable items
 *   B) Delivered: horizontal carousel of unrated orders (80% width, next peeks 20%)
 *
 * Props:
 *   activeOrder      – in-transit order data (or null)
 *   deliveredOrders  – array of delivered-but-unrated orders
 *   onRate           – (orderId) => void  (open rating modal for specific order)
 *   onDismiss        – (orderId) => void  (dismiss / hide this card)
 */
export default function OrderTrackingCard({ activeOrder, deliveredOrders = [], onRate, onDismiss, onReorder }) {
  const scrollRef = useRef(null);
  // Hook must run unconditionally (before any early return) — React rules.
  const drag = useDragScroll(scrollRef);

  const showActive = activeOrder && activeOrder.store_name;
  const showDelivered = deliveredOrders.length > 0;

  // Nothing to show
  if (!showActive && !showDelivered) return null;

  // Render BOTH an in-transit card AND any delivered-unrated orders.
  // Previously an active order returned early and HID the delivered
  // carousel, so a delivered order from one vendor was invisible (and
  // un-rateable) whenever another vendor's order was still in transit.
  return (
    <div className="flex flex-col gap-3">
      {showActive && (
        <ActiveOrderCard
          order={activeOrder}
          onDismiss={() => onDismiss?.(activeOrder.order_id)}
        />
      )}

      {showDelivered && (
        <div className="-mx-4">
          <div
            ref={scrollRef}
            className="flex gap-3 overflow-x-auto pb-2 px-4 scrollbar-hide"
            style={{
              scrollSnapType: drag.isDragging ? "none" : "x mandatory",
              WebkitOverflowScrolling: "touch",
              cursor: deliveredOrders.length > 1 ? "grab" : "default",
            }}
            onMouseDown={drag.onMouseDown}
            onMouseMove={drag.onMouseMove}
            onMouseUp={drag.onMouseUp}
            onMouseLeave={drag.onMouseLeave}
          >
            {deliveredOrders.map((order) => (
              <DeliveredCard
                key={order.order_id}
                order={order}
                onRate={() => {
                  if (drag.isDragging) return;
                  onRate(order.order_id);
                  trackEvent("rating_start", { orderId: order.order_id });
                }}
                onDismiss={() => onDismiss?.(order.order_id)}
                onReorder={onReorder}
                isSingle={deliveredOrders.length === 1}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── Active order card ───────────────────────────────────── */

/**
 * ActiveOrderCard — In-transit order with smart delivery label + expandable items.
 *
 * Shows:
 *   🚚  Order placed · from Kibsons          ×
 *       Tomorrow, 7 AM – 1 PM
 *       5 items on the way ▼
 *         [expanded item list]
 */
function ActiveOrderCard({ order, onDismiss }) {
  const [expanded, setExpanded] = useState(false);
  const { store_name, order_ref, status, expected_by, order_date, delivery_window, items = [] } = order;

  // Smart delivery label
  const { label: deliveryLabel, isEstimated } = buildDeliveryLabel(expected_by, order_date, delivery_window);

  const statusLabels = {
    expected: "Order placed",
    confirmed: "Confirmed",
    processing: "Processing",
    shipped: "Shipped",
    out_for_delivery: "Out for delivery",
    dispatched: "Dispatched",
    preparing: "Preparing",
    unknown: "Order placed",
  };
  const statusText = statusLabels[status] || status || "Order placed";

  const itemCount = items.length;

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
      className="bg-emerald-50 border border-emerald-200 rounded-xl px-4 py-3"
    >
      <div className="flex items-start gap-3">
        {/* Truck icon */}
        <div className="w-8 h-8 rounded-lg bg-emerald-100 flex items-center justify-center flex-shrink-0 mt-0.5">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="text-emerald-600">
            <path d="M1 3H10V10H1V3Z" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />
            <path d="M10 5.5H13L15 8V10H10V5.5Z" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />
            <circle cx="4" cy="11.5" r="1.5" stroke="currentColor" strokeWidth="1.25" />
            <circle cx="12.5" cy="11.5" r="1.5" stroke="currentColor" strokeWidth="1.25" />
          </svg>
        </div>

        <div className="flex-1 min-w-0">
          {/* Status + store */}
          <div className="flex items-center gap-2 mb-0.5">
            <span className="text-sm font-semibold text-emerald-800">{statusText}</span>
            <span className="text-xs text-emerald-600">from {store_name}</span>
          </div>

          {/* Delivery label — "Tomorrow, 7 AM – 1 PM" */}
          {deliveryLabel && (
            <p className="text-xs text-emerald-700">
              {isEstimated ? `Est. delivery ${deliveryLabel}` : `Expected ${deliveryLabel}`}
            </p>
          )}

          {/* Order ref */}
          {order_ref && (
            <p className="text-xs text-emerald-500 mt-0.5">#{order_ref}</p>
          )}

          {/* "X items on the way" expandable row */}
          {itemCount > 0 && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-emerald-700 hover:text-emerald-900 transition-colors cursor-pointer"
            >
              <svg width="10" height="10" viewBox="0 0 16 16" fill="none" className="text-emerald-500">
                <path d="M2 4H14M2 8H14M2 12H8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              {itemCount} {itemCount === 1 ? "item" : "items"} on the way
              <svg
                width="10" height="10" viewBox="0 0 16 16" fill="none"
                className={`transition-transform ${expanded ? "rotate-180" : ""}`}
              >
                <path d="M4 6L8 10L12 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}

          {/* Expanded item list */}
          <AnimatePresence>
            {expanded && itemCount > 0 && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="mt-2 flex flex-col gap-1">
                  {items.map((item, i) => (
                    <div key={item.order_item_id || i} className="flex items-center gap-2 py-1">
                      <span className="w-5 h-5 rounded bg-emerald-100 flex items-center justify-center text-[10px] font-semibold text-emerald-600 flex-shrink-0">
                        {item.name?.charAt(0).toUpperCase()}
                      </span>
                      <span className="text-xs text-emerald-800 truncate flex-1">
                        {item.name}
                        {item.quantity > 1 && (
                          <span className="text-emerald-500 font-normal"> x{item.quantity}</span>
                        )}
                      </span>
                      {item.price > 0 && (
                        <span className="text-xs text-emerald-500 tabular-nums flex-shrink-0">
                          {Number(item.price).toFixed(2)}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Dismiss × */}
        <DismissButton onClick={onDismiss} className="text-emerald-600 -mt-0.5 -mr-1" />
      </div>
    </motion.div>
  );
}

/* ─── Delivered order card ────────────────────────────────── */

/**
 * DeliveredCard — Compact delivered order card.
 *
 * Layout:
 *   [Logo✓] Delivered · 11 items · AED 250
 *           Monday, 23 Feb, 09:05 PM
 *           [⭐ Rate]  [Dismiss]
 */
function DeliveredCard({ order, onRate, onDismiss, onReorder, isSingle }) {
  const { order_id, store_name, delivery_date, total_amount, item_count, items = [] } = order;
  const [expanded, setExpanded] = useState(false);
  const [reorderedIds, setReorderedIds] = useState({});

  let deliveryLabel = "";
  if (delivery_date) {
    try {
      const d = new Date(delivery_date);
      deliveryLabel = d.toLocaleDateString("en-AE", {
        weekday: "long",
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      deliveryLabel = delivery_date;
    }
  }

  const count = item_count || items.length;

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex-shrink-0 bg-surface-card border border-border rounded-xl px-4 py-3 ${
        isSingle ? "w-full" : "w-[80%] min-w-[280px]"
      }`}
      style={{ scrollSnapAlign: "start" }}
    >
      <div className="flex items-start gap-3">
        {/* Store logo with delivered checkmark */}
        <div className="relative flex-shrink-0 mt-0.5">
          <div className="w-8 h-8 rounded-lg bg-chip-bg flex items-center justify-center overflow-hidden">
            <VendorLogo name={store_name} size={22} />
          </div>
          {/* Small checkmark badge */}
          <div className="absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full bg-emerald-500 flex items-center justify-center">
            <svg width="7" height="7" viewBox="0 0 16 16" fill="none" className="text-white">
              <path d="M3 8.5L6.5 12L13 4" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
        </div>

        <div className="flex-1 min-w-0">
          {/* Line 1: Delivered + item count + total — tap to expand.
              If items aren't in the DB yet (catalog scrape pending),
              skip the "0 items" string entirely -- the total alone is
              honest; "0 items · AED 289" reads as broken data. */}
          <button
            type="button"
            onClick={() => items.length > 0 && setExpanded((v) => !v)}
            className="flex items-baseline gap-1.5 w-full text-left cursor-pointer"
          >
            <span className="text-sm font-semibold text-text-primary">Delivered</span>
            <span className="text-xs text-text-tertiary tabular-nums">
              {count > 0 && <>&middot; {count} {count === 1 ? "item" : "items"}</>}
              {total_amount > 0 && <> &middot; AED {Number(total_amount).toFixed(0)}</>}
            </span>
            {items.length > 0 && (
              <svg
                width="10" height="10" viewBox="0 0 16 16" fill="none"
                className={`ml-auto text-text-tertiary transition-transform flex-shrink-0 ${expanded ? "rotate-180" : ""}`}
              >
                <path d="M4 6L8 10L12 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
          </button>

          {/* Line 2: Delivery date */}
          {deliveryLabel && (
            <p className="text-xs text-text-secondary mt-0.5">{deliveryLabel}</p>
          )}

          {/* Expanded item list */}
          <AnimatePresence>
            {expanded && items.length > 0 && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                {/* History label -- makes it visually distinct from the
                    "items being added" UI in the cart-build progress
                    state. Previously the two felt interchangeable. */}
                <div className="mt-2.5 mb-1 px-1 flex items-center gap-1.5">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-text-tertiary">
                    What you bought
                  </span>
                  <span className="flex-1 h-px bg-border-light" />
                </div>
                <div className="flex flex-col gap-1.5">
                  {items.map((item) => {
                    const subtitle = [item.brand, item.size].filter(Boolean).join(" \u00b7 ");
                    return (
                      <div key={item.order_item_id} className="flex items-center gap-2.5 py-1.5">
                        {/* Thumbnail or initial */}
                        <div className="w-8 h-8 rounded-lg flex-shrink-0 overflow-hidden bg-chip-bg flex items-center justify-center">
                          {item.image_url ? (
                            <img
                              src={item.image_url}
                              alt={item.name}
                              className="w-full h-full object-cover"
                              onError={(e) => {
                                e.target.style.display = "none";
                                e.target.nextSibling && (e.target.nextSibling.style.display = "flex");
                              }}
                            />
                          ) : null}
                          <span
                            className="text-xs font-semibold text-text-secondary"
                            style={item.image_url ? { display: "none" } : {}}
                          >
                            {item.name.charAt(0).toUpperCase()}
                          </span>
                        </div>

                        {/* Name + subtitle */}
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium text-text-primary truncate leading-snug">
                            {item.name}
                            {item.quantity > 1 && (
                              <span className="text-text-tertiary font-normal"> x{item.quantity}</span>
                            )}
                          </p>
                          {subtitle && (
                            <p className="text-[10px] text-text-tertiary truncate leading-tight">{subtitle}</p>
                          )}
                        </div>

                        {/* Price */}
                        {item.price > 0 && (
                          <span className="text-xs text-text-secondary tabular-nums flex-shrink-0">
                            {Number(item.price).toFixed(2)}
                          </span>
                        )}

                        {/* Re-order button -- one-tap re-add to the
                            current Scheduled list. Per-item state
                            shows a checkmark briefly so the user
                            confirms the action took effect. */}
                        {onReorder && item.order_item_id && (
                          <button
                            type="button"
                            disabled={!!reorderedIds[item.order_item_id]}
                            onClick={(e) => {
                              e.stopPropagation();
                              onReorder(item.order_item_id, item.name);
                              setReorderedIds((prev) => ({ ...prev, [item.order_item_id]: true }));
                              trackEvent("reorder_from_history", {
                                orderItemId: item.order_item_id,
                                name: item.name,
                              });
                            }}
                            aria-label={`Re-add ${item.name} to Scheduled`}
                            className={`flex items-center gap-0.5 px-2 h-7 rounded-md text-[11px] font-medium transition-colors cursor-pointer flex-shrink-0 ${
                              reorderedIds[item.order_item_id]
                                ? "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 cursor-default"
                                : "bg-accent/10 text-accent hover:bg-accent/20"
                            }`}
                          >
                            {reorderedIds[item.order_item_id] ? (
                              <>
                                <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
                                  <path d="M3 8.5L6.5 12L13 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                                </svg>
                                Added
                              </>
                            ) : (
                              <>
                                <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
                                  <path d="M8 3V13M3 8H13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                </svg>
                                Re-add
                              </>
                            )}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Action buttons: Rate + Dismiss */}
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              onClick={onRate}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
            >
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
                <path d="M8 1L10 5.5L15 6.5L11.5 10L12.5 15L8 12.5L3.5 15L4.5 10L1 6.5L6 5.5L8 1Z"
                  stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />
              </svg>
              Rate This Order
            </button>
            <button
              type="button"
              onClick={onDismiss}
              className="px-2.5 py-1.5 rounded-lg text-xs text-text-tertiary hover:text-text-secondary hover:bg-chip-bg transition-colors cursor-pointer"
            >
              Dismiss
            </button>
          </div>
        </div>

        {/* Top-right dismiss × for carousel cards */}
        {!isSingle && (
          <DismissButton onClick={onDismiss} className="text-text-tertiary -mt-0.5 -mr-1" />
        )}
      </div>
    </motion.div>
  );
}
