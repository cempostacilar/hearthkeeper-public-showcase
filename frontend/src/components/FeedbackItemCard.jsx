import { useState } from "react";
import { motion } from "framer-motion";
import { trackEvent } from "../analytics";

const memberColors = {
  Sara: { badge: "bg-violet-100 text-violet-700", border: "border-l-violet-400" },
  Max: { badge: "bg-teal-100 text-teal-700", border: "border-l-teal-400" },
};

export default function FeedbackItemCard({ item, onApprove, onReject }) {
  const [imgError, setImgError] = useState(false);

  const colors = memberColors[item.memberName] || {
    badge: "bg-gray-100 text-gray-700",
    border: "border-l-gray-400",
  };

  const isAdd = item.actionType === "add";
  const displayName = item.resolvedName || item.itemText;
  const hasImage = item.image && !imgError;

  // Skip non-pending items
  if (item.status !== "pending" && item.status !== "unresolved") return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      className={`border-l-2 ${colors.border} bg-surface-card rounded-lg px-3 py-2.5 mb-2`}
    >
      {/* Header: who + what */}
      <div className="flex items-center gap-1.5 mb-1.5">
        <span className={`inline-flex items-center px-1.5 py-0.5 rounded-full text-[9px] font-semibold ${colors.badge}`}>
          {item.memberName}
        </span>
        <span className="text-[10px] text-text-tertiary">
          wants to {isAdd ? "add" : "remove"}:
        </span>
      </div>

      {/* Item row */}
      <div className="flex items-center gap-2.5">
        {/* Image / initial */}
        <div className="w-8 h-8 rounded-lg flex items-center justify-center text-xs font-semibold flex-shrink-0 overflow-hidden bg-chip-bg">
          {hasImage ? (
            <img
              src={item.image}
              alt={displayName}
              className="w-full h-full object-cover"
              onError={() => setImgError(true)}
            />
          ) : (
            <span className="text-text-tertiary">
              {displayName.charAt(0).toUpperCase()}
            </span>
          )}
        </div>

        {/* Name + price */}
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium text-text-primary truncate">
            {displayName}
            {item.quantity > 1 && (
              <span className="text-text-tertiary font-normal"> x{item.quantity}</span>
            )}
          </p>
          {item.price != null && (
            <p className="text-[10px] text-text-tertiary">
              {item.price.toFixed(2)} AED
            </p>
          )}
          {item.status === "unresolved" && (
            <p className="text-[10px] text-status-error font-medium mt-0.5">
              Could not find in catalog
            </p>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            type="button"
            onClick={() => {
              onApprove(item.id);
              trackEvent("feedback_approve", { itemId: item.id, memberName: item.memberName });
            }}
            className="px-2.5 py-1 rounded-md text-[10px] font-medium bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200 transition-colors cursor-pointer"
          >
            {isAdd ? "Approve" : "Approve"}
          </button>
          <button
            type="button"
            onClick={() => {
              onReject(item.id);
              trackEvent("feedback_reject", { itemId: item.id, memberName: item.memberName });
            }}
            className="px-2.5 py-1 rounded-md text-[10px] font-medium bg-chip-bg text-text-tertiary hover:text-status-error hover:bg-red-50 transition-colors cursor-pointer"
          >
            {isAdd ? "Reject" : "Keep"}
          </button>
        </div>
      </div>
    </motion.div>
  );
}
