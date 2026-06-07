import { useState, useEffect, useRef } from "react";
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

const tagStyles = {
  positive: "bg-emerald-50 text-emerald-700 border-emerald-200",
  negative: "bg-red-50 text-red-600 border-red-200",
  info: "bg-blue-50 text-blue-600 border-blue-200",
};

/**
 * ProductDetails -- shows brand/size/origin/organic + description +
 * ingredients (collapsible) + a link out to the vendor's product
 * page. Only renders if at least one of those fields is populated;
 * otherwise we don't show a "Details" header pointing to nothing.
 */
function ProductDetails({ item }) {
  const [ingExpanded, setIngExpanded] = useState(false);
  const rows = [];
  if (item.brand) rows.push(["Brand", item.brand]);
  if (item.size) rows.push(["Size", item.size]);
  if (item.originCountry) rows.push(["Origin", item.originCountry]);
  if (item.isOrganic) rows.push(["Organic", "Yes"]);

  const hasAnything = rows.length > 0
    || item.description
    || item.ingredients
    || item.productUrl;
  if (!hasAnything) return null;

  // Truncate long ingredient lists for first render.
  const ingredients = item.ingredients || "";
  const ingredientsLong = ingredients.length > 140;
  const ingredientsPreview = ingredientsLong && !ingExpanded
    ? ingredients.slice(0, 140).trim() + "…"
    : ingredients;

  return (
    <div className="mb-4 rounded-xl bg-surface-elevated border border-border px-4 py-3">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-text-tertiary mb-2">
        Details
      </h3>

      {rows.length > 0 && (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-text-tertiary">{k}</dt>
              <dd className="text-text-primary font-medium">{v}</dd>
            </div>
          ))}
        </dl>
      )}

      {item.description && (
        <div className={rows.length > 0 ? "mt-3 pt-3 border-t border-border-light" : ""}>
          <p className="text-xs text-text-secondary leading-relaxed whitespace-pre-line">
            {item.description}
          </p>
        </div>
      )}

      {ingredients && (
        <div className={(rows.length > 0 || item.description) ? "mt-3 pt-3 border-t border-border-light" : ""}>
          <p className="text-[11px] font-semibold uppercase tracking-wider text-text-tertiary mb-1">
            Ingredients
          </p>
          <p className="text-xs text-text-secondary leading-relaxed">
            {ingredientsPreview}
          </p>
          {ingredientsLong && (
            <button
              type="button"
              onClick={() => setIngExpanded((v) => !v)}
              className="mt-1 text-[11px] font-medium text-accent hover:underline cursor-pointer"
            >
              {ingExpanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      )}

      {item.productUrl && (
        <a
          href={item.productUrl}
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => trackEvent("product_url_click", { itemId: item.itemId, vendor: item.vendor })}
          className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:underline"
        >
          View at {item.vendor || "store"}
          <svg width="10" height="10" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M6 3H13V10M13 3L3 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </a>
      )}
    </div>
  );
}

