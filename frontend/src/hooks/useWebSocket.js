/**
 * Hearthkeeper - WebSocket Hook
 *
 * Manages the WebSocket connection to the FastAPI backend.
 * Handles message routing, reconnection, and exposes all state
 * the React frontend needs.
 *
 * Message types handled:
 *   Inbound: agent, user, list_data, item_update, item_removed,
 *            state_change, cart_complete, cart_progress, chat_history, status
 *   Outbound: text, action, feedback
 */

import { useState, useEffect, useRef, useCallback } from "react";

// Connection states
export const ConnectionState = {
  CONNECTING: "connecting",
  CONNECTED: "connected",
  DISCONNECTED: "disconnected",
  RECONNECTING: "reconnecting",
};

// Reconnect with exponential backoff
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16000];

/**
 * Apply an incremental update to one item in a list. Accepts either a bare
 * vendor array (legacy listData) or a { vendors: [...] } list object
 * (suggestionData / cartData), and returns the same shape it was given.
 * Null/undefined is passed through unchanged.
 */
function applyItemUpdate(list, listItemId, changes) {
  if (!list) return list;
  // A { vendors: [...] } list object — update vendors, preserve other fields.
  if (!Array.isArray(list) && Array.isArray(list.vendors)) {
    return { ...list, vendors: applyItemUpdate(list.vendors, listItemId, changes) };
  }
  if (!Array.isArray(list)) return list;  // unknown shape — leave as-is
  return list.map((vendor) => ({
    ...vendor,
    categories: vendor.categories.map((cat) => ({
      ...cat,
      items: cat.items.map((item) =>
        item.id === listItemId ? { ...item, ...changes } : item
      ),
    })),
  }));
}

