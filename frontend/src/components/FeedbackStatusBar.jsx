import { motion, AnimatePresence } from "framer-motion";

const memberColors = {
  Sara: { bg: "bg-violet-100", text: "text-violet-700", dot: "bg-violet-500" },
  Max: { bg: "bg-teal-100", text: "text-teal-700", dot: "bg-teal-500" },
};

function MemberPill({ member }) {
  const colors = memberColors[member.name] || { bg: "bg-gray-100", text: "text-gray-700", dot: "bg-gray-500" };
  const statusIcon = {
    done: "\u2713",
    replied: "\u2713",
    notified: "\u23F3",
    pending: "\u23F3",
    no_reply: "\u2014",
  };

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${colors.bg} ${colors.text}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${colors.dot}`} />
      {member.name}
      <span className="opacity-70">{statusIcon[member.status] || ""}</span>
      {member.itemCount > 0 && (
        <span className="opacity-70">{member.itemCount} items</span>
      )}
    </span>
  );
}

export default function FeedbackStatusBar({ feedbackStatus, onCancel, onDismiss }) {
  if (!feedbackStatus) return null;

  const { status, members = [], remainingMin, pendingCount } = feedbackStatus;

  // Don't show for cancelled rounds
  if (status === "cancelled") return null;

  let statusText = "";
  let statusColor = "text-text-secondary";

  if (status === "collecting") {
    statusText = remainingMin != null ? `${remainingMin} min left` : "Collecting...";
    statusColor = "text-accent";
  } else if (status === "complete") {
    statusText = pendingCount > 0
      ? `Feedback complete \u00B7 ${pendingCount} pending`
      : "Feedback complete";
    statusColor = "text-emerald-600";
  } else if (status === "timeout") {
    const noReply = members.filter((m) => m.status === "no_reply");
    statusText = noReply.length > 0
      ? `${noReply.map((m) => m.name).join(", ")} didn't respond`
      : "Feedback timed out";
    statusColor = "text-text-tertiary";
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, height: 0 }}
        animate={{ opacity: 1, height: "auto" }}
        exit={{ opacity: 0, height: 0 }}
        className="bg-chip-bg rounded-xl px-3 py-2 mb-2.5"
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 flex-wrap">
            {members.map((m) => (
              <MemberPill key={m.name} member={m} />
            ))}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <span className={`text-[10px] font-medium ${statusColor}`}>
              {statusText}
            </span>
            {status === "collecting" && onCancel && (
              <button
                type="button"
                onClick={() => onCancel(feedbackStatus.roundId)}
                className="text-[10px] text-text-tertiary hover:text-status-error transition-colors cursor-pointer"
              >
                Cancel
              </button>
            )}
            {(status === "timeout" || status === "complete") && onDismiss && (
              <button
                type="button"
                onClick={onDismiss}
                className="w-4 h-4 flex items-center justify-center text-text-tertiary hover:text-text-secondary transition-colors cursor-pointer"
                aria-label="Dismiss"
              >
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                  <path d="M1.5 1.5L8.5 8.5M8.5 1.5L1.5 8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