export default function ImageBottomSheet({ item, onClose, onUpdateQuantity, onRemoveItem, onItemPref, onFindAlternatives }) {
  const [imgError, setImgError] = useState(false);
  const [wrongItemMode, setWrongItemMode] = useState(false);
  const [feedbackText, setFeedbackText] = useState("");
  const [isProcessing, setIsProcessing] = useState(null); // null | "block_brand" | "wrong_item"
  const inputRef = useRef(null);

  // Reset all states when item changes
  useEffect(() => {
    setImgError(false);
    setWrongItemMode(false);
    setFeedbackText("");
    setIsProcessing(null);
  }, [item?.id]);

  // Auto-focus text input when wrong item mode activates
  useEffect(() => {
    if (wrongItemMode && inputRef.current) {
      setTimeout(() => inputRef.current.focus(), 100);
    }
  }, [wrongItemMode]);

  if (!item) return null;

  const initial = item.name.charAt(0).toUpperCase();
  const colorIndex = item.id % avatarColors.length;
  const hasImage = item.image && !imgError;

  return (
    <AnimatePresence>
      {item && (
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
                trackEvent("bottom_sheet_dismiss", { method: "drag" });
              }
            }}
            className="fixed bottom-0 left-0 right-0 z-50 bg-surface-card rounded-t-2xl max-h-[85dvh] overflow-y-auto"
          >
            {/* Drag handle */}
            <div className="flex justify-center pt-3 pb-2">
              <div className="w-10 h-1 rounded-full bg-border" />
            </div>

            <div className="px-5 pb-6">
              {/* Large product image or avatar */}
              <div className="flex justify-center mb-5">
                {hasImage ? (
                  <div className="w-40 h-40 rounded-2xl overflow-hidden bg-white dark:bg-surface-elevated flex items-center justify-center">
                    <img
                      src={item.image}
                      alt={item.name}
                      className="w-full h-full object-contain"
                      onError={() => setImgError(true)}
                    />
                  </div>
                ) : (
                  <div
                    className={`w-40 h-40 rounded-2xl flex items-center justify-center text-5xl font-bold ${avatarColors[colorIndex]}`}
                  >
                    {initial}
                  </div>
                )}
              </div>

              {/* Product info */}
              <div className="flex items-start justify-between gap-3 mb-1">
                <h2 className="text-lg font-semibold text-text-primary leading-snug flex-1">
                  {item.name}
                </h2>
                <div className="text-right flex-shrink-0">
                  <p className="text-lg font-semibold text-text-primary tabular-nums">
                    <span className="text-sm font-normal text-text-tertiary mr-0.5">AED</span>
                    {item.price.toFixed(2)}
                  </p>
                  <p className="text-xs text-text-tertiary tabular-nums">
                    {item.pricePerUnit}
                  </p>
                  {item.isPricey && (
                    <span className="inline-flex items-center gap-1 mt-1 px-2 py-0.5 rounded-full border text-[10px] font-semibold bg-amber-50 text-amber-700 border-amber-200">
                      💸 Pricey{item.pricedPerUnit ? ` · ${item.pricedPerUnit}` : ""}
                    </span>
                  )}
                </div>
              </div>

              <p className="text-sm text-text-secondary mb-3">
                {item.vendor} · {item.identifiers}
              </p>

              {/* Quality tags */}
              {item.tags && item.tags.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-4">
                  {item.tags.map((t) => (
                    <span
                      key={t.id}
                      className={`inline-flex items-center px-2 py-1 rounded-lg text-xs font-medium border ${
                        tagStyles[t.sentiment] || tagStyles.info
                      }`}
                    >
                      {t.label}
                    </span>
                  ))}
                </div>
              )}

              {/* Product details panel. User feedback: "the bread is
                  missing description and details, we need all the
                  basic info from the vendor so we can assess." We
                  surface whatever the catalog has -- brand, size,
                  origin, organic, plain-text description, ingredients
                  (collapsible if long), and a link out to the vendor's
                  product page. */}
              <ProductDetails item={item} />

              {/* Product feedback actions */}
              {onItemPref && (
                <div className="mb-4">
                  <AnimatePresence mode="wait">
                    {wrongItemMode ? (
                      /* Wrong item text input mode */
                      <motion.div
                        key="wrong-input"
                        initial={{ opacity: 0, y: 8 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -8 }}
                        className="space-y-2"
                      >
                        <p className="text-xs font-medium text-text-secondary">
                          What should this be instead?
                        </p>
                        <div className="flex gap-2">
                          <input
                            ref={inputRef}
                            type="text"
                            value={feedbackText}
                            onChange={(e) => setFeedbackText(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && !isProcessing) {
                                onItemPref(item.itemId, "wrong_item", item.catalogId, {
                                  listItemId: item.id,
                                  feedbackText: feedbackText.trim(),
                                });
                                setIsProcessing("wrong_item");
                                trackEvent("item_pref", { type: "wrong_item", itemId: item.itemId, hasFeedback: !!feedbackText.trim() });
                              }
                            }}
                            placeholder="e.g. organic version, different size..."
                            disabled={!!isProcessing}
                            name="hk-item-note"
                            autoComplete="off"
                            autoCorrect="off"
                            autoCapitalize="sentences"
                            data-1p-ignore="true"
                            data-lpignore="true"
                            data-form-type="other"
                            // text-base (16px) avoids mobile auto-zoom on focus.
                            className="flex-1 px-3 py-2 rounded-xl text-base bg-surface border border-border focus:border-accent focus:outline-none placeholder:text-text-tertiary disabled:opacity-50"
                          />
                          <button
                            type="button"
                            disabled={!!isProcessing}
                            onClick={() => {
                              onItemPref(item.itemId, "wrong_item", item.catalogId, {
                                listItemId: item.id,
                                feedbackText: feedbackText.trim(),
                              });
                              setIsProcessing("wrong_item");
                              trackEvent("item_pref", { type: "wrong_item", itemId: item.itemId, hasFeedback: !!feedbackText.trim() });
                            }}
                            className="px-4 py-2 rounded-xl text-sm font-medium bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer disabled:opacity-50 flex items-center gap-1.5"
                          >
                            {isProcessing === "wrong_item" ? (
                              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                              </svg>
                            ) : null}
                            {isProcessing === "wrong_item" ? "Finding..." : "Replace"}
                          </button>
                        </div>
                        <button
                          type="button"
                          onClick={() => {
                            setWrongItemMode(false);
                            setFeedbackText("");
                          }}
                          disabled={!!isProcessing}
                          className="text-xs text-text-tertiary hover:text-text-secondary cursor-pointer disabled:opacity-50"
                        >
                          Cancel
                        </button>
                      </motion.div>
                    ) : (
                      /* Normal button row */
                      <motion.div
                        key="buttons"
                        initial={{ opacity: 0, y: -8 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: 8 }}
                        className="flex flex-wrap gap-2"
                      >
                        {/* Pin / Unpin */}
                        <button
                          type="button"
                          disabled={!!isProcessing}
                          onClick={() => {
                            onItemPref(item.itemId, item.isPinned ? "unpin_product" : "pin_product", item.catalogId, {
                              listItemId: item.id,
                            });
                            trackEvent("item_pref", { type: item.isPinned ? "unpin" : "pin", itemId: item.itemId });
                          }}
                          className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium transition-colors cursor-pointer disabled:opacity-50 ${
                            item.isPinned
                              ? "bg-accent/10 text-accent border border-accent/30"
                              : "bg-chip-bg text-text-secondary hover:bg-border"
                          }`}
                        >
                          <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                            <path d="M8 1L10 5.5L15 6.5L11.5 10L12.5 15L8 12.5L3.5 15L4.5 10L1 6.5L6 5.5L8 1Z"
                              stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round"
                              fill={item.isPinned ? "currentColor" : "none"} />
                          </svg>
                          {item.isPinned ? "Pinned" : "Always this"}
                        </button>

                        {/* Block brand */}
                        {item.brand && (
                          <button
                            type="button"
                            disabled={!!isProcessing}
                            onClick={() => {
                              onItemPref(item.itemId, "block_brand", item.brand, {
                                listItemId: item.id,
                              });
                              setIsProcessing("block_brand");
                              trackEvent("item_pref", { type: "block_brand", brand: item.brand, itemId: item.itemId });
                              // Don't close — backend removes item, list_data update auto-closes sheet
                            }}
                            className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium bg-chip-bg text-text-secondary hover:bg-status-error-bg hover:text-status-error transition-colors cursor-pointer disabled:opacity-50"
                          >
                            {isProcessing === "block_brand" ? (
                              <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                              </svg>
                            ) : (
                              <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                                <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.25" />
                                <path d="M3.5 12.5L12.5 3.5" stroke="currentColor" strokeWidth="1.25" />
                              </svg>
                            )}
                            {isProcessing === "block_brand" ? "Replacing..." : `Never ${item.brand}`}
                          </button>
                        )}

                        {/* Wrong item */}
                        <button
                          type="button"
                          disabled={!!isProcessing}
                          onClick={() => {
                            setWrongItemMode(true);
                            trackEvent("item_pref", { type: "wrong_item_start", itemId: item.itemId });
                          }}
                          className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium bg-chip-bg text-text-secondary hover:bg-status-warning-bg hover:text-status-warning transition-colors cursor-pointer disabled:opacity-50"
                        >
                          <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                            <path d="M8 5V8.5M8 11H8.01" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                            <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.25" />
                          </svg>
                          Wrong item
                        </button>

                        {/* Find a better option -- opens the
                            AlternativesBottomSheet. Closes this sheet
                            first so the user has a clean transition
                            instead of nested overlays. */}
                        {onFindAlternatives && (
                          <button
                            type="button"
                            disabled={!!isProcessing}
                            onClick={() => {
                              onFindAlternatives(item);
                              onClose();
                              trackEvent("find_alternatives_open", { itemId: item.itemId });
                            }}
                            className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors cursor-pointer disabled:opacity-50"
                          >
                            <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                              <path d="M2 8h12M12 4l4 4-4 4M2 3v10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                            Alternatives
                          </button>
                        )}
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              )}

              {/* Quantity + Remove actions — sticky footer so they stay visible
                  no matter how long the content is (was getting cut off). */}
              <div
                className="flex items-center justify-between sticky bottom-0 bg-surface-card border-t border-border -mx-5 px-5 mt-3"
                style={{ paddingTop: "0.75rem", paddingBottom: "calc(0.75rem + env(safe-area-inset-bottom))" }}
              >
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => {
                      onUpdateQuantity(item.id, -1);
                      trackEvent("quantity_change_sheet", { itemId: item.id, delta: -1 });
                    }}
                    disabled={item.quantity <= 1}
                    className="w-10 h-10 rounded-xl flex items-center justify-center text-base bg-chip-bg text-text-primary hover:bg-border transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-default"
                  >
                    -
                  </button>
                  <span className="w-10 text-center text-base font-semibold tabular-nums">
                    {item.quantity}
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      onUpdateQuantity(item.id, 1);
                      trackEvent("quantity_change_sheet", { itemId: item.id, delta: 1 });
                    }}
                    className="w-10 h-10 rounded-xl flex items-center justify-center text-base bg-chip-bg text-text-primary hover:bg-border transition-colors cursor-pointer"
                  >
                    +
                  </button>
                </div>

                <button
                  type="button"
                  onClick={() => {
                    onRemoveItem(item.id);
                    onClose();
                    trackEvent("item_remove_sheet", { itemId: item.id, name: item.name });
                  }}
                  className="flex items-center gap-1.5 px-4 py-2.5 rounded-xl text-sm text-status-error bg-status-error-bg hover:bg-status-error/10 transition-colors cursor-pointer"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <path
                      d="M5.5 2H10.5M2 4H14M12.6667 4L12.1111 12.0712C12.0482 12.9527 11.3061 13.6367 10.4222 13.6367H5.57778C4.6939 13.6367 3.9518 12.9527 3.88889 12.0712L3.33333 4M6.5 7V10.5M9.5 7V10.5"
                      stroke="currentColor"
                      strokeWidth="1.25"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  Remove
                </button>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
