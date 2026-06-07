import { useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import CategoryRow from "./CategoryRow";
import VendorLogo from "./VendorLogo";

export default function VendorSection({
  vendor,
  vendorIndex,
  // Map of categoryKey -> bool. Replaces the previous single-string
  // expandedCategory so multiple categories can be open at once.
  expandedCategories,
  onToggleCategory,
  onUpdateQuantity,
  onRemoveItem,
  onImageClick,
  categoryOffset,
  showCherryPick = false,
  onCherryPick,
  cartItemNames = null,
}) {
  const vendorTotal = useMemo(() => {
    let total = 0;
    for (const cat of vendor.categories) {
      for (const item of cat.items) {
        if (item.quantity > 0) {
          total += item.price * item.quantity;
        }
      }
    }
    return total;
  }, [vendor.categories]);

  return (
    <section className="mb-4">
      <div className="sticky top-0 z-10 bg-surface px-1 py-2 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <VendorLogo name={vendor.name} size={18} />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-text-secondary">
            {vendor.name}
          </h2>
        </div>
        <span className="text-xs font-semibold text-text-primary tabular-nums">
          <span className="text-[10px] font-normal text-text-secondary mr-0.5">AED</span>
          {vendorTotal.toFixed(2)}
        </span>
      </div>
      <div className="flex flex-col gap-1.5">
        <AnimatePresence initial={false}>
          {vendor.categories.map((category, catIdx) => {
            const categoryKey = `${vendor.name}:${category.name}`;
            // Hide categories with no visible items
            const visibleItems = category.items.filter((i) => i.quantity > 0);
            if (visibleItems.length === 0) return null;
            return (
              <motion.div
                key={categoryKey}
                layout
                exit={{ opacity: 0, height: 0, marginBottom: 0, overflow: "hidden" }}
                transition={{ duration: 0.25, ease: "easeInOut" }}
              >
                <CategoryRow
                  category={category}
                  isExpanded={!!expandedCategories?.[categoryKey]}
                  onToggle={() => onToggleCategory(categoryKey)}
                  onUpdateQuantity={onUpdateQuantity}
                  onRemoveItem={onRemoveItem}
                  onImageClick={onImageClick}
                  showCherryPick={showCherryPick}
                  onCherryPick={onCherryPick}
                  cartItemNames={cartItemNames}
                />
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </section>
  );
}
