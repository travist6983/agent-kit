import { useState } from "react";

// The status pill on an Invoice row: Draft, Due, Paid, Void.
export function StatusBadge(props) {
  const [status] = useState(props.status);
  return (
    <span className={`badge badge-${status}`} aria-label={`Invoice status: ${status}`}>
      {status}
    </span>
  );
}
