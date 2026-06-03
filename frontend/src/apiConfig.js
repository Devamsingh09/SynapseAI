/**
 * Local dev: use CRA proxy (package.json → localhost:8000) via relative URLs.
 * Production: set REACT_APP_API_URL to your Hugging Face / API host.
 */
export const API_BASE =
  process.env.REACT_APP_API_URL ||
  (process.env.NODE_ENV === "development" ? "" : "http://localhost:8000");
