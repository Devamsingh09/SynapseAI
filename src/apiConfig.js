/**
 * Local dev: talks directly to the backend via REACT_APP_API_URL (see
 * project-root .env) — must be http://localhost:8000, not 127.0.0.1, so
 * the auth cookie is same-site with the frontend's localhost:3000 origin.
 *
 * Production default is "" (same-origin, relative URLs) rather than any
 * hardcoded host — this is what makes the Vercel rewrites/proxy setup work
 * (see vercel.json): the browser only ever calls its own origin, and
 * Vercel's edge forwards /auth, /thread, /threads, /chat, /voice, /upload,
 * /health to the real backend host transparently, keeping the session
 * cookie same-site. Only set REACT_APP_API_URL in production if you
 * deliberately want the browser to call a different origin directly
 * (e.g. a subdomain setup) instead of going through rewrites.
 */
export const API_BASE = process.env.REACT_APP_API_URL || "";
