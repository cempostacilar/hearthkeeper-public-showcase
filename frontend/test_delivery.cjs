function formatTimeWindow(start, end) {
  const fmt = (t) => {
    const [h, m] = t.split(":").map(Number);
    const suffix = h >= 12 ? "PM" : "AM";
    const h12 = h % 12 || 12;
    return m > 0 ? `${h12}:${String(m).padStart(2, "0")} ${suffix}` : `${h12} ${suffix}`;
  };
  return `${fmt(start)} \u2013 ${fmt(end)}`;
}

function buildDeliveryLabel(expected_by, order_date, delivery_window) {
  let targetDate = null;
  let isEstimated = false;
  if (expected_by) {
    targetDate = new Date(expected_by);
  } else if (order_date) {
    const d = new Date(order_date);
    d.setDate(d.getDate() + 1);
    targetDate = d;
    isEstimated = true;
  }
  if (!targetDate || isNaN(targetDate.getTime())) return { label: null, isEstimated: false };
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(targetDate.getFullYear(), targetDate.getMonth(), targetDate.getDate());
  const diffDays = Math.round((target - today) / 86400000);
  let dayLabel;
  if (diffDays === 0) dayLabel = "Today";
  else if (diffDays === 1) dayLabel = "Tomorrow";
  else if (diffDays === -1) dayLabel = "Yesterday";
  else dayLabel = "Other";

  console.log("  targetDate:", targetDate.toISOString());
  console.log("  today:", today.toISOString());
  console.log("  diffDays:", diffDays);
  console.log("  dayLabel:", dayLabel);
  console.log("  delivery_window:", JSON.stringify(delivery_window));

  if (delivery_window && delivery_window.start && delivery_window.end) {
    return { label: `${dayLabel}, ${formatTimeWindow(delivery_window.start, delivery_window.end)}`, isEstimated };
  }
  return { label: dayLabel, isEstimated };
}

var result = buildDeliveryLabel("2026-02-28", "2026-02-28", {start: "07:00", end: "13:00"});
console.log("Result:", JSON.stringify(result));
