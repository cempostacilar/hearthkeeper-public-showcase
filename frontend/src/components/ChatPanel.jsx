import { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";

function TypingIndicator({ label }) {
  return (
    <div className="flex items-start gap-2 mb-3">
      <div className="w-6 h-6 rounded-full bg-accent/10 flex items-center justify-center flex-shrink-0">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="text-accent">
          <path
            d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z"
            stroke="currentColor"
            strokeWidth="1.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <div className="bg-border-light rounded-2xl rounded-tl-md px-4 py-2.5">
        <div className="flex items-center gap-1.5">
          {[0, 1, 2].map((i) => (
            <motion.span
              key={i}
              className="w-1.5 h-1.5 rounded-full bg-text-tertiary"
              animate={{ opacity: [0.3, 1, 0.3] }}
              transition={{
                duration: 1,
                repeat: Infinity,
                delay: i * 0.2,
              }}
            />
          ))}
          <AnimatePresence mode="wait">
            {label && (
              <motion.span
                key={label}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.2 }}
                className="text-xs text-text-tertiary ml-1"
              >
                {label}
              </motion.span>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}

/** Feedback icons row: thumbs up, thumbs down, alert */
function FeedbackRow({ msgId, onFeedback }) {
  const [selected, setSelected] = useState(null); // "up" | "down" | "alert" | null
  const [showAlertDialog, setShowAlertDialog] = useState(false);
  const [alertText, setAlertText] = useState("");
  const alertInputRef = useRef(null);

  useEffect(() => {
    if (showAlertDialog && alertInputRef.current) {
      const t = setTimeout(() => alertInputRef.current.focus(), 100);
      return () => clearTimeout(t);
    }
  }, [showAlertDialog]);

  const handleVote = (vote) => {
    if (vote === "alert") {
      setShowAlertDialog(true);
      return;
    }
    setSelected(vote);
    onFeedback(msgId, vote, null);
    trackEvent("feedback_vote", { msgId, vote });
  };

  const handleAlertSubmit = () => {
    const text = alertText.trim();
    if (!text) return;
    setSelected("alert");
    setShowAlertDialog(false);
    onFeedback(msgId, "alert", text);
    trackEvent("feedback_alert", { msgId, text });
    setAlertText("");
  };

  const btnBase =
    "p-1.5 rounded-md transition-all duration-150 cursor-pointer ";
  const btnIdle =
    "text-text-tertiary/40 hover:text-text-tertiary hover:bg-chip-bg ";
  const btnActive = (color) =>
    `${color} scale-110 `;

  return (
    <>
      <div className="flex items-center gap-0.5 mt-1 ml-1">
        {/* Thumbs up */}
        <button
          type="button"
          onClick={() => handleVote("up")}
          className={
            btnBase +
            (selected === "up"
              ? btnActive("text-green-500")
              : selected
              ? "text-text-tertiary/20 cursor-default"
              : btnIdle)
          }
          disabled={selected && selected !== "up"}
          aria-label="Good response"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 10v12M15 5.88L14 10h5.83a2 2 0 011.92 2.56l-2.33 8A2 2 0 0117.5 22H4a2 2 0 01-2-2v-8a2 2 0 012-2h2.76a2 2 0 001.79-1.11L12 2a3.13 3.13 0 013 3.88z" />
          </svg>
        </button>

        {/* Thumbs down */}
        <button
          type="button"
          onClick={() => handleVote("down")}
          className={
            btnBase +
            (selected === "down"
              ? btnActive("text-red-400")
              : selected
              ? "text-text-tertiary/20 cursor-default"
              : btnIdle)
          }
          disabled={selected && selected !== "down"}
          aria-label="Bad response"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M17 14V2M9 18.12L10 14H4.17a2 2 0 01-1.92-2.56l2.33-8A2 2 0 016.5 2H20a2 2 0 012 2v8a2 2 0 01-2 2h-2.76a2 2 0 00-1.79 1.11L12 22a3.13 3.13 0 01-3-3.88z" />
          </svg>
        </button>

        {/* Alert / needs fixing */}
        <button
          type="button"
          onClick={() => handleVote("alert")}
          className={
            btnBase +
            (selected === "alert"
              ? btnActive("text-amber-500")
              : selected
              ? "text-text-tertiary/20 cursor-default"
              : btnIdle)
          }
          disabled={selected && selected !== "alert"}
          aria-label="Report issue"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
          </svg>
        </button>
      </div>

      {/* Alert detail dialog */}
      <AnimatePresence>
        {showAlertDialog && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden ml-1 mt-1"
          >
            <div className="bg-surface-card border border-border rounded-xl p-3">
              <p className="text-xs font-medium text-text-secondary mb-2">
                What went wrong? What should have happened?
              </p>
              <textarea
                ref={alertInputRef}
                value={alertText}
                onChange={(e) => setAlertText(e.target.value)}
                placeholder="e.g. It suggested the wrong brand, I always buy organic..."
                rows={3}
                name="hk-feedback-note"
                autoComplete="off"
                autoCapitalize="sentences"
                spellCheck={true}
                data-1p-ignore="true"
                data-lpignore="true"
                data-form-type="other"
                // text-base (16px) avoids mobile auto-zoom on focus.
                className="w-full bg-chip-bg rounded-lg border border-border px-3 py-2 text-base text-text-primary placeholder:text-text-tertiary outline-none focus:ring-2 focus:ring-accent/30 resize-none"
              />
              <div className="flex items-center justify-end gap-2 mt-2">
                <button
                  type="button"
                  onClick={() => {
                    setShowAlertDialog(false);
                    setAlertText("");
                  }}
                  className="px-3 py-1.5 text-xs text-text-tertiary hover:text-text-primary rounded-lg hover:bg-chip-bg transition-colors cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleAlertSubmit}
                  disabled={!alertText.trim()}
                  className="px-3 py-1.5 text-xs bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-30 transition-colors cursor-pointer disabled:cursor-default"
                >
                  Submit
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

export default function ChatPanel({ open, onClose, messages, onSendMessage, onSendFeedback, isTyping, thinkingLabel }) {
  const [input, setInput] = useState("");
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  // Smooth-scroll to bottom when messages change (with a short delay so
  // the user can see the new message arriving instead of a jarring jump)
  const prevMsgCount = useRef(0);
  useEffect(() => {
    if (!scrollRef.current) return;
    const el = scrollRef.current;
    const isNewMessage = messages.length > prevMsgCount.current;
    prevMsgCount.current = messages.length;

    // Small delay so the DOM renders the new bubble first
    const timer = setTimeout(() => {
      el.scrollTo({
        top: el.scrollHeight,
        behavior: isNewMessage ? "smooth" : "auto",
      });
    }, isNewMessage ? 150 : 0);

    return () => clearTimeout(timer);
  }, [messages, isTyping]);

  // Focus input when panel opens
  useEffect(() => {
    if (open && inputRef.current) {
      const timer = setTimeout(() => inputRef.current.focus(), 350);
      return () => clearTimeout(timer);
    }
  }, [open]);

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    onSendMessage(text);
    setInput("");
    trackEvent("chat_panel_send", { text });
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <>
      {/* Backdrop */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: open ? 1 : 0 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        onClick={onClose}
        className="fixed inset-0 z-40 bg-surface-overlay"
        style={{ pointerEvents: open ? "auto" : "none" }}
      />

      {/* Panel */}
      <motion.div
        initial={{ y: "100%" }}
        animate={{ y: open ? "0%" : "100%" }}
        exit={{ y: "100%" }}
        transition={{ type: "spring", damping: 32, stiffness: 300 }}
        drag="y"
        dragConstraints={{ top: 0, bottom: 0 }}
        dragElastic={0.15}
        onDragEnd={(_, info) => {
          if (info.offset.y > 120 || info.velocity.y > 400) {
            onClose();
            trackEvent("chat_panel_dismiss", { method: "drag" });
          }
        }}
        className="fixed bottom-0 left-0 right-0 z-50 bg-surface-card rounded-t-2xl flex flex-col"
        style={{ height: "75vh", maxHeight: "75vh" }}
      >
        {/* Drag handle + header */}
        <div className="flex-shrink-0">
          <div className="flex justify-center pt-3 pb-1">
            <div className="w-10 h-1 rounded-full bg-border" />
          </div>
          <div className="flex items-center justify-between px-4 py-2 border-b border-border-light">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-full bg-accent/10 flex items-center justify-center">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="text-accent">
                  <path
                    d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z"
                    stroke="currentColor"
                    strokeWidth="1.25"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
              <div>
                <h2 className="text-sm font-semibold text-text-primary">Hearthkeeper</h2>
                <p className="text-[11px] text-text-tertiary">AI assistant</p>
              </div>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="p-2 rounded-lg text-text-tertiary hover:text-text-primary hover:bg-chip-bg transition-colors cursor-pointer"
              aria-label="Close chat"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path
                  d="M12 4L4 12M4 4L12 12"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          </div>
        </div>

        {/* Messages scroll area */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
          <div className="flex flex-col gap-3">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"}`}
              >
                {msg.role === "assistant" && (
                  <div className="flex items-center gap-1.5 mb-1">
                    <div className="w-5 h-5 rounded-full bg-accent/10 flex items-center justify-center">
                      <svg width="10" height="10" viewBox="0 0 16 16" fill="none" className="text-accent">
                        <path
                          d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z"
                          stroke="currentColor"
                          strokeWidth="1.25"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </div>
                    <span className="text-[11px] text-text-tertiary">
                      Hearthkeeper · {msg.time}
                    </span>
                  </div>
                )}
                <div
                  className={`text-sm leading-relaxed px-3.5 py-2.5 max-w-[85%] whitespace-pre-wrap ${
                    msg.role === "user"
                      ? "bg-accent text-text-inverse rounded-2xl rounded-br-md"
                      : "bg-border-light text-text-primary rounded-2xl rounded-tl-md"
                  }`}
                >
                  {msg.text}
                </div>
                {/* Feedback icons for assistant messages */}
                {msg.role === "assistant" && onSendFeedback && (
                  <FeedbackRow msgId={msg.id} onFeedback={onSendFeedback} />
                )}
                {msg.role === "user" && (
                  <span className="text-[11px] text-text-tertiary mt-1 mr-1">
                    {msg.time}
                  </span>
                )}
              </div>
            ))}
            {isTyping && <TypingIndicator label={thinkingLabel} />}
          </div>
        </div>

        {/* Input area — safe-area-inset for iPhone notch/home bar */}
        <div className="flex-shrink-0 border-t border-border-light px-3 py-2.5 bg-surface-card"
          style={{ paddingBottom: "max(0.625rem, env(safe-area-inset-bottom))" }}>
          <div className="flex items-center gap-1.5 w-full min-w-0">
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Message Hearthkeeper..."
              // text-base (16px) so iOS/Android don't auto-zoom on focus
              // (which was hiding the Send button). Autofill-suppressing
              // attrs stop Edge/Chrome offering the password/card/address
              // bar on this free-text chat field.
              name="hk-chat-message"
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="sentences"
              spellCheck={true}
              inputMode="text"
              enterKeyHint="send"
              data-1p-ignore="true"
              data-lpignore="true"
              data-form-type="other"
              className="flex-1 min-w-0 bg-chip-bg rounded-xl px-3 py-2.5 text-base text-text-primary placeholder:text-text-tertiary outline-none focus:ring-2 focus:ring-accent/30 transition-shadow"
            />
            <button
              type="button"
              onClick={handleSend}
              disabled={!input.trim()}
              className="flex-shrink-0 p-2.5 rounded-xl bg-accent text-text-inverse hover:bg-accent-hover disabled:opacity-30 transition-colors cursor-pointer disabled:cursor-default"
              aria-label="Send message"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path
                  d="M14 2L7.5 8.5M14 2L9.5 14L7.5 8.5M14 2L2 6.5L7.5 8.5"
                  stroke="currentColor"
                  strokeWidth="1.25"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          </div>
        </div>
      </motion.div>
    </>
  );
}
