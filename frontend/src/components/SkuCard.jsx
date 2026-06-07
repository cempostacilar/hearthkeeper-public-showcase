import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";

const statusBorderColors = {
  unresolved: "border-l-status-error",
  price_change: "border-l-status-warning",
  substituted: "border-l-status-warning",
  matched: "border-l-transparent",
};

const statusLabels = {
  unresolved: "Needs decision",
  price_change: "Price changed",
  substituted: "Substituted",
};

const tagStyles = {
  positive: "bg-emerald-50 text-emerald-700 border-emerald-200",
  negative: "bg-red-50 text-red-600 border-red-200",
  info: "bg-blue-50 text-blue-600 border-blue-200",
};

function TagChips({ tags, max = 3 }) {
  if (!tags || tags.length === 0) return null;
  const visible = tags.slice(0, max);
  const overflow = tags.length - max;
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {visible.map((t) => (
        <span
          key={t.id}
          className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium leading-none border ${
            tagStyles[t.sentiment] || tagStyles.info
          }`}
        >
          {t.label}
        </span>
      ))}
      {overflow > 0 && (
        <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] text-text-tertiary leading-none">
          +{overflow}
        </span>
      )}
    </div>
  );
}

const avatarColors = [
  "bg-blue-100 text-blue-700",
  "bg-amber-100 text-amber-700",
  "bg-emerald-100 text-emerald-700",
  "bg-rose-100 text-rose-700",
  "bg-violet-100 text-violet-700",
  "bg-teal-100 text-teal-700",
];

function ItemAvatar({ item, onClick, isInCart }) {
  const [imgError, setImgError] = useState(false);
  const initial = item.name.charAt(0).toUpperCase();
  const colorIndex = item.id % avatarColors.length;
  const hasImage = item.image && !imgError;

  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick(item);
        trackEvent("image_click", { itemId: item.id, name: item.name });
      }}
      className={`relative w-8 h-8 rounded-lg flex items-center justify-center text-xs font-semibold flex-shrink-0 cursor-pointer overflow-hidden ${
        hasImage ? "bg-white dark:bg-surface-elevated" : avatarColors[colorIndex]
      }`}
    >
      {hasImage ? (
        <img
          src={item.image}
          alt={item.name}
          className="w-full h-full object-cover"
          onError={() => setImgError(true)}
        />
      ) : (
        initial
      )}
      {/* In-cart checkmark badge */}
      {isInCart && (
        <div className="absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full bg-emerald-500 flex items-center justify-center">
          <svg width="7" height="7" viewBox="0 0 16 16" fill="none" className="text-white">
            <path d="M3 8.5L6.5 12L13 4" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
      )}
    </button>
  );
}

export default function SkuCard({ item, onUpdateQuantity, onRemoveItem, onImageClick, showCherryPick = false, onCherryPick, isInCart = false }) {
  if (item.quantity <= 0) return null;

  const borderClass = statusBorderColors[item.status] || "border-l-transparent";
  const statusLabel = statusLabels[item.status];

  return (
    <div
      className={`flex items-start gap-2.5 py-2.5 first:pt-2.5 border-l-2 pl-3 -ml-1 ${borderClass}`}
    >
      <ItemAvatar item={item} onClick={onImageClick} isInCart={isInCart} />

      <div className="flex-1 min-w-0">
        {/* Line 1: Name + price */}
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-xs font-medium text-text-primary truncate leading-snug">
            {item.name}
            {item.quantity > 1 && (
              <span className="text-text-tertiary font-normal"> x{item.quantity}</span>
            )}
            {item.addedBy && !["system", "chat", "cherry_pick", "user"].includes(item.addedBy) && (
              <span className={`ml-1.5 px-1.5 py-0.5 rounded-full text-[9px] font-medium align-middle ${
                item.addedBy.toLowerCase() === "ecem" ? "bg-violet-100 text-violet-700"
                : item.addedBy.toLowerCase() === "irish" ? "bg-teal-100 text-teal-700"
                : "bg-gray-100 text-gray-700"
              }`}>
                {item.addedBy}
              </span>
            )}
          </p>
          <span className="flex items-center gap-1.5 flex-shrink-0">
            {/* Accepted-price-band verdict: how this pick compares to what we've
                historically accepted paying for this item's type. */}
            {item.priceVerdict === "outrageous" && (
              <span
                title={item.priceCallout || ""}
                className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-red-50 text-red-700 border border-red-200">
                ⚠️ Over usual
              </span>
            )}
            {item.priceVerdict === "premium" && (
              <span
                title={item.priceCallout || ""}
                className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-50 text-amber-700 border border-amber-200">
                Above usual
              </span>
            )}
            {item.isPricey && !item.priceVerdict && (
              <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-50 text-amber-700 border border-amber-200">
                💸 Pricey
              </span>
            )}
            <span className="text-xs text-text-secondary tabular-nums">
              {item.price.toFixed(2)}
            </span>
          </span>
        </div>

        {/* Line 2: Identifiers (brand · size) */}
        {item.identifiers && (
          <p className="text-[10px] text-text-tertiary truncate leading-tight">
            {item.identifiers}
          </p>
        )}

        {/* Price call-out: when this pick is above our usual accepted range, say
            so plainly with the usual price for context. */}
        {item.priceVerdict && item.priceCallout && (
          <p className={`text-[10px] leading-tight mt-0.5 ${
            item.priceVerdict === "outrageous" ? "text-status-error" : "text-amber-700"
          }`}>
            {item.priceCallout}
            {item.acceptedMedian ? ` (usual ~${item.acceptedMedian}/${item.acceptedUnit || ""})` : ""}
          </p>
        )}

        <TagChips tags={item.tags} max={3} />

        {(statusLabel || item.notes) && (
          <p className={`text-[11px] mt-0.5 font-medium ${
            item.status === "unresolved" ? "text-status-error"
            : item.notes?.startsWith("\u2191") ? "text-red-500"
            : item.notes?.startsWith("\u2193") ? "text-emerald-600"
            : "text-status-warning"
          }`}>
            {item.notes || statusLabel}
          </p>
        )}

        {/* Controls row */}
        <div className="flex items-center justify-between mt-1.5">
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => {
                onUpdateQuantity(item.id, -1);
                trackEvent("quantity_change", { itemId: item.id, delta: -1 });
              }}
              disabled={item.quantity <= 1}
              className="w-6 h-6 rounded-md flex items-center justify-center text-xs bg-chip-bg text-text-primary hover:bg-border transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-default"
            >
              -
            </button>
            <AnimatePresence mode="popLayout">
              <motion.span
                key={item.quantity}
                initial={{ scale: 1.3, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.7, opacity: 0 }}
                transition={{ duration: 0.15 }}
                className="w-7 text-center text-xs font-medium tabular-nums"
              >
                {item.quantity}
              </motion.span>
            </AnimatePresence>
            <button
              type="button"
              onClick={() => {
                onUpdateQuantity(item.id, 1);
                trackEvent("quantity_change", { itemId: item.id, delta: 1 });
              }}
              className="w-6 h-6 rounded-md flex items-center justify-center text-xs bg-chip-bg text-text-primary hover:bg-border transition-colors cursor-pointer"
            >
              +
            </button>
          </div>

          <div className="flex items-center gap-1">
            {showCherryPick && onCherryPick && (
              isInCart ? (
                <span className="px-2 h-6 rounded-md text-[10px] font-medium text-emerald-600 flex items-center gap-1 opacity-60">
                  <svg width="10" height="10" viewBox="0 0 16 16" fill="none">
                    <path d="M3 8.5L6.5 12L13 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  In Cart
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    onCherryPick(item.id);
                    trackEvent("cherry_pick", { itemId: item.id, name: item.name });
                  }}
                  className="px-2 h-6 rounded-md text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors cursor-pointer flex items-center gap-1"
                  aria-label={`Add ${item.name} to cart`}
                >
                  <svg width="10" height="10" viewBox="0 0 16 16" fill="none">
                    <path d="M8 3V13M3 8H13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                  </svg>
                  Cart
                </button>
              )
            )}
            <button
              type="button"
              onClick={() => {
                onRemoveItem(item.id);
                trackEvent("item_remove", { itemId: item.id, name: item.name });
              }}
              className="p-1 rounded-md text-text-tertiary hover:text-status-error hover:bg-status-error-bg transition-colors cursor-pointer"
              aria-label={`Remove ${item.name}`}
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
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
