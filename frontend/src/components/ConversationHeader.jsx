import { motion } from "framer-motion";
import { trackEvent } from "../analytics";

/**
 * Conversation summary card -- the top-of-screen "what's the agent
 * thinking" tile. Tapping the card opens the chat panel. A settings
 * gear in the top-right opens the settings sheet (theme + future
 * preferences).
 *
 * Structure: the gear button lives OUTSIDE the wrapping card-button
 * as an absolutely-positioned sibling. Nesting one <button> inside
 * another is invalid HTML and confuses screen readers, so we layer
 * them with a relative parent and absolute positioning instead.
 *
 * The former top-right chat-bubble icon was removed:
 *   - The whole card is the chat opener; the icon was redundant.
 *   - The "Last message" preview row below has its own chat-bubble.
 *   - Removing it frees corner space for the new gear button.
 */
export default function ConversationHeader({
  conversation,
  messages,
  onOpenChat,
  onOpenSettings,
}) {
  const lastAssistantMsg = [...messages].reverse().find((m) => m.role === "assistant");

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => {
          onOpenChat();
          trackEvent("header_open_chat");
        }}
        aria-label="Open chat with Hearthkeeper"
        className="w-full text-left bg-surface-card rounded-xl border border-border px-4 py-3 pr-14 cursor-pointer hover:border-accent/30 transition-colors group"
      >
        <div className="flex items-center justify-between">
          <div className="min-w-0 flex-1">
            <h1 className="text-base font-semibold text-text-primary truncate">
              {conversation.title}
            </h1>
            <p className="text-sm text-text-secondary mt-0.5 truncate">
              {conversation.description}
            </p>
          </div>
          {messages.length > 0 && (
            <span
              aria-label={`${messages.length} chat message${messages.length === 1 ? "" : "s"}`}
              className="ml-3 w-5 h-5 rounded-full bg-accent/10 flex items-center justify-center text-[10px] font-semibold text-accent tabular-nums flex-shrink-0"
            >
              {messages.length}
            </span>
          )}
        </div>

        {/* Last message preview */}
        {lastAssistantMsg && (
          <motion.div
            initial={{ opacity: 0, y: 2 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-2 pt-2 border-t border-border-light"
          >
            <div className="flex items-start gap-2">
              <div className="w-4 h-4 rounded-full bg-accent/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                <svg width="8" height="8" viewBox="0 0 16 16" fill="none" className="text-accent" aria-hidden="true">
                  <path
                    d="M8 1C4.134 1 1 3.91 1 7.5c0 1.95.936 3.686 2.4 4.84V15l2.546-1.398A7.67 7.67 0 008 14c3.866 0 7-2.91 7-6.5S11.866 1 8 1z"
                    stroke="currentColor"
                    strokeWidth="1.25"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
              <p className="text-xs text-text-secondary leading-relaxed line-clamp-2 flex-1 overflow-hidden">
                {lastAssistantMsg.text.split("\n")[0].slice(0, 120)}
              </p>
            </div>
          </motion.div>
        )}
      </button>

      {/* Settings gear -- sibling of the card-button so we don't nest
          interactive elements (illegal HTML + screen-reader confusion).
          Replaces the previous floating ThemeToggle in the page's
          top-right corner, which overlapped the message-count badge
          on small screens. */}
      {onOpenSettings && (
        <button
          type="button"
          onClick={onOpenSettings}
          aria-label="Open settings"
          className="absolute top-2 right-2 w-9 h-9 rounded-full flex items-center justify-center text-text-tertiary hover:text-text-primary hover:bg-chip-bg transition-colors cursor-pointer"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2" strokeLinecap="round"
               strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      )}
    </div>
  );
}
