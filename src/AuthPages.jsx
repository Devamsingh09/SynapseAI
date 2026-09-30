import React, { useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "./AuthContext";

// ═══════════════════════════════════════════════════════
// SHARED LAYOUT
// ═══════════════════════════════════════════════════════
function AuthCard({ title, subtitle, children }) {
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-logo">
          <div className="logo-orb">🧠</div>
          <div className="logo-name">Synapse AI</div>
        </div>
        <h1 className="auth-title">{title}</h1>
        {subtitle && <p className="auth-subtitle">{subtitle}</p>}
        {children}
      </div>
    </div>
  );
}

function FieldError({ children }) {
  if (!children) return null;
  return <div className="auth-error">{children}</div>;
}

function FieldNotice({ children }) {
  if (!children) return null;
  return <div className="auth-notice">{children}</div>;
}

// ═══════════════════════════════════════════════════════
// LOGIN
// ═══════════════════════════════════════════════════════
export function LoginPage() {
  const { login, isAuthenticated, loading } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  if (!loading && isAuthenticated) return <Navigate to="/" replace />;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      navigate("/");
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthCard title="Welcome back" subtitle="Log in to continue to Synapse AI">
      <form className="auth-form" onSubmit={handleSubmit}>
        <label className="auth-label">
          Email
          <input
            className="auth-input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </label>
        <label className="auth-label">
          Password
          <input
            className="auth-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <FieldError>{error}</FieldError>
        <button className="auth-submit" type="submit" disabled={submitting}>
          {submitting ? "Logging in…" : "Log in"}
        </button>
      </form>
      <div className="auth-links">
        <Link to="/forgot-password">Forgot password?</Link>
        <span className="auth-links-sep">·</span>
        <Link to="/signup">Create an account</Link>
      </div>
    </AuthCard>
  );
}

// ═══════════════════════════════════════════════════════
// SIGNUP
// ═══════════════════════════════════════════════════════
export function SignupPage() {
  const { signup, isAuthenticated, loading } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(null); // { email_sent }

  if (!loading && isAuthenticated) return <Navigate to="/" replace />;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setSubmitting(true);
    try {
      const res = await signup(email, password);
      setDone(res);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    return (
      <AuthCard title="Check your email" subtitle={`We sent a verification link to ${email}.`}>
        <FieldNotice>
          {done.email_sent
            ? "Click the link in that email to activate your account, then come back and log in."
            : "Email sending isn't configured on this server yet — check the backend server log for the verification link (dev mode)."}
        </FieldNotice>
        <div className="auth-links">
          <Link to="/login">Back to login</Link>
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Create your account" subtitle="Free forever — just an email and password">
      <form className="auth-form" onSubmit={handleSubmit}>
        <label className="auth-label">
          Email
          <input
            className="auth-input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </label>
        <label className="auth-label">
          Password
          <input
            className="auth-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </label>
        <div className="auth-hint">At least 8 characters.</div>
        <FieldError>{error}</FieldError>
        <button className="auth-submit" type="submit" disabled={submitting}>
          {submitting ? "Creating account…" : "Sign up"}
        </button>
      </form>
      <div className="auth-links">
        <span>Already have an account?</span> <Link to="/login">Log in</Link>
      </div>
    </AuthCard>
  );
}

// ═══════════════════════════════════════════════════════
// VERIFY EMAIL
// ═══════════════════════════════════════════════════════
export function VerifyEmailPage() {
  const { verifyEmail } = useAuth();
  const [params] = useSearchParams();
  const [status, setStatus] = useState("verifying"); // verifying | ok | error
  const [message, setMessage] = useState("");

  // Ref guard, not just an empty dep array: the verification token is
  // single-use server-side, so if this effect ever ran twice (e.g. under
  // StrictMode's double-invoke in dev) a second call would overwrite a
  // successful "verified" status with a spurious "invalid token" error.
  const ranRef = React.useRef(false);
  React.useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    const token = params.get("token");
    if (!token) {
      setStatus("error");
      setMessage("Missing verification token.");
      return;
    }
    verifyEmail(token)
      .then((res) => {
        setStatus("ok");
        setMessage(res.message || "Email verified.");
      })
      .catch((err) => {
        setStatus("error");
        setMessage(err.message);
      });
  }, [params, verifyEmail]);

  return (
    <AuthCard title={status === "ok" ? "Email verified" : "Verifying…"}>
      {status === "verifying" && <FieldNotice>One moment…</FieldNotice>}
      {status === "ok" && <FieldNotice>{message}</FieldNotice>}
      {status === "error" && <FieldError>{message}</FieldError>}
      <div className="auth-links">
        <Link to="/login">Go to login</Link>
      </div>
    </AuthCard>
  );
}

// ═══════════════════════════════════════════════════════
// FORGOT PASSWORD
// ═══════════════════════════════════════════════════════
export function ForgotPasswordPage() {
  const { forgotPassword } = useAuth();
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await forgotPassword(email);
      setSent(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (sent) {
    return (
      <AuthCard title="Check your email" subtitle="If that email has an account, a reset link is on its way.">
        <div className="auth-links">
          <Link to="/login">Back to login</Link>
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Reset your password" subtitle="Enter your account email and we'll send a reset link">
      <form className="auth-form" onSubmit={handleSubmit}>
        <label className="auth-label">
          Email
          <input
            className="auth-input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </label>
        <FieldError>{error}</FieldError>
        <button className="auth-submit" type="submit" disabled={submitting}>
          {submitting ? "Sending…" : "Send reset link"}
        </button>
      </form>
      <div className="auth-links">
        <Link to="/login">Back to login</Link>
      </div>
    </AuthCard>
  );
}

// ═══════════════════════════════════════════════════════
// RESET PASSWORD
// ═══════════════════════════════════════════════════════
export function ResetPasswordPage() {
  const { resetPassword } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!token) {
      setError("Missing or invalid reset link.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setSubmitting(true);
    try {
      await resetPassword(token, password);
      setDone(true);
      setTimeout(() => navigate("/login"), 1500);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    return (
      <AuthCard title="Password updated" subtitle="Redirecting you to login…">
        <div className="auth-links">
          <Link to="/login">Go to login now</Link>
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Set a new password">
      <form className="auth-form" onSubmit={handleSubmit}>
        <label className="auth-label">
          New password
          <input
            className="auth-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </label>
        <label className="auth-label">
          Confirm new password
          <input
            className="auth-input"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </label>
        <FieldError>{error}</FieldError>
        <button className="auth-submit" type="submit" disabled={submitting}>
          {submitting ? "Updating…" : "Update password"}
        </button>
      </form>
      <div className="auth-links">
        <Link to="/login">Back to login</Link>
      </div>
    </AuthCard>
  );
}
