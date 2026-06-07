import { motion, AnimatePresence } from "framer-motion";
import SkuCard from "./SkuCard";
import { trackEvent } from "../analytics";

export default function CategoryRow({
  category,
  isExpanded,
  onToggle,
  onUpdateQuantity,
  onRemoveItem,
  onImageClick,
  showCherryPick = false,
  onCherryPick,
  cartItemNames = null,
}) {
  const itemCount = category.items.filter((i) => i.quantity > 0).length;
  const hasIssues = category.items.some(
    (i) => i.status === "unresolved" || i.status === "price_change" || i.status === "substituted"
  );

  return (
    <div className="rounded-xl overflow-hidden bg-surface-card border border-border">
      <button
        type="button"
        onClick={() => {
          onToggle();
          trackEvent("category_toggle", { category: category.name, expanded: !isExpanded });
        }}
        className="w-full flex items-center gap-3 px-4 py-3 text-left cursor-pointer hover:bg-border-light/50 transition-colors"
      >
        <span className="flex-1 text-sm font-medium text-text-primary truncate">
          {category.name}
        </span>
        {hasIssues && (
          <span className="w-1.5 h-1.5 rounded-full bg-status-warning flex-shrink-0" />
        )}
        <span className="text-xs text-text-tertiary flex-shrink-0 tabular-nums">
          {itemCount} {itemCount === 1 ? "item" : "items"}
        </span>
        <motion.div
          animate={{ rotate: isExpanded ? 180 : 0 }}
          transition={{ duration: 0.2 }}
          className="text-text-tertiary flex-shrink-0"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path
              d="M4 6L8 10L12 6"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </motion.div>
      </button>

      <AnimatePresence initial={false}>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-3 flex flex-col border-t border-border-light">
              <AnimatePresence initial={false}>
                {category.items.filter((i) => i.quantity > 0).map((item) => (
                  <motion.div
                    key={item.id}
                    layout
                    initial={{ opacity: 1, height: "auto" }}
                    exit={{ opacity: 0, height: 0, marginTop: 0, marginBottom: 0, overflow: "hidden" }}
                    transition={{ duration: 0.25, ease: "easeInOut" }}
                  >
                    <SkuCard
                      item={item}
                      onUpdateQuantity={onUpdateQuantity}
                      onRemoveItem={onRemoveItem}
                      onImageClick={onImageClick}
                      showCherryPick={showCherryPick}
                      onCherryPick={onCherryPick}
                      isInCart={cartItemNames?.has(item.name) || false}
                    />
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
