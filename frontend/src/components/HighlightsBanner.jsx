export default function HighlightsBanner({ highlights }) {
  if (!highlights || highlights.length === 0) return null;

  const iconByType = {
    warning: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="flex-shrink-0 mt-px">
        <path
          d="M8 5.5V8.5M8 10.5H8.005M14 8C14 11.3137 11.3137 14 8 14C4.68629 14 2 11.3137 2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8Z"
          stroke="currentColor"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
    error: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="flex-shrink-0 mt-px">
        <path
          d="M8 5.5V8.5M8 10.5H8.005M14 8C14 11.3137 11.3137 14 8 14C4.68629 14 2 11.3137 2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8Z"
          stroke="currentColor"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
    info: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="flex-shrink-0 mt-px">
        <path
          d="M8 7V10.5M8 5.5H8.005M14 8C14 11.3137 11.3137 14 8 14C4.68629 14 2 11.3137 2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8Z"
          stroke="currentColor"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  };

  const colorByType = {
    warning: "text-status-warning",
    error: "text-status-error",
    info: "text-text-secondary",
  };

  return (
    <div className="bg-surface-card rounded-xl border border-border px-4 py-3">
      <div className="flex flex-col gap-1.5">
        {highlights.map((h, i) => (
          <div key={i} className={`flex items-start gap-2 text-xs ${colorByType[h.type] || "text-text-secondary"}`}>
            {iconByType[h.type]}
            <span className="leading-snug">{h.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
