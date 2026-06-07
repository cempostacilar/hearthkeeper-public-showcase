import { useMemo, useState, useCallback, useEffect, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import ConversationHeader from "./components/ConversationHeader";
import VendorSection from "./components/VendorSection";
import OrderTrackingCard from "./components/OrderTrackingCard";
import StickyActionBar from "./components/StickyActionBar";
import ImageBottomSheet from "./components/ImageBottomSheet";
import SuggestionBottomSheet from "./components/SuggestionBottomSheet";
import AlternativesBottomSheet from "./components/AlternativesBottomSheet";
import RatingModal from "./components/RatingModal";
import ChatPanel from "./components/ChatPanel";
import FeedbackStatusBar from "./components/FeedbackStatusBar";
import FeedbackItemCard from "./components/FeedbackItemCard";
import RecentLearningPanel from "./components/RecentLearningPanel";
import SettingsSheet from "./components/SettingsSheet";
import { useWebSocket, ConnectionState } from "./hooks/useWebSocket";
import { trackEvent } from "./analytics";

/**
 * formatRelativeDays -- "today" / "yesterday" / "3 days ago".
 * Per user feedback: days are simpler than dates for the
 * "scheduled since X" caption. Accepts an ISO string (or
 * SQLite "YYYY-MM-DD HH:MM:SS"); returns "" for missing input.
 */
function formatRelativeDays(ts) {
  if (!ts) return "";
  // SQLite returns "YYYY-MM-DD HH:MM:SS" -- normalize to a Date.
  // Treat as UTC so we don't get spurious -1 day offsets in UAE.
  const iso = typeof ts === "string" && !ts.includes("T")
    ? ts.replace(" ", "T") + "Z"
    : ts;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((today - target) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

export default function App() {
  // ── WebSocket hook replaces sampleData ──
  const {
    connectionState,
    messages,
    listData,
    listDiff,
    conversation,
    agentState,
    isTyping,
    thinkingLabel,
    cartResult,
    cartProgress,
    cartFailedItems,
    clearCartFailedItems,
    alternativesData,
    findAlternatives,
    acceptAlternative,
    clearAlternatives,
    suggestion,
    orderTracking,
    ratingLlmResponse,
    // Two-entity model
    suggestionData,
    cartData,
    suggestionListId,
    cartListId,
    activeView,
    suggestionConversation,
    cartConversation,
    wsToast,
    // Feedback
    feedbackStatus,
    feedbackItems,
    whatsappEnabled,
    // A12 Recently-learned
    learningItems,
    // A13 Preferences page (full memory)
    preferences,
    // Actions
    sendMessage,
    sendAction,
    sendFeedback,
    unreadNudges,
    clearNudges,
    createCart,
    dismissCart,
    cherryPick,
    switchView,
    clearToast,
    requestFeedback,
    approveFeedback,
    rejectFeedback,
    cancelFeedback,
    getLearningItems,
    resetPreference,
    getPreferences,
    deactivatePreference,
  } = useWebSocket();

  // Ephemeral dismiss state — hides order cards until next page refresh
  const [dismissedOrderIds, setDismissedOrderIds] = useState(new Set());
  const handleDismissOrder = useCallback((orderId) => {
    setDismissedOrderIds((prev) => new Set([...prev, orderId]));
  }, []);

  // Feedback bar dismissal. Persisted to localStorage keyed by roundId
  // so a "timed out" bar that the user dismissed stays dismissed across
  // page reloads -- previously it came back every time you opened the
  // app, even weeks after the round ended.
  const feedbackRoundId = feedbackStatus?.roundId;
  const dismissedKey = feedbackRoundId ? `hk-feedback-dismissed-${feedbackRoundId}` : null;
  const [feedbackDismissed, setFeedbackDismissed] = useState(() => {
    if (typeof window === "undefined" || !dismissedKey) return false;
    return window.localStorage.getItem(dismissedKey) === "1";
  });
  // Rehydrate when the round changes (different round → different key).
  const prevRoundIdRef = useRef(feedbackRoundId);
  if (feedbackRoundId !== prevRoundIdRef.current) {
    prevRoundIdRef.current = feedbackRoundId;
    const isDismissed = !!(dismissedKey && typeof window !== "undefined"
      && window.localStorage.getItem(dismissedKey) === "1");
    setFeedbackDismissed(isDismissed);
  }
  const handleDismissFeedback = useCallback(() => {
    setFeedbackDismissed(true);
    if (typeof window !== "undefined" && dismissedKey) {
      try { window.localStorage.setItem(dismissedKey, "1"); } catch { /* quota or private mode */ }
    }
  }, [dismissedKey]);

  // Derived: vendors for the ACTIVE tab, with fallback to legacy listData
  const suggestionVendors = suggestionData?.vendors || [];
  const cartVendors = cartData?.vendors || [];
  const vendors = activeView === "cart" ? cartVendors : suggestionVendors;
  // Fallback for legacy single-list mode (only when no dual-list data exists at all)
  const legacyVendors = listData || [];
  const isDualListMode = suggestionData !== null || cartData !== null;
  const displayVendors = isDualListMode ? vendors : legacyVendors;
  const hasCart = !!cartListId;
  const hasSuggestions = !!suggestionListId;

  // Set of item names in the cart — for showing "In Cart" checkmarks on suggestions tab
  const cartItemNames = useMemo(() => {
    return new Set(
      cartVendors.flatMap((v) =>
        v.categories.flatMap((c) => c.items.map((i) => i.name))
      )
    );
  }, [cartVendors]);

  // Pick the conversation header for the ACTIVE tab
  // (so switching tabs updates the header summary)
  const activeConversation = useMemo(() => {
    if (isDualListMode) {
      const viewConv = activeView === "cart" ? cartConversation : suggestionConversation;
      if (viewConv) return viewConv;
    }
    // Fallback to the global conversation (legacy or default)
    return conversation;
  }, [isDualListMode, activeView, cartConversation, suggestionConversation, conversation]);

  // Local-only UI state.
  // expandedCategories: a Record<string, boolean> rather than a single
  // string, so multiple categories can stay open at once. The old
  // single-string behaviour made short lists feel inconsistent
  // (Carrefour's category expanded, Kibsons' didn't).
  const [expandedCategories, setExpandedCategories] = useState({});
  const [bottomSheetItem, setBottomSheetItem] = useState(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [cartBanner, setCartBanner] = useState(null); // {type: "success"|"error", text}
  const [toast, setToast] = useState(null); // {text, type}
  // Confirm dialog for clearing the cart -- destructive action that
  // wipes the user's curated picks. {itemCount} or null.
  const [clearConfirm, setClearConfirm] = useState(null);
  // Tracks whether the user has explicitly interacted with the category
  // collapse. While this is false, we may auto-expand short lists
  // (UX: don't make a 2-item list require a tap to view).
  const userTouchedCategoryRef = useRef(false);

  // A12 — fetch ladder state once the socket is up. Only ask if we
  // haven't been told yet (learningItems === null means "not loaded";
  // {} means "loaded empty"). Avoids re-asking on every reconnect.
  useEffect(() => {
    if (connectionState === ConnectionState.CONNECTED && learningItems === null) {
      getLearningItems();
    }
  }, [connectionState, learningItems, getLearningItems]);

  // A13 — lazy-load the full preferences memory when Settings opens.
  // Settings is opt-in chrome, no need to keep this hot.
  useEffect(() => {
    if (settingsOpen && connectionState === ConnectionState.CONNECTED) {
      getPreferences();
    }
  }, [settingsOpen, connectionState, getPreferences]);

  // Rating modal persistent state (survives open/close)
  const [ratingOpen, setRatingOpen] = useState(false);
  const [ratingOrderId, setRatingOrderId] = useState(null);
  const [ratingItemFeedback, setRatingItemFeedback] = useState({});
  const [ratingFeedbackText, setRatingFeedbackText] = useState("");

  // Derive the rating order from deliveredOrders array
  const ratingOrder = useMemo(() => {
    if (!ratingOrderId) return orderTracking.deliveredOrders?.[0] || orderTracking.deliveredOrder;
    return orderTracking.deliveredOrders?.find((o) => o.order_id === ratingOrderId)
      || orderTracking.deliveredOrder;
  }, [ratingOrderId, orderTracking]);

  // ── Keep bottom sheet item in sync with list updates ──
  useEffect(() => {
    if (bottomSheetItem && displayVendors.length > 0) {
      for (const v of displayVendors) {
        for (const c of v.categories) {
          const updated = c.items.find((i) => i.id === bottomSheetItem.id);
          if (updated) {
            setBottomSheetItem(updated);
            return;
          }
        }
      }
      // Item was removed — close the bottom sheet
      setBottomSheetItem(null);
    }
  }, [displayVendors]);

  // Reset rating feedback when switching to a DIFFERENT order
  const prevRatingOrderId = useRef(null);
  useEffect(() => {
    if (ratingOrderId && ratingOrderId !== prevRatingOrderId.current) {
      // Switching to a different order — clear old feedback
      setRatingItemFeedback({});
      setRatingFeedbackText("");
    }
    prevRatingOrderId.current = ratingOrderId;
  }, [ratingOrderId]);

  const toggleCategory = useCallback((categoryKey) => {
    userTouchedCategoryRef.current = true;
    setExpandedCategories((prev) => ({
      ...prev,
      [categoryKey]: !prev[categoryKey],
    }));
  }, []);

  // Compute total visible items across the active view's vendors. Used
  // for the action-bar CTA label AND to decide whether to auto-expand
  // the first category for short lists (so a 2-item list doesn't need
  // a tap to view its contents).
  const visibleItemCount = useMemo(() => {
    let n = 0;
    for (const v of displayVendors) {
      for (const c of v.categories || []) {
        for (const i of c.items || []) {
          if (i.quantity > 0) n++;
        }
      }
    }
    return n;
  }, [displayVendors]);

  // Auto-expand the first non-empty category of EACH vendor when the
  // list is short. Previously we only expanded the very first match,
  // which produced asymmetric output ("Carrefour Dairy open, Kibsons
  // Pantry closed") for a 4-item list -- both should open.
  // Threshold = 8 items: with one category per vendor expanded, that's
  // still a phone-screen-worth of content. Tap-to-toggle on any
  // category turns this off (userTouchedCategoryRef), so the user
  // always wins.
  useEffect(() => {
    if (userTouchedCategoryRef.current) return;
    if (visibleItemCount === 0 || visibleItemCount > 8) return;
    const next = {};
    for (const v of displayVendors) {
      for (const c of v.categories || []) {
        if ((c.items || []).some((i) => i.quantity > 0)) {
          next[`${v.name}:${c.name}`] = true;
          break; // only the first non-empty category of each vendor
        }
      }
    }
    // Only update if there's something new -- otherwise we churn state.
    if (Object.keys(next).length > 0) {
      setExpandedCategories((prev) => ({ ...next, ...prev }));
    }
  }, [visibleItemCount, displayVendors]);

  // ── Quantity change → WebSocket action ──
  const updateQuantity = useCallback(
    (itemId, delta) => {
      // Find current quantity from all available vendor lists
      let currentQty = 1;
      const allVendors = [...(suggestionVendors || []), ...(cartVendors || []), ...(legacyVendors || [])];
      for (const v of allVendors) {
        for (const c of v.categories) {
          for (const item of c.items) {
            if (item.id === itemId) currentQty = item.quantity;
          }
        }
      }
      const newQty = Math.max(0, Math.round(currentQty + delta));
      sendAction("change_qty", { listItemId: itemId, quantity: newQty });
      trackEvent("quantity_change", { itemId, delta, newQty });
    },
    [suggestionVendors, cartVendors, legacyVendors, sendAction]
  );

  // ── Remove item → WebSocket action ──
  const removeItem = useCallback(
    (itemId) => {
      sendAction("remove_item", { listItemId: itemId });
      trackEvent("item_remove", { itemId });
    },
    [sendAction]
  );

  // ── Item preference feedback (pin, block brand, wrong item) ──
  const handleItemPref = useCallback(
    (itemId, prefType, value, extras = {}) => {
      sendAction("item_pref", {
        itemId,
        prefType,
        value,
        listItemId: extras.listItemId,
        feedbackText: extras.feedbackText,
      });
      trackEvent("item_pref", { itemId, prefType, value });
    },
    [sendAction]
  );

  // ── Order action (Ready/Skip/Delay) → WebSocket ──
  // Reset on the Cart view is destructive (wipes curated picks).
  // Intercept it with a confirm dialog showing how many items are
  // about to disappear. Reset on Scheduled is reversible (re-applies
  // the suggestion snapshot), so we let that one through quietly.
  const handleOrderAction = useCallback(
    (action) => {
      if (action === "reset" && activeView === "cart" && visibleItemCount > 0) {
        setClearConfirm({ count: visibleItemCount });
        trackEvent("clear_cart_prompt", { count: visibleItemCount });
        return;
      }
      sendAction("order_action", { command: action, view: activeView });
      setCartBanner(null); // clear any previous banner
      trackEvent("order_action_confirmed", { action, view: activeView });
    },
    [sendAction, activeView, visibleItemCount]
  );

  // Confirm dialog completion
  const confirmClearCart = useCallback(() => {
    sendAction("order_action", { command: "reset", view: "cart" });
    setCartBanner(null);
    trackEvent("clear_cart_confirm", { count: clearConfirm?.count || 0 });
    setClearConfirm(null);
  }, [sendAction, clearConfirm]);

  // ── Alternatives flow ──
  // Source item is kept locally so the AlternativesBottomSheet can
  // show "Currently: {name}" even after the original ImageBottomSheet
  // is closed.
  const [alternativesSourceItem, setAlternativesSourceItem] = useState(null);

  const handleFindAlternatives = useCallback((item, scope = "same_vendor") => {
    setAlternativesSourceItem(item);
    findAlternatives(item.id, scope);
    trackEvent("find_alternatives_request", { itemId: item.itemId, scope });
  }, [findAlternatives]);

  const handleScopeSwitch = useCallback((scope) => {
    if (!alternativesSourceItem) return;
    findAlternatives(alternativesSourceItem.id, scope);
  }, [findAlternatives, alternativesSourceItem]);

  const handleAcceptAlternative = useCallback((catalogId, scope) => {
    if (!alternativesSourceItem) return;
    acceptAlternative(alternativesSourceItem.id, catalogId, scope);
    setAlternativesSourceItem(null);
  }, [acceptAlternative, alternativesSourceItem]);

  const closeAlternatives = useCallback(() => {
    clearAlternatives();
    setAlternativesSourceItem(null);
  }, [clearAlternatives]);

  // ── Show cart completion banner when backend sends cart_complete ──
  useEffect(() => {
    if (cartResult) {
      setCartBanner({
        type: cartResult.success ? "success" : "error",
        text: cartResult.message,
      });
      // Auto-dismiss success after 20 seconds
      if (cartResult.success) {
        const t = setTimeout(() => setCartBanner(null), 20000);
        return () => clearTimeout(t);
      }
    }
  }, [cartResult]);

  // ── Chat message → WebSocket text ──
  const handleSendMessage = useCallback(
    (text) => {
      sendMessage(text);
      trackEvent("chat_send", { text });
    },
    [sendMessage]
  );

  // ── Feedback handler (thumbs up/down/alert) ──
  const handleFeedback = useCallback(
    (msgId, vote, alertText) => {
      sendFeedback(msgId, vote);
      // For alert feedback, also send the detail text as a special message
      if (vote === "alert" && alertText) {
        sendAction("feedback_alert", { msg_id: msgId, vote: "alert", detail: alertText });
      }
      trackEvent("feedback", { msgId, vote, hasDetail: !!alertText });
    },
    [sendFeedback, sendAction]
  );

  // Auto-open chat when a nudge arrives DURING the session.
  //
  // The previous version auto-opened on first render too, which meant
  // any unread nudges queued from a previous session popped the chat
  // panel up the moment you visited the app. That was unwanted -- the
  // user wants to land on the suggestions/cart view, not the chat.
  //
  // First-render guard: skip the very first effect invocation. Any
  // nudges that exist at mount are "history"; only auto-open for
  // genuinely new ones that arrive afterwards.
  const initialNudgeSkip = useRef(true);
  useEffect(() => {
    if (initialNudgeSkip.current) {
      initialNudgeSkip.current = false;
      // Quietly clear the unread badge on first load so the user
      // doesn't see an "unread" count for nudges they already saw.
      if (unreadNudges > 0) clearNudges();
      return;
    }
    if (unreadNudges > 0 && !chatOpen) {
      setChatOpen(true);
      clearNudges();
    }
  }, [unreadNudges]);

  // Auto-dismiss toast. Action toasts get a slightly longer window
  // (6s) so the user has time to react to "🛒 Had it" / "View Cart"
  // chips; bare info toasts dismiss at 4s.
  useEffect(() => {
    if (wsToast) {
      const delay = wsToast.action ? 6000 : 4000;
      const t = setTimeout(() => clearToast(), delay);
      return () => clearTimeout(t);
    }
  }, [wsToast, clearToast]);

  const openChat = useCallback((initialMessage) => {
    setChatOpen(true);
    clearNudges();
    trackEvent("chat_open");
    // If initial message provided (e.g., from "Create Order"), auto-send it
    if (initialMessage && typeof initialMessage === "string") {
      setTimeout(() => sendMessage(initialMessage), 300);
    }
  }, [clearNudges, sendMessage]);

  const closeChat = useCallback(() => {
    setChatOpen(false);
    trackEvent("chat_close");
  }, []);

  const lastAssistantMessage = useMemo(() => {
    const last = [...messages].reverse().find((m) => m.role === "assistant");
    if (!last) return null;
    // Show just the first line, truncated, for the sticky bar preview
    const firstLine = last.text.split("\n")[0];
    return firstLine.length > 80 ? firstLine.slice(0, 80) + "..." : firstLine;
  }, [messages]);

  // Check if we have orders to show (filter out dismissed ones)
  const filteredDeliveredOrders = (orderTracking.deliveredOrders || []).filter(
    (o) => !dismissedOrderIds.has(o.order_id)
  );
  const activeOrder = orderTracking.activeOrder && !dismissedOrderIds.has(orderTracking.activeOrder.order_id)
    ? orderTracking.activeOrder
    : null;
  const hasDeliveredOrders = filteredDeliveredOrders.length > 0;
  const hasActiveOrder = !!activeOrder;

  let categoryOffset = 0;

  return (
    <div className="min-h-screen bg-surface">
      {/* Connection status banner */}
      {connectionState !== ConnectionState.CONNECTED && (
        <div
          className={`fixed top-0 left-0 right-0 z-50 text-center text-xs py-1.5 ${
            connectionState === ConnectionState.DISCONNECTED
              ? "bg-status-error-bg text-status-error"
              : "bg-status-warning-bg text-status-warning"
          }`}
        >
          {connectionState === ConnectionState.CONNECTING && "Connecting to Hearthkeeper..."}
          {connectionState === ConnectionState.RECONNECTING && "Reconnecting..."}
          {connectionState === ConnectionState.DISCONNECTED &&
            "Disconnected. Check if backend is running (python3 app.py)"}
        </div>
      )}

      <div className={`max-w-lg mx-auto px-4 pt-4 ${activeConversation?.state === "all_caught_up" ? "pb-8" : "pb-36"}`}>
        {/* Conversation header */}
        <div className="mb-3">
          <ConversationHeader
            conversation={
              activeConversation || { title: "Hearthkeeper", description: "Connecting to agent..." }
            }
            messages={messages}
            onOpenChat={openChat}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        </div>

        {/* Cart completion banner.
            When the build had failures, we list them right here in the
            banner -- the user previously had to guess which items were
            missed. Each failure shows the item name, store, and the
            reason if the agent gave one. */}
        <AnimatePresence>
          {cartBanner && (
            <motion.div
              initial={{ opacity: 0, y: -8, height: 0 }}
              animate={{ opacity: 1, y: 0, height: "auto" }}
              exit={{ opacity: 0, y: -8, height: 0 }}
              className="mb-3 overflow-hidden"
            >
              <div
                className={`rounded-xl px-4 py-3 ${
                  cartBanner.type === "success"
                    ? "bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800"
                    : "bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800"
                }`}
              >
                <div className="flex items-start gap-3">
                  <span className="text-lg mt-0.5">
                    {cartBanner.type === "success" ? "\u2705" : "\u26a0\ufe0f"}
                  </span>
                  <div className="flex-1 min-w-0">
                    <p
                      className={`text-sm font-medium ${
                        cartBanner.type === "success"
                          ? "text-emerald-800 dark:text-emerald-200"
                          : "text-red-800 dark:text-red-200"
                      }`}
                    >
                      {cartBanner.text}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => { setCartBanner(null); clearCartFailedItems(); }}
                    className="text-text-tertiary hover:text-text-secondary p-1 -mr-1 cursor-pointer"
                    aria-label="Dismiss"
                  >
                    {"\u2715"}
                  </button>
                </div>

                {/* Failed items list -- only when something actually
                    failed. Lets the user see exactly what didn't make
                    it instead of just a count. */}
                {cartFailedItems && cartFailedItems.length > 0 && (
                  <div className="mt-2.5 pt-2.5 border-t border-red-200/60 dark:border-red-800/60 space-y-1.5">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-red-700 dark:text-red-300">
                      Couldn't add ({cartFailedItems.length})
                    </p>
                    <ul className="space-y-1">
                      {cartFailedItems.map((f, i) => (
                        <li key={i} className="text-xs text-red-700 dark:text-red-200 flex items-start gap-1.5">
                          <span className="text-red-400 dark:text-red-500 mt-0.5">&bull;</span>
                          <span className="flex-1">
                            <span className="font-medium">{f.name}</span>
                            {f.store && <span className="text-red-500/80 dark:text-red-400/80"> &middot; {f.store}</span>}
                            {f.reason && <span className="text-red-600/90 dark:text-red-300/90"> \u2014 {f.reason}</span>}
                          </span>
                        </li>
                      ))}
                    </ul>
                    <p className="text-[11px] text-red-600/80 dark:text-red-300/80 pt-0.5">
                      These items stayed on Scheduled so you can sort them out manually.
                    </p>
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* "Suggested 3 days ago" / "Cart updated today" caption.
            Tells the user at a glance whether they're looking at fresh
            picks or stale ones. Days, not dates -- per user feedback
            "use days actually use days - its simpler". */}
        {(suggestionData?.createdAt || cartData?.createdAt) && (
          <p className="text-[11px] text-text-tertiary text-center px-1 mb-2.5 -mt-1">
            {(() => {
              const targetData = activeView === "cart" ? cartData : suggestionData;
              const ts = targetData?.createdAt;
              if (!ts) return null;
              const noun = activeView === "cart" ? "Cart updated" : "Suggested";
              return `${noun} ${formatRelativeDays(ts)}`;
            })()}
          </p>
        )}

        {/* ── Two-Tab UI: Scheduled / Curated ──
            Active tab uses an accent-tint background instead of hard-coded
            bg-white. That fixes two problems at once:
              - Light mode: white-on-white-container was indistinguishable
                from the inactive tab (just a faint shadow). Accent tint
                gives clear active-state contrast.
              - Dark mode: pure white blew out on the dark page. Accent
                tint stays subtle while remaining clearly highlighted.
            Inactive tab text bumped from text-tertiary -> text-secondary
            so it's actually readable, not ghost-faded.
        */}
        {(hasSuggestions || hasCart) && (
          <div className="flex items-center gap-1 mb-3 bg-surface-elevated rounded-xl p-1">
            <button
              type="button"
              onClick={() => switchView("suggestions")}
              className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium transition-colors cursor-pointer ${
                activeView === "suggestions"
                  ? "bg-accent/10 text-accent shadow-sm"
                  : "text-text-secondary hover:text-text-primary"
              }`}
            >
              Scheduled
              {suggestionVendors.length > 0 && (
                <span className="ml-1.5 text-xs opacity-75">
                  {suggestionVendors.reduce((n, v) => n + v.categories.reduce((m, c) => m + c.items.length, 0), 0)}
                </span>
              )}
            </button>
            <button
              type="button"
              onClick={() => switchView("cart")}
              className={`flex-1 py-2 px-3 rounded-lg text-sm font-medium transition-colors cursor-pointer ${
                activeView === "cart"
                  ? "bg-accent/10 text-accent shadow-sm"
                  : "text-text-secondary hover:text-text-primary"
              }`}
            >
              {/* "Curated" was confusing -- the user understood the word
                  but the relationship to the "Build carts" action was
                  unclear. "Cart" matches the mental model: items you've
                  picked, ready to send to stores. */}
              Cart
              {cartVendors.length > 0 && (
                <span className="ml-1.5 text-xs opacity-60">
                  {cartVendors.reduce((n, v) => n + v.categories.reduce((m, c) => m + c.items.length, 0), 0)}
                </span>
              )}
              {!hasCart && (
                <span className="ml-1.5 text-xs opacity-40">0</span>
              )}
            </button>
          </div>
        )}

        {/* Feedback status bar — shows when a feedback round is active */}
        {feedbackStatus && activeView === "suggestions" && !feedbackDismissed && (
          <FeedbackStatusBar
            feedbackStatus={feedbackStatus}
            onCancel={cancelFeedback}
            onDismiss={handleDismissFeedback}
          />
        )}

        {/* A12 Recently-learned panel — surfaces the Tier-1/Tier-3
            ladder state (cadence_adjustments + active stop_suggesting
            prefs) so the agent's silent decisions are visible and
            reversible. Only on the suggestions tab; auto-hidden when
            empty. Collapsed by default. */}
        {activeView === "suggestions" && (
          <RecentLearningPanel
            learningItems={learningItems}
            onReset={resetPreference}
          />
        )}

        {/* Vendor > Category > SKU list (for active tab) */}
        {displayVendors.length > 0 ? (
          <div className="mb-3" key={isDualListMode ? `view-${activeView}` : "legacy"}>
            {displayVendors.map((vendor, vIdx) => {
              const offset = categoryOffset;
              categoryOffset += vendor.categories.length;
              return (
                <VendorSection
                  key={vendor.name}
                  vendor={vendor}
                  vendorIndex={vIdx}
                  expandedCategories={expandedCategories}
                  onToggleCategory={toggleCategory}
                  onUpdateQuantity={updateQuantity}
                  onRemoveItem={removeItem}
                  onImageClick={setBottomSheetItem}
                  categoryOffset={offset}
                  showCherryPick={activeView === "suggestions"}
                  onCherryPick={cherryPick}
                  cartItemNames={activeView === "suggestions" ? cartItemNames : null}
                />
              );
            })}
          </div>
        ) : (
          <div className="py-20">
            {connectionState !== ConnectionState.CONNECTED ? (
              <p className="text-sm text-text-tertiary text-center">Waiting for connection...</p>
            ) : activeView === "cart" ? (
              /* Empty cart — chat CTA */
              <div className="flex flex-col items-center gap-3">
                <div className="w-14 h-14 rounded-2xl bg-accent/10 flex items-center justify-center">
                  <svg width="24" height="24" viewBox="0 0 16 16" fill="none" className="text-accent">
                    <path
                      d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z"
                      stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <p className="text-sm font-medium text-text-primary">
                  {hasCart ? "Your cart is empty" : "Start your order"}
                </p>
                <p className="text-xs text-text-tertiary text-center max-w-[240px]">
                  {hasCart
                    ? "Add items via chat or pick from Scheduled tab"
                    : "Tell me what you need and I'll find the best products across stores"}
                </p>
                <button
                  type="button"
                  onClick={() => openChat()}
                  className="mt-2 px-5 py-2.5 rounded-full text-sm font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
                >
                  Open Chat
                </button>
              </div>
            ) : activeConversation?.state === "all_caught_up" ? (
              <div className="flex flex-col items-center gap-3">
                <span className="text-4xl text-emerald-500 font-bold">&check;</span>
                <p className="text-sm text-text-secondary">
                  Your pantry is fully stocked.
                </p>
                <button
                  type="button"
                  onClick={() => openChat()}
                  className="mt-2 px-5 py-2.5 rounded-full text-sm font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
                >
                  Create Order
                </button>
                <p className="text-xs text-text-tertiary mt-1">
                  or wait — I'll suggest one when supplies run low
                </p>
              </div>
            ) : hasCart && cartVendors.some((v) => v.categories?.some((c) => c.items?.some((i) => i.quantity > 0))) ? (
              /* Suggestions empty BUT cart has items -- likely just
                 moved items there. Tell the user where they went and
                 give them a one-tap path instead of staring at a
                 "Loading shopping list..." message that never resolves. */
              <div className="flex flex-col items-center gap-3">
                <div className="w-14 h-14 rounded-2xl bg-emerald-100 dark:bg-emerald-900/30 flex items-center justify-center">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" className="text-emerald-600">
                    <path d="M5 13l4 4L19 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </div>
                <p className="text-sm font-medium text-text-primary">
                  Scheduled items moved to Cart
                </p>
                <p className="text-xs text-text-tertiary text-center max-w-[240px]">
                  Review them before sending to stores.
                </p>
                <button
                  type="button"
                  onClick={() => switchView("cart")}
                  className="mt-2 px-5 py-2.5 rounded-full text-sm font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
                >
                  View Cart &rarr;
                </button>
              </div>
            ) : (suggestionListId || isDualListMode) ? (
              /* Suggestions empty, no cart filling it -- this is the
                 real "nothing scheduled" state, not a loading state. */
              <div className="flex flex-col items-center gap-3">
                <p className="text-sm text-text-secondary">Nothing scheduled right now.</p>
                <button
                  type="button"
                  onClick={() => openChat()}
                  className="mt-1 px-5 py-2.5 rounded-full text-sm font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
                >
                  Ask the agent
                </button>
              </div>
            ) : (
              <p className="text-sm text-text-tertiary text-center">Loading shopping list...</p>
            )}
          </div>
        )}

        {/* Feedback item cards — pending suggestions from household */}
        {feedbackItems.filter((fi) => fi.status === "pending" || fi.status === "unresolved").length > 0 && activeView === "suggestions" && (
          <div className="mb-3">
            <p className="text-[10px] font-semibold text-text-tertiary uppercase tracking-wider mb-1.5 px-1">
              Household Suggestions
            </p>
            <AnimatePresence>
              {feedbackItems
                .filter((fi) => fi.status === "pending" || fi.status === "unresolved")
                .map((fi) => (
                  <FeedbackItemCard
                    key={fi.id}
                    item={fi}
                    onApprove={approveFeedback}
                    onReject={rejectFeedback}
                  />
                ))}
            </AnimatePresence>
          </div>
        )}

        {/* Order tracking card */}
        {(hasActiveOrder || hasDeliveredOrders) && (
          <OrderTrackingCard
            activeOrder={activeOrder}
            deliveredOrders={filteredDeliveredOrders}
            onRate={(orderId) => {
              setRatingOrderId(orderId);
              setRatingOpen(true);
            }}
            onDismiss={(orderId) => {
              handleDismissOrder(orderId);
              // Only tell backend for delivered orders — active order dismiss is ephemeral
              const isDelivered = filteredDeliveredOrders.some((o) => o.order_id === orderId);
              if (isDelivered) {
                sendAction("dismiss_rating", { orderId });
              }
            }}
            onReorder={(orderItemId) => {
              sendAction("reorder_from_history", { orderItemId });
              trackEvent("reorder_from_history_send", { orderItemId });
            }}
          />
        )}
      </div>

      {/* Toast notification */}
      <AnimatePresence>
        {wsToast && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 20 }}
            transition={{ duration: 0.2 }}
            className="fixed bottom-20 left-1/2 -translate-x-1/2 z-40 max-w-sm"
          >
            <div className="bg-text-primary text-white rounded-xl px-4 py-2.5 shadow-lg flex items-center gap-2.5 text-sm">
              <span>{wsToast.text}</span>
              {wsToast.action === "view_cart" && (
                <button
                  type="button"
                  onClick={() => {
                    switchView("cart");
                    clearToast();
                  }}
                  className="text-accent font-medium whitespace-nowrap cursor-pointer hover:underline"
                >
                  View Cart &rarr;
                </button>
              )}
              {/* One-tap "had it" upgrade. Fires after a drop; tapping
                  it records an external purchase so the cadence math
                  advances (next-restock pushes weeks out instead of
                  just one cycle). Ignoring keeps the default Tier-0
                  skip behavior. */}
              {wsToast.action === "mark_have_it" && wsToast.context?.item_id && (
                <button
                  type="button"
                  onClick={() => {
                    sendAction("mark_have_it", {
                      item_id: wsToast.context.item_id,
                      name: wsToast.context.name,
                      list_item_id: wsToast.context.list_item_id,
                    });
                    trackEvent("mark_have_it", {
                      item_id: wsToast.context.item_id,
                      name: wsToast.context.name,
                    });
                    clearToast();
                  }}
                  className="text-emerald-300 font-medium whitespace-nowrap cursor-pointer hover:underline"
                >
                  🛒 Had it
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Sticky bottom action bar */}
      <StickyActionBar
        onOrderAction={handleOrderAction}
        onOpenChat={openChat}
        agentState={agentState}
        lastMessage={lastAssistantMessage}
        cartProgress={cartProgress}
        diff={listDiff}
        listState={conversation?.state}
        activeView={activeView}
        hasCart={hasCart}
        hasItems={visibleItemCount > 0}
        itemCount={visibleItemCount}
        onDismissCart={dismissCart}
        onCreateCart={createCart}
        whatsappEnabled={whatsappEnabled}
        feedbackRound={feedbackStatus?.status === "collecting" ? feedbackStatus : null}
        onRequestFeedback={requestFeedback}
      />

      {/* Chat slide-up panel */}
      <AnimatePresence>
        {chatOpen && (
          <ChatPanel
            open={chatOpen}
            onClose={closeChat}
            messages={messages}
            onSendMessage={handleSendMessage}
            onSendFeedback={handleFeedback}
            isTyping={isTyping}
            thinkingLabel={thinkingLabel}
          />
        )}
      </AnimatePresence>

      {/* Settings bottom sheet (theme + future preferences).
          Replaces the floating ThemeToggle that previously lived in
          the page's top-right corner and overlapped the conversation
          header on small screens. Triggered by the gear button inside
          ConversationHeader. */}
      <SettingsSheet
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        preferences={preferences}
        onDeactivatePreference={deactivatePreference}
      />

      {/* Clear-cart confirm. Reset on the Cart view used to wipe
          curated picks silently -- the button felt broken because
          "nothing happened" (the list just emptied). Now we explicitly
          name the cost ("N items") before the destructive action. */}
      <AnimatePresence>
        {clearConfirm && (
          <>
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.18 }}
              className="fixed inset-0 z-50 bg-surface-overlay"
              onClick={() => setClearConfirm(null)}
              aria-hidden="true"
            />
            <motion.div
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="clear-cart-title"
              initial={{ opacity: 0, scale: 0.95, y: 8 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 8 }}
              transition={{ duration: 0.18 }}
              className="fixed inset-0 z-50 flex items-center justify-center px-5 pointer-events-none"
            >
              <div className="bg-surface-card rounded-2xl border border-border shadow-2xl max-w-sm w-full p-5 pointer-events-auto">
                <h2 id="clear-cart-title" className="text-base font-semibold text-text-primary mb-1">
                  Clear cart?
                </h2>
                <p className="text-sm text-text-secondary mb-4">
                  This removes {clearConfirm.count} {clearConfirm.count === 1 ? "item" : "items"} from your cart. You can pick again from the Scheduled tab.
                </p>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setClearConfirm(null)}
                    className="px-4 py-2 rounded-full text-sm font-medium text-text-secondary hover:text-text-primary hover:bg-chip-bg transition-colors cursor-pointer min-h-[44px]"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={confirmClearCart}
                    className="px-4 py-2 rounded-full text-sm font-medium bg-status-error text-white hover:bg-status-error/90 transition-colors cursor-pointer min-h-[44px]"
                    autoFocus
                  >
                    Yes, clear
                  </button>
                </div>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>

      {/* Image bottom sheet */}
      <AnimatePresence>
        {bottomSheetItem && (
          <ImageBottomSheet
            item={bottomSheetItem}
            onClose={() => {
              setBottomSheetItem(null);
              trackEvent("bottom_sheet_close");
            }}
            onUpdateQuantity={updateQuantity}
            onRemoveItem={removeItem}
            onItemPref={handleItemPref}
            onFindAlternatives={handleFindAlternatives}
          />
        )}
      </AnimatePresence>

      {/* Alternatives bottom sheet. Opened from the "Find better"
          button on the item ImageBottomSheet. Drives both same-store
          and cross-store lookups, plus the cart-consolidation flow. */}
      <AnimatePresence>
        {alternativesData && alternativesSourceItem && (
          <AlternativesBottomSheet
            data={alternativesData}
            currentItem={alternativesSourceItem}
            onFindScope={handleScopeSwitch}
            onAccept={handleAcceptAlternative}
            onClose={closeAlternatives}
          />
        )}
      </AnimatePresence>

      {/* Product suggestion bottom sheet (for add-item via chat) */}
      <AnimatePresence>
        {suggestion && (
          <SuggestionBottomSheet
            suggestion={suggestion}
            onApprove={(catalogId, itemName, quantity) => {
              sendAction("add_suggestion", { catalogId, itemName, quantity });
            }}
            onNext={() => {
              sendAction("next_suggestion");
            }}
            onSkip={() => {
              sendAction("skip_suggestion");
            }}
            onClose={() => {
              sendAction("skip_suggestion");
            }}
          />
        )}
      </AnimatePresence>

      {/* Rating modal (full-screen overlay) */}
      <RatingModal
        open={ratingOpen}
        order={ratingOrder}
        itemFeedback={ratingItemFeedback}
        feedbackText={ratingFeedbackText}
        llmResponse={ratingLlmResponse}
        onItemFeedback={(orderItemId, feedback) => {
          setRatingItemFeedback((prev) => {
            const next = { ...prev };
            if (feedback) {
              next[orderItemId] = feedback;
            } else {
              delete next[orderItemId];
            }
            return next;
          });
        }}
        onFeedbackText={setRatingFeedbackText}
        onGetLlmFeedback={() => {
          // Build items array with names for LLM context
          const items = (ratingOrder?.items || [])
            .filter((i) => ratingItemFeedback[i.order_item_id])
            .map((i) => ({
              orderItemId: i.order_item_id,
              feedback: ratingItemFeedback[i.order_item_id],
              name: i.name,
            }));
          sendAction("get_llm_feedback", { text: ratingFeedbackText, items });
        }}
        onFinalize={() => {
          const orderId = ratingOrder?.order_id;
          if (!orderId) return;
          // Build items array
          const items = Object.entries(ratingItemFeedback).map(([id, fb]) => ({
            orderItemId: Number(id),
            feedback: fb,
          }));
          sendAction("submit_rating", {
            orderId,
            items,
            text: ratingFeedbackText,
            llmResponse: ratingLlmResponse || "",
          });
          setRatingOpen(false);
          setRatingItemFeedback({});
          setRatingFeedbackText("");
          setRatingOrderId(null);
          // Show success toast
          setToast({ text: "Thanks for reviewing your order!", type: "success" });
          setTimeout(() => setToast(null), 4000);
        }}
        onClose={() => setRatingOpen(false)}
      />

      {/* Toast notification */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: 50 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 50 }}
            className="fixed bottom-24 left-1/2 -translate-x-1/2 z-50 px-4 py-2.5 rounded-xl bg-emerald-600 text-white text-sm font-medium shadow-lg"
          >
            {toast.text}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