export function useWebSocket() {
  const [connectionState, setConnectionState] = useState(ConnectionState.CONNECTING);
  const [messages, setMessages] = useState([]);
  const [listData, setListData] = useState(null);
  const [listDiff, setListDiff] = useState(null);
  const [conversation, setConversation] = useState(null);
  const [highlights, setHighlights] = useState([]);
  const [agentState, setAgentState] = useState(null);
  const [isTyping, setIsTyping] = useState(false);
  const [thinkingLabel, setThinkingLabel] = useState(null); // e.g. "Comparing products across stores..."
  const [unreadNudges, setUnreadNudges] = useState(0);
  const [cartResult, setCartResult] = useState(null); // {success, message}
  const [cartProgress, setCartProgress] = useState([]); // step-by-step progress during building
  const [cartFailedItems, setCartFailedItems] = useState([]); // items that failed in the last build
  // Alternatives lookup state. Shape:
  //   null                                         -- sheet closed
  //   { listItemId, scope, loading: true }         -- request in flight
  //   { listItemId, scope, alternatives: [...] }   -- result arrived
  const [alternativesData, setAlternativesData] = useState(null);
  const [suggestion, setSuggestion] = useState(null); // product suggestion for add-item flow
  const [orderTracking, setOrderTracking] = useState({ activeOrder: null, deliveredOrder: null, deliveredOrders: [] });
  const [ratingLlmResponse, setRatingLlmResponse] = useState(null);

  // Two-entity model: Suggestions + Cart
  const [suggestionData, setSuggestionData] = useState(null);
  const [cartData, setCartData] = useState(null);
  const [suggestionListId, setSuggestionListId] = useState(null);
  const [cartListId, setCartListId] = useState(null);
  const [activeView, setActiveView] = useState("suggestions"); // "suggestions" | "cart"
  const [suggestionConversation, setSuggestionConversation] = useState(null);
  const [cartConversation, setCartConversation] = useState(null);

  const [wsToast, setWsToast] = useState(null); // {text, action?}

  // WhatsApp feedback system
  const [feedbackStatus, setFeedbackStatus] = useState(null); // round status
  const [feedbackItems, setFeedbackItems] = useState([]); // pending suggestions
  const [whatsappEnabled, setWhatsappEnabled] = useState(false);

  // A12 — Recently-learned panel (cadence nudges + auto-stops)
  // null = not loaded yet; {} = loaded empty
  const [learningItems, setLearningItems] = useState(null);

  // A13 — full preferences memory (Settings -> Preferences section)
  // Lazy-loaded the first time the Settings sheet opens.
  // null = not loaded yet; {} = loaded empty.
  const [preferences, setPreferences] = useState(null);

  const wsRef = useRef(null);
  const reconnectAttempt = useRef(0);
  const reconnectTimer = useRef(null);
  const initialLoadDone = useRef(false);

  const connect = useCallback(() => {
    // Build WebSocket URL relative to current host
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/ws`;

    const ws = new WebSocket(url);
    wsRef.current = ws;
    setConnectionState(ConnectionState.CONNECTING);

    ws.onopen = () => {
      setConnectionState(ConnectionState.CONNECTED);
      reconnectAttempt.current = 0;
      // Pull fresh order tracking on every (re)connect so status is never
      // stale after a wake/reconnect (belt-and-suspenders with the server's
      // connect-time push).
      try {
        ws.send(JSON.stringify({ type: "action", action: "get_order_tracking" }));
      } catch (e) {
        /* ignore -- onmessage/onclose handle connection issues */
      }
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleMessage(data);
      } catch (err) {
        console.warn("Hearthkeeper WS: failed to parse message", err);
      }
    };

    ws.onclose = (event) => {
      // 1008 = server rejected the connection for auth (session missing/expired).
      // Reconnecting won't help — send the user to the login page.
      if (event.code === 1008) {
        window.location.href = "/login";
        return;
      }
      setConnectionState(ConnectionState.DISCONNECTED);
      attemptReconnect();
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  const handleMessage = useCallback((data) => {
    switch (data.type) {
      case "agent":
        setMessages((prev) => [
          ...prev,
          {
            id: Date.now(),
            role: "assistant",
            text: data.text,
            time: data.time,
            nudge: data.nudge || false,
            priority: data.priority || "normal",
          },
        ]);
        setIsTyping(false);
        setThinkingLabel(null);
        if (data.nudge) {
          setUnreadNudges((prev) => prev + 1);
        }
        break;

      case "agent_progress":
        // New unified progress indicator (debounced from backend)
        setIsTyping(true);
        setThinkingLabel(data.label || "Thinking...");
        break;

      case "agent_thinking":
        // Legacy backward compat
        setIsTyping(true);
        setThinkingLabel(data.label || "Thinking...");
        break;

      case "tool_activity":
        // Legacy backward compat
        setIsTyping(true);
        setThinkingLabel(data.label || "Working...");
        break;

      case "user":
        setMessages((prev) => [
          ...prev,
          { id: Date.now(), role: "user", text: data.text, time: data.time },
        ]);
        break;

      case "list_data":
        // Backward-compat: vendors is the "primary" list (cart or suggestion)
        if (data.vendors) setListData(data.vendors);
        if (data.diff !== undefined) setListDiff(data.diff);
        if (data.conversation) setConversation(data.conversation);
        if (data.highlights) setHighlights(data.highlights);
        if (data.agentState) setAgentState(data.agentState);

        // Two-entity model: separate suggestion + cart data
        if (data.suggestions !== undefined) setSuggestionData(data.suggestions);
        if (data.cart !== undefined) setCartData(data.cart);
        if (data.suggestionListId !== undefined) setSuggestionListId(data.suggestionListId);
        if (data.cartListId !== undefined) setCartListId(data.cartListId);
        // Per-view conversation headers
        if (data.suggestionConversation !== undefined) setSuggestionConversation(data.suggestionConversation);
        if (data.cartConversation !== undefined) setCartConversation(data.cartConversation);

        // Auto-select view ONLY on initial load (not every list_data refresh).
        // Always prefer Scheduled (suggestions) when it exists -- it's the
        // agent's plan and the natural landing surface. Only fall back to
        // Cart if there are no suggestions at all.
        if (!initialLoadDone.current) {
          if (data.suggestionListId || data.suggestions) {
            setActiveView("suggestions");
          } else if (data.cartListId || data.cart) {
            setActiveView("cart");
          }
          initialLoadDone.current = true;
        }
        break;

      case "view_switch":
        // Backend-initiated tab switch (e.g. after create_cart, dismiss_cart, cherry_pick)
        if (data.view) setActiveView(data.view);
        break;

      case "item_update":
        if (data.listItemId != null && data.changes) {
          // Update all three list states: the dual-list UI renders
          // suggestionData / cartData; listData is the legacy single list.
          setListData((prev) => applyItemUpdate(prev, data.listItemId, data.changes));
          setSuggestionData((prev) => applyItemUpdate(prev, data.listItemId, data.changes));
          setCartData((prev) => applyItemUpdate(prev, data.listItemId, data.changes));
        }
        break;

      case "item_removed":
        if (data.listItemId != null) {
          setListData((prev) => applyItemUpdate(prev, data.listItemId, { quantity: 0 }));
          setSuggestionData((prev) => applyItemUpdate(prev, data.listItemId, { quantity: 0 }));
          setCartData((prev) => applyItemUpdate(prev, data.listItemId, { quantity: 0 }));
        }
        break;

      case "cart_complete":
        // Terminal signal for a build turn — clear the typing indicator so
        // the spinner never hangs if no trailing "agent" message follows.
        setIsTyping(false);
        setThinkingLabel(null);
        setCartResult({
          success: data.success,
          message: data.message,
        });
        // Snapshot the failed items from the progress stream before
        // clearing it -- the UI shows them inline in the result banner
        // so the user can see WHICH items the agent couldn't add, not
        // just that "N failed".
        setCartProgress((prev) => {
          const failed = prev
            .filter((s) => s.step === "item_fail")
            .map((s) => ({
              name: s.item || "Unknown",
              store: s.store || "",
              reason: s.reason || "Couldn't add to cart",
            }));
          setCartFailedItems(failed);
          return [];
        });
        break;

      case "cart_progress":
        // Real-time step-by-step progress during cart building
        setCartProgress((prev) => [...prev, {
          step: data.step,
          store: data.store,
          item: data.item,
          status: data.status,
          reason: data.reason,
          product: data.product,
          price: data.price,
          index: data.index,
          total: data.total,
          message: data.message,
          time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        }]);
        break;

      case "chat_history":
        // Load persisted messages from previous sessions
        if (data.messages && data.messages.length > 0) {
          setMessages((prev) => {
            // Prepend history before current messages, avoiding duplicates
            const historyMsgs = data.messages.map((m, i) => ({
              id: i + 1, // low IDs so they don't conflict with Date.now()
              role: m.role,
              text: m.text,
              time: m.time || "",
              history: true, // mark as historical
            }));
            return [...historyMsgs, ...prev];
          });
        }
        break;

      case "state_change":
        if (data.state) setAgentState(data.state);
        if (data.state === "building") {
          setIsTyping(true);
          setCartResult(null);
          setCartProgress([]); // reset progress for new build
        }
        if (data.state === "idle" || data.state === "done") setIsTyping(false);
        break;

      case "status":
        if (data.text === "building") setIsTyping(true);
        if (data.text === "idle") setIsTyping(false);
        break;

      case "suggestion":
        // Product suggestion for the add-item bottom sheet
        setSuggestion(data);
        break;

      case "suggestion_dismiss":
        // Close the suggestion bottom sheet
        setSuggestion(null);
        break;

      case "order_tracking":
        // Active + delivered order state from backend
        setOrderTracking({
          activeOrder: data.activeOrder || null,
          deliveredOrder: data.deliveredOrder || null,
          deliveredOrders: data.deliveredOrders || [],
        });
        break;

      case "order_status":
        // Real-time status update from the poller (re-fetch tracking data)
        if (data.order) {
          setOrderTracking((prev) => ({
            ...prev,
            activeOrder: data.order.status === "delivered" ? null : data.order,
          }));
        }
        break;

      case "rating_llm_response":
        // LLM conversational response to rating feedback text
        setRatingLlmResponse(data.response || null);
        break;

      case "toast":
        // Ephemeral toast notification from backend.
        // `context` carries payload for action buttons (e.g. for the
        // "🛒 Had it" chip on a remove toast, context is
        // {item_id, name, list_item_id}). App.jsx reads it on click.
        setWsToast({
          text: data.text,
          action: data.action || null,
          context: data.context || null,
        });
        break;

      case "alternatives_result":
        // Result of a "find alternatives" lookup. Only accept if the
        // open bottom sheet matches this listItemId+scope; otherwise
        // the user may have closed/changed it before the WS arrived.
        setAlternativesData((prev) => {
          if (!prev) return prev;
          if (prev.listItemId !== data.listItemId) return prev;
          if (prev.scope !== data.scope) return prev;
          return {
            listItemId: data.listItemId,
            scope: data.scope,
            alternatives: data.alternatives || [],
            loading: false,
          };
        });
        break;

      case "feedback_status":
        // WhatsApp feedback round status + items
        if (data.data !== undefined) setFeedbackStatus(data.data);
        if (data.items !== undefined) setFeedbackItems(data.items);
        if (data.whatsappEnabled !== undefined) setWhatsappEnabled(data.whatsappEnabled);
        break;

      case "learning_items":
        // A12 — Recently-learned panel state push.
        // Backend sends this in response to get_learning_items and
        // after every reset_preference write. Two arrays:
        //   cadence_adjustments: items with cadence_adjustment > 1.0
        //   stops:               active stop_suggesting prefs
        setLearningItems({
          cadence_adjustments: data.cadence_adjustments || [],
          stops: data.stops || [],
        });
        break;

      case "preferences":
        // A13 — Settings -> Preferences page snapshot.
        // Backend sends a dict keyed by pref_type. We store the whole
        // thing as-is so new pref types added later show up without
        // a frontend change. Excludes the "type" envelope key.
        {
          // eslint-disable-next-line no-unused-vars
          const { type: _t, ...prefs } = data;
          setPreferences(prefs);
        }
        break;

      default:
        break;
    }
  }, []);

  const attemptReconnect = useCallback(() => {
    const attempt = reconnectAttempt.current;
    setConnectionState(ConnectionState.RECONNECTING);
    // Escalate the backoff, then hold at the maximum — retry indefinitely so
    // a phone sleeping or a network drop never permanently strands the client.
    const delay = RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)];
    reconnectTimer.current = setTimeout(() => {
      reconnectAttempt.current += 1;
      connect();
    }, delay);
  }, [connect]);

  useEffect(() => {
    connect();
    // When the device wakes or the tab refocuses, reconnect immediately
    // instead of waiting out the backoff timer.
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        // Still connected but was backgrounded -- pull fresh order status so
        // re-opening the app never shows stale tracking (the poller only
        // pushes on a change while connected). Cheap, read-only refresh.
        try {
          ws.send(JSON.stringify({ type: "action", action: "get_order_tracking" }));
        } catch (e) {
          /* socket race -- the close/reconnect path below handles it */
        }
        return;
      }
      if (ws && ws.readyState !== WebSocket.CLOSED) return;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectAttempt.current = 0;
      connect();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      if (wsRef.current) wsRef.current.close();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, [connect]);

  // ── Outbound message senders ──

  const sendMessage = useCallback((text) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      // Include the tab the user is viewing so chat edits act on the list
      // they SEE (scheduled vs cart), not always the cart.
      wsRef.current.send(JSON.stringify({ text, view: activeView }));
      setIsTyping(true);
    }
  }, [activeView]);

  const sendAction = useCallback((action, payload = {}) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "action", action, ...payload }));
    }
  }, []);

  const sendFeedback = useCallback((msgId, vote) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "feedback", msg_id: msgId, vote }));
    }
  }, []);

  const clearNudges = useCallback(() => setUnreadNudges(0), []);

  // ── Cart lifecycle action senders ──

  const createCart = useCallback(() => {
    sendAction("create_cart");
  }, [sendAction]);

  const dismissCart = useCallback(() => {
    sendAction("dismiss_cart");
  }, [sendAction]);

  const cherryPick = useCallback((listItemId) => {
    sendAction("cherry_pick", { listItemId });
  }, [sendAction]);

  const switchView = useCallback((view) => {
    setActiveView(view);
  }, []);

  const clearToast = useCallback(() => setWsToast(null), []);

  const clearCartFailedItems = useCallback(() => setCartFailedItems([]), []);

  // Alternatives lookup. Setting `loading: true` immediately lets the
  // sheet open with a spinner while the WS round-trip completes.
  const findAlternatives = useCallback((listItemId, scope = "same_vendor") => {
    setAlternativesData({ listItemId, scope, alternatives: null, loading: true });
    sendAction("find_alternatives", { listItemId, scope });
  }, [sendAction]);

  const acceptAlternative = useCallback((listItemId, catalogId, scope) => {
    sendAction("accept_alternative", { listItemId, catalogId, scope });
    setAlternativesData(null);
  }, [sendAction]);

  const clearAlternatives = useCallback(() => setAlternativesData(null), []);

  // ── Feedback action senders ──

  const requestFeedback = useCallback(() => {
    sendAction("request_feedback");
  }, [sendAction]);

  const approveFeedback = useCallback((feedbackItemId) => {
    sendAction("approve_feedback", { feedbackItemId });
  }, [sendAction]);

  const rejectFeedback = useCallback((feedbackItemId) => {
    sendAction("reject_feedback", { feedbackItemId });
  }, [sendAction]);

  const cancelFeedback = useCallback((roundId) => {
    sendAction("cancel_feedback", { roundId });
  }, [sendAction]);

  // ── A12 Recently-learned action senders ──

  const getLearningItems = useCallback(() => {
    sendAction("get_learning_items");
  }, [sendAction]);

  const resetPreference = useCallback((itemId, kind = "all") => {
    sendAction("reset_preference", { itemId, kind });
  }, [sendAction]);

  // ── A13 Preferences page action senders ──

  const getPreferences = useCallback(() => {
    sendAction("get_preferences");
  }, [sendAction]);

  const deactivatePreference = useCallback((prefId) => {
    sendAction("deactivate_preference", { prefId });
  }, [sendAction]);

  return {
    connectionState,
    messages,
    listData,
    listDiff,
    conversation,
    highlights,
    agentState,
    isTyping,
    thinkingLabel,
    unreadNudges,
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
  };
}
