import { useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { trackEvent } from "../analytics";
import VendorLogo from "./VendorLogo";

/**
 * AlternativesBottomSheet -- "Find a better option" UI.
 *
 * Two scopes the user can toggle between:
 *   - Same store: alternatives at the same vendor (different SKU /
 *     brand / size at the same place).
 *   - Other stores: alternatives at a DIFFERENT vendor. Used for cart
 *     consolidation -- "I'd rather order all 4 things from one
 *     store, find this in Kibsons instead".
 *
 * Every selection pins that catalog_id as a preference so the agent
 * remembers next time.
 *
 * Props:
 *   data             {listItemId, scope, alternatives, loading} or null
 *   currentItem      the item the sheet was opened from (for header)
 *   onFindScope      (scope: "same_vendor"|"other_vendors") => void
 *   onAccept         (catalogId, scope) => void
 *   onClose          () => void
 */
export default function AlternativesBottomSheet({
  data,
  currentItem,
  onFindScope,
  onAccept,
  onClose,
}) {
  // Local tab state mirrors data.scope so the toggle reflects what
  // came back from the WS.
  const [scope, setScope] = useState(data?.scope || "same_vendor");
  useEffect(() => {
    if (data?.scope) setScope(data.scope);
  }, [data?.scope]);

  if (!data || !currentItem) return null;

  const alternatives = data.alternatives;
  const loading = data.loading;
  // Name the 2nd tab by the actual other store(s) rather than "Other stores".
  // Prefer the real vendor names from loaded results; fall back to the known
  // other store for the label before results arrive.
  const OTHER_STORE = { Kibsons: "Carrefour", "Carrefour UAE": "Kibsons", Carrefour: "Kibsons" };
  const otherFromResults =
    scope === "other_vendors" && Array.isArray(alternatives)
      ? [...new Set(alternatives.map((a) => a.vendor).filter((v) => v && v !== currentItem.vendor))]
      : [];
  const otherStoreLabel = otherFromResults.length
    ? otherFromResults.join(", ")
    : OTHER_STORE[currentItem.vendor] || "Other store";
  const switchScope = (nextScope) => {
    if (nextScope === scope) return;
    setScope(nextScope);
    onFindScope(nextScope);
    trackEvent("alternatives_scope_switch", { scope: nextScope });
  };

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        onClick={onClose}
        className="fixed inset-0 z-40 bg-surface-overlay"
      />

      <motion.div
        initial={{ y: "100%" }}
        animate={{ y: "0%" }}
        exit={{ y: "100%" }}
        transition={{ type: "spring", damping: 30, stiffness: 300 }}
        drag="y"
        dragConstraints={{ top: 0, bottom: 0 }}
        dragElastic={0.2}
        onDragEnd={(_, info) => {
          if (info.offset.y > 100 || info.velocity.y > 500) onClose();
        }}
        className="fixed bottom-0 left-0 right-0 z-50 bg-surface-card rounded-t-2xl max-h-[78vh] overflow-y-auto"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {/* Drag handle */}
        <div className="flex justify-center pt-3 pb-2">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        <div className="px-5 pb-6">
          {/* Header */}
          <div className="mb-3">
            <h2 className="text-base font-semibold text-text-primary">Alternatives</h2>
            <p className="text-xs text-text-secondary mt-0.5 truncate">
              Currently: <span className="text-text-primary font-medium">{currentItem.name}</span>
            </p>
          </div>

          {/* Scope toggle */}
          <div className="flex items-center gap-1 mb-4 bg-surface-elevated rounded-xl p-1">
            <button
              type="button"
              onClick={() => switchScope("same_vendor")}
              className={`flex-1 py-2 px-3 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
                scope === "same_vendor"
                  ? "bg-accent/10 text-accent shadow-sm"
                  : "text-text-secondary hover:text-text-primary"
              }`}
            >
              At {currentItem.vendor || "this store"}
            </button>
            <button
              type="button"
              onClick={() => switchScope("other_vendors")}
              className={`flex-1 py-2 px-3 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
                scope === "other_vendors"
                  ? "bg-accent/10 text-accent shadow-sm"
                  : "text-text-secondary hover:text-text-primary"
              }`}
            >
              {otherStoreLabel}
            </button>
          </div>

          {/* Helper text explaining the cross-vendor case -- this is the
              cart-consolidation flow the user specifically asked for. */}
          {scope === "other_vendors" && (
            <p className="text-[11px] text-text-tertiary mb-3 leading-relaxed">
              Lets you move this item to a different store so you can
              consolidate the cart (e.g. order everything from Carrefour).
            </p>
          )}

          {/* Body -- loading, empty, or list */}
          {loading ? (
            <div className="py-12 flex flex-col items-center gap-3">
              <svg className="animate-spin h-6 w-6 text-accent" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                <path className="opacity-75" fill="currentColor"
                      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <p className="text-xs text-text-tertiary">Searching the catalog...</p>
            </div>
          ) : !alternatives || alternatives.length === 0 ? (
            <div className="py-10 flex flex-col items-center gap-2">
              <p className="text-sm text-text-secondary">
                {scope === "same_vendor"
                  ? "No other options at this store."
                  : "Not stocked at any other store I know."}
              </p>
              <p className="text-[11px] text-text-tertiary text-center max-w-[260px]">
                {scope === "same_vendor"
                  ? "Try 'Other stores' to look across vendors."
                  : "Try 'Wrong item' below to tell me what you'd prefer."}
              </p>
            </div>
          ) : (
            <ul className="flex flex-col gap-2.5">
              {alternatives.map((alt) => (
                <AlternativeCard
                  key={alt.catalogId}
                  alt={alt}
                  onPick={() => {
                    onAccept(alt.catalogId, scope);
                    trackEvent("alternative_accepted", {
                      catalogId: alt.catalogId,
                      vendor: alt.vendor,
                      scope,
                    });
                  }}
                />
              ))}
            </ul>
          )}

          {/* Footer note -- makes the learning loop visible. */}
          {!loading && alternatives && alternatives.length > 0 && (
            <p className="text-[11px] text-text-tertiary text-center mt-4 leading-relaxed">
              Your pick gets remembered. Next time I'll prefer it for this item.
            </p>
          )}
        </div>
      </motion.div>
    </AnimatePresence>
  );
}

