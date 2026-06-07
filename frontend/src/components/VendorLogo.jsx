import { useState } from "react";

/**
 * Vendor logo.
 *
 * Uses the store's real favicon (PNG in /public/logos/) when one is
 * available -- people recognise the actual brand mark faster than
 * any treatment we'd invent. The brand-color letter tile is kept
 * only as a fallback for vendors we don't yet have a logo for.
 *
 * `size` controls both width and height in pixels.
 */

const VENDOR_LOGOS = {
  "Carrefour UAE": "/logos/carrefour.png",
  "Carrefour":     "/logos/carrefour.png",
  "Kibsons":       "/logos/kibsons.png",
  "Amazon":        "/logos/amazon.png",
};

// Brand-color tiles used only when we don't have a PNG for the vendor.
// Slate fallback reads on both light and dark backgrounds.
const FALLBACK_TILE = { bg: "#475569", fg: "#FFFFFF" };

export default function VendorLogo({ name, size = 20 }) {
  const src = VENDOR_LOGOS[name];
  const [imgError, setImgError] = useState(false);

  // Real logo first
  if (src && !imgError) {
    return (
      <img
        src={src}
        alt={name}
        width={size}
        height={size}
        className="rounded-sm object-contain"
        onError={() => setImgError(true)}
      />
    );
  }

  // Letter-tile fallback for unknown/missing vendors
  const letter = (name?.[0] || "?").toUpperCase();
  const fontSize = Math.max(10, Math.floor(size * 0.55));
  return (
    <div
      role="img"
      aria-label={name}
      className="rounded-md flex items-center justify-center flex-shrink-0 font-bold leading-none"
      style={{
        background: FALLBACK_TILE.bg,
        color: FALLBACK_TILE.fg,
        width: size,
        height: size,
        fontSize,
      }}
    >
      {letter}
    </div>
  );
}
