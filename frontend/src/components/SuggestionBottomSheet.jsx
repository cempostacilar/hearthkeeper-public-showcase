import { useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";

const avatarColors = [
  "bg-blue-100 text-blue-700",
  "bg-amber-100 text-amber-700",
  "bg-emerald-100 text-emerald-700",
  "bg-rose-100 text-rose-700",
  "bg-violet-100 text-violet-700",
  "bg-teal-100 text-teal-700",
];

/**
 * SuggestionBottomSheet — Product suggestion overlay for add-item flow.
 *
 * Shown when the user asks to add an unknown item via chat and the
 * backend finds matching products in the catalog. User can:
 *   - Approve the suggested product ("Add This")
 *   - See the next-ranked alternative ("Show Alternative")
 *   - Skip entirely ("Skip")
 *
 * Props:
 *   suggestion  – the suggestion object from WebSocket: {itemName, product, hasMore, ...}
 *   onApprove   – (catalogId, itemName, quantity) => void
 *   onNext      – () => void   (request next alternative)
 *   onSkip      – () => void   (close and skip)
 *   onClose     – () => void   (dismiss the sheet)
 */
export default function SuggestionBottomSheet({ suggestion, onApprove, onNext, onSkip, onClose }) {
  const [imgError, setImgError] = useState(false);

  // Reset image error when suggestion changes
  useEffect(() => { setImgError(false); }, [suggestion?.product?.catalogId]);

  if (!suggestion || !suggestion.product) return null;

  const { itemName, product, hasMore, isAlternative, currentIndex, totalOptions, quantity } = suggestion;
  const { catalogId, name, brand, price, size, image, isOrganic, storeName } = product;

  const initial = (name || itemName || "?").charAt(0).toUpperCase();
  const colorIndex = (catalogId || 0) % avatarColors.length;
  const hasImage = image && !imgError;

  const title = isAlternative
    ? `Alternative for ${itemName}`
    : `Suggestion for ${itemName}`;

  const subtitle = totalOptions > 1
    ? `${(currentIndex || 0) + 1} of ${totalOptions} options`
    : null;

  return (
    <AnimatePresence>
      {suggestion && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-surface-overlay"
          />

          {/* Sheet */}
          <motion.div
            initial={{ y: "100%" }}
            animate={{ y: "0%" }}
            exit={{ y: "100%" }}
            transition={{ type: "spring", damping: 30, stiffness: 300 }}
            drag="y"
            dragConstraints={{ top: 0, bottom: 0 }}
            dragElastic={0.2}
            onDragEnd={(_, info) => {
              if (info.offset.y > 100 || info.velocity.y > 500) {
                onClose();
                trackEvent("suggestion_sheet_dismiss", { method: "drag" });
              }
            }}
            className="fixed bottom-0 left-0 right-0 z-50 bg-surface-card rounded-t-2xl max-h-[70vh] overflow-y-auto"
            style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
          >
            {/* Drag handle */}
            <div className="flex justify-center pt-3 pb-2">
              <div className="w-10 h-1 rounded-full bg-border" />
            </div>

            <div className="px-5 pb-6">
              {/* Title */}
              <div className="mb-4">
                <h2 className="text-base font-semibold text-text-primary">{title}</h2>
                {subtitle && (
                  <p className="text-xs text-text-tertiary mt-0.5">{subtitle}</p>
                )}
              </div>

              {/* Product image or avatar */}
              <div className="flex justify-center mb-4">
                {hasImage ? (
                  <div className="w-36 h-36 rounded-2xl overflow-hidden bg-white dark:bg-surface-elevated flex items-center justify-center">
                    <img
                      src={image}
                      alt={name}
                      className="w-full h-full object-contain"
                      onError={() => setImgError(true)}
                    />
                  </div>
                ) : (
                  <div
                    className={`w-36 h-36 rounded-2xl flex items-center justify-center text-5xl font-bold ${avatarColors[colorIndex]}`}
                  >
                    {initial}
                  </div>
                )}
              </div>

              {/* Product info */}
              <div className="flex items-start justify-between gap-3 mb-1">
                <h3 className="text-lg font-semibold text-text-primary leading-snug flex-1">
                  {name}
                </h3>
                {price != null && (
                  <div className="text-right flex-shrink-0">
                    <p className="text-lg font-semibold text-text-primary tabular-nums">
                      <span className="text-sm font-normal text-text-tertiary mr-0.5">AED</span>
                      {Number(price).toFixed(2)}
                    </p>
                  </div>
                )}
              </div>

              {/* Meta line: brand, size, store */}
              <p className="text-sm text-text-secondary mb-3">
                {[brand, size, storeName].filter(Boolean).join(" · ")}
              </p>

              {/* Tags */}
              <div className="flex flex-wrap gap-1.5 mb-5">
                {isOrganic && (
                  <span className="inline-flex items-center px-2 py-1 rounded-lg text-xs font-medium border bg-emerald-50 text-emerald-700 border-emerald-200">
                    Organic
                  </span>
                )}
                {quantity > 1 && (
                  <span className="inline-flex items-center px-2 py-1 rounded-lg text-xs font-medium border bg-blue-50 text-blue-600 border-blue-200">
                    x{quantity}
                  </span>
                )}
              </div>

              {/* Action buttons */}
              <div className="flex gap-3">
                {/* Add This (primary) */}
                <button
                  type="button"
                  onClick={() => {
                    onApprove(catalogId, itemName, quantity || 1);
                    trackEvent("suggestion_approve", { catalogId, itemName });
                  }}
                  className="flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-xl text-sm font-semibold bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <path d="M3 8L6.5 11.5L13 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  Add This
                </button>

                {/* Show Alternative (secondary) */}
                {hasMore && (
                  <button
                    type="button"
                    onClick={() => {
                      onNext();
                      trackEvent("suggestion_next", { catalogId, itemName, currentIndex });
                    }}
                    className="flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-xl text-sm font-medium bg-chip-bg text-text-primary hover:bg-border transition-colors cursor-pointer"
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                    Alternative
                  </button>
                )}

                {/* Skip (tertiary) */}
                <button
                  type="button"
                  onClick={() => {
                    onSkip();
                    trackEvent("suggestion_skip", { itemName });
                  }}
                  className="px-4 py-3 rounded-xl text-sm text-text-tertiary hover:text-text-secondary hover:bg-chip-bg transition-colors cursor-pointer"
                >
                  Skip
                </button>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