function AlternativeCard({ alt, onPick }) {
  return (
    <li>
      <button
        type="button"
        onClick={onPick}
        className="w-full text-left flex items-start gap-3 rounded-xl border border-border bg-surface-card hover:border-accent/40 hover:bg-accent/5 active:scale-[0.99] transition-all px-3 py-3 cursor-pointer"
      >
        {/* Image / placeholder */}
        <div className="w-14 h-14 rounded-lg overflow-hidden bg-white dark:bg-surface-elevated flex items-center justify-center flex-shrink-0">
          {alt.image ? (
            <img
              src={alt.image}
              alt={alt.name}
              className="w-full h-full object-contain"
              onError={(e) => { e.currentTarget.style.display = "none"; }}
            />
          ) : (
            <span className="text-lg font-bold text-text-tertiary">
              {(alt.name?.[0] || "?").toUpperCase()}
            </span>
          )}
        </div>

        <div className="flex-1 min-w-0">
          {/* Row 1: name + price */}
          <div className="flex items-baseline justify-between gap-2">
            <p className="text-sm font-medium text-text-primary leading-snug line-clamp-2">
              {alt.name}
            </p>
            <span className="text-sm font-semibold text-text-primary tabular-nums flex-shrink-0">
              <span className="text-[10px] text-text-tertiary mr-0.5">AED</span>
              {Number(alt.price).toFixed(2)}
            </span>
          </div>

          {/* Row 2: brand · size · origin */}
          {(alt.brand || alt.size || alt.originCountry) && (
            <p className="text-[11px] text-text-tertiary leading-tight mt-0.5 truncate">
              {[alt.brand, alt.size, alt.originCountry].filter(Boolean).join(" · ")}
            </p>
          )}

          {/* Row 3: vendor pill + organic chip */}
          <div className="mt-1.5 flex items-center gap-2 flex-wrap">
            <span className="inline-flex items-center gap-1 text-[10px] text-text-secondary">
              <VendorLogo name={alt.vendor} size={12} />
              {alt.vendor}
            </span>
            {alt.isOrganic && (
              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-200">
                Organic
              </span>
            )}
          </div>
        </div>
      </button>
    </li>
  );
}
