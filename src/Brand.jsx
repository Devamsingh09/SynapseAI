import React from "react";

// SynapseAI brand primitives (spec §02): a rounded-square "S" monogram with
// the indigo→blue gradient, and the "Synapse" + cyan "AI" wordmark.
// The mark is decorative wherever the wordmark sits next to it, so screen
// readers hear "SynapseAI" once, not "S SynapseAI".
export function BrandMark({ size = 40, label }) {
  return (
    <span
      className="brand-mark"
      style={{ "--mark-size": `${size}px` }}
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
    >
      S
    </span>
  );
}

export function Wordmark({ className = "" }) {
  return (
    <span className={`wordmark ${className}`}>
      Synapse<span className="wordmark-ai">AI</span>
    </span>
  );
}
