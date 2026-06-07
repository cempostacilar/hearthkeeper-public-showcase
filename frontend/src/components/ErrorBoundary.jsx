import React from "react";

/**
 * Catches render-time crashes anywhere in the tree and shows a recoverable
 * message instead of a blank white screen. Uses inline styles so the
 * fallback renders even if the stylesheet itself is the problem.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error, info) {
    console.error("Hearthkeeper: uncaught render error", error, info);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "0.75rem",
          padding: "2rem",
          textAlign: "center",
          fontFamily: "system-ui, -apple-system, sans-serif",
          background: "#f4f1ea",
          color: "#2a2a2a",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1.25rem" }}>Something went wrong</h2>
        <p style={{ margin: 0, color: "#777", fontSize: "0.9rem" }}>
          The app hit an unexpected error. Reloading usually fixes it.
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            marginTop: "0.5rem",
            padding: "0.7rem 1.5rem",
            fontSize: "1rem",
            border: 0,
            borderRadius: "10px",
            background: "#c2693f",
            color: "#fff",
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          Reload
        </button>
      </div>
    );
  }
}
