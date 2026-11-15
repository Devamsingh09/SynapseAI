import React, { useEffect, useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { BrandMark, Wordmark } from "./Brand";
import { EyeIcon, EyeOffIcon, ArrowRightIcon } from "./Icons";

// ═══════════════════════════════════════════════════════
// SHARED LAYOUT — spec §04: brand/story column + sign-in card.
// Every auth page (login, signup, verify, forgot, reset) uses the same
// shell so the whole entry flow reads as one product.
// ═══════════════════════════════════════════════════════
function AuthShell({ title, subtitle, children }) {
  return (
    <div className="auth-screen">
      <div className="auth-grid">
        <section className="auth-story" aria-label="About SynapseAI">
          <div className="auth-brand">
            <BrandMark size={46} />
            <div>
              <Wordmark className="wordmark-lg" />
              <div className="auth-tagline">Think beyond answers.</div>
            </div>
          </div>

          <div className="auth-story-body">
            <div className="eyebrow">Your intelligent workspace</div>
            <h2 className="auth-headline">
              Ideas connect.<br />
              <span>Intelligence happens.</span>
            </h2>
            <p className="auth-lede">Research, reasoning and AI-powered tools in one focused workspace.</p>
            <div className="auth-features">
              <div className="feature-tile">
                <div className="feature-kicker">Agentic AI</div>
                <div className="feature-text">Reason and execute</div>
              </div>
              <div className="feature-tile">
                <div className="feature-kicker cyan">Smart tools</div>
                <div className="feature-text">One connected space</div>
              </div>
            </div>
          </div>
        </section>

        <main className="auth-card">
          <h1 className="auth-title">{title}</h1>
          {subtitle && <p className="auth-subtitle">{subtitle}</p>}
          {children}
        </main>
      </div>
    </div>
  );
}

function FieldError({ children }) {
  if (!children) return null;
  return <div className="inline-alert error" role="alert">{children}</div>;
}

function FieldNotice({ children }) {
  if (!children) return null;
  return <div className="inline-alert info" role="status">{children}</div>;
}

function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

function SubmitButton({ busy, busyLabel, children }) {
  return (
    <button className="btn-primary btn-block" type="submit" disabled={busy} aria-busy={busy}>
      {busy ? <><Spinner /> {busyLabel}</> : children}
    </button>
  );
}

function TextField({ id, label, labelAside, ...inputProps }) {
  return (
    <div className="field">
      <div className="field-row">
        <label className="field-label" htmlFor={id}>{label}</label>
        {labelAside}
      </div>
      <input id={id} className="field-input" {...inputProps} />
    </div>
  );
}

// Optional reveal toggle (spec §04): a real <button>, keyboard reachable,
// with an accessible name and pressed state.
function PasswordField({ id, label, labelAside, ...inputProps }) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="field">
      <div className="field-row">
        <label className="field-label" htmlFor={id}>{label}</label>
        {labelAside}
      </div>
      <div className="password-wrap">
        <input id={id} className="field-input" type={visible ? "text" : "password"} {...inputProps} />
        <button
          type="button"
          className="password-toggle"
          onClick={() => setVisible(v => !v)}
          aria-label={visible ? "Hide password" : "Show password"}
          aria-pressed={visible}
          aria-controls={id}
        >
          {visible ? <EyeOffIcon size={17} /> : <EyeIcon size={17} />}
        </button>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// VERIFY WITH A CODE — after signup, or when signing in to an
// unverified account. The verification email has a 6-digit code (also in
// its subject, so it shows in the phone's notification) and a link; the
// code verifies and signs in without leaving the app.
// ═══════════════════════════════════════════════════════
const OTP_LENGTH = 6;
const RESEND_COOLDOWN_SEC = 30;   // matches the server's cooldown

function VerifyCodePanel({ email, emailSent = true, sentJustNow = false, onChangeEmail }) {
  const { verifyOtp, resendVerification } = useAuth();
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [resending, setResending] = useState(false);
  const [cooldown, setCooldown] = useState(sentJustNow ? RESEND_COOLDOWN_SEC : 0);

  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  const submit = async (value) => {
    if (submitting) return;
    if (value.length !== OTP_LENGTH) {
      setError("Enter the 6-digit code from the email.");
      return;
    }
    setError(null);
    setNotice(null);
    setSubmitting(true);
    try {
      await verifyOtp(email, value);
      navigate("/", { replace: true });
    } catch (err) {
      setError(!err.status || err.status >= 500
        ? "We couldn't reach the server. Check your connection and try again."
        : err.message);
      setSubmitting(false);
    }
  };

  // Digits only; a full code (typed, pasted or autofilled) is sent right away.
  const onChange = (e) => {
    const digits = e.target.value.replace(/\D/g, "").slice(0, OTP_LENGTH);
    setCode(digits);
    if (error) setError(null);
    if (digits.length === OTP_LENGTH && code.length !== OTP_LENGTH) submit(digits);
  };

  const resend = async () => {
    if (resending || cooldown > 0) return;
    setResending(true);
    setError(null);
    try {
      await resendVerification(email);
      setNotice("A new code is on its way. Use the newest email — older codes stop working.");
      setCode("");
      setCooldown(RESEND_COOLDOWN_SEC);
    } catch (err) {
      setError(!err.status || err.status >= 500
        ? "We couldn't reach the server. Check your connection and try again."
        : err.message);
    } finally {
      setResending(false);
    }
  };

  return (
    <>
      {!emailSent && (
        <FieldError>
          We couldn't send the email just now. Wait a moment, then tap “Send a new code”.
        </FieldError>
      )}
      <form className="auth-form" onSubmit={(e) => { e.preventDefault(); submit(code); }}>
        <div className="field">
          <label className="field-label" htmlFor="otp-code">Verification code</label>
          <input
            id="otp-code"
            className="field-input otp-input"
            value={code}
            onChange={onChange}
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="one-time-code"
            // no maxLength: a pasted "123 456" would be cut to "123 45" before
            // onChange strips the space — onChange keeps the first 6 digits instead
            placeholder="••••••"
            aria-describedby="otp-hint"
            autoFocus
            required
          />
        </div>
        <div className="field-hint" id="otp-hint">
          The code works for 15 minutes. You can also tap the link in the email.
        </div>
        <FieldError>{error}</FieldError>
        {notice && <FieldNotice>{notice}</FieldNotice>}
        <SubmitButton busy={submitting} busyLabel="Verifying…">
          Verify and continue <ArrowRightIcon size={16} />
        </SubmitButton>
      </form>
      <div className="otp-resend" style={{ marginTop: 16 }}>
        Didn't get it?
        <button type="button" className="text-btn" onClick={resend} disabled={resending || cooldown > 0}>
          {resending ? "Sending…" : cooldown > 0 ? `Send a new code in ${cooldown}s` : "Send a new code"}
        </button>
      </div>
      <div className="auth-links">
        {onChangeEmail
          ? <button type="button" className="text-btn" onClick={onChangeEmail}>Back to sign in</button>
          : <Link to="/login">Back to sign in</Link>}
      </div>
    </>
  );
}

// ═══════════════════════════════════════════════════════
// LOGIN
// ═══════════════════════════════════════════════════════
function loginErrorMessage(err) {
  // 401 = wrong email or password. Generic on purpose: never reveal whether
  // an account exists. 0 / 5xx = server unreachable or still waking up.
  // Anything else (e.g. 403 "Please verify your email…") is shown as sent.
  if (err.status === 401) {
    return "We couldn't sign you in with those details. Check your email and password and try again.";
  }
  if (!err.status || err.status >= 500) {
    return "We couldn't reach the server. Check your connection and try again.";
  }
  return err.message;
}

export function LoginPage() {
  const { login, isAuthenticated, loading } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [needsCode, setNeedsCode] = useState(false); // right password, email not verified yet

  if (!loading && isAuthenticated) return <Navigate to="/" replace />;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      navigate("/");
    } catch (err) {
      if (err.status === 403) setNeedsCode(true);   // 403 only after a correct password
      else setError(loginErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  if (needsCode) {
    return (
      <AuthShell
        title="Verify your email"
        subtitle={`Enter the 6-digit code we emailed to ${email.trim()}. If it has expired, send a new one.`}
      >
        <VerifyCodePanel email={email.trim()} onChangeEmail={() => setNeedsCode(false)} />
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Welcome back" subtitle="Sign in to continue to your workspace.">
      <form className="auth-form" onSubmit={handleSubmit}>
        <TextField
          id="login-email"
          label="Email address"
          type="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />
        <PasswordField
          id="login-password"
          label="Password"
          labelAside={<Link className="field-link" to="/forgot-password">Forgot password?</Link>}
          placeholder="Enter your password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
        <FieldError>{error}</FieldError>
        <SubmitButton busy={submitting} busyLabel="Signing in…">
          Sign in to SynapseAI <ArrowRightIcon size={16} />
        </SubmitButton>
      </form>
      <div className="auth-links">
        New to SynapseAI? <Link to="/signup">Create an account</Link>
      </div>
      <div className="auth-foot">Secure workspace · Built for intelligent work</div>
    </AuthShell>
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
    if (submitting) return;
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
      <AuthShell title="Check your email" subtitle={`We sent a 6-digit code to ${email.trim()}. Enter it below to finish signing up.`}>
        <VerifyCodePanel email={email.trim()} emailSent={done.email_sent} sentJustNow />
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Create your account" subtitle="Free forever — just an email and password.">
      <form className="auth-form" onSubmit={handleSubmit}>
        <TextField
          id="signup-email"
          label="Email address"
          type="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />
        <PasswordField
          id="signup-password"
          label="Password"
          placeholder="At least 8 characters"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
          aria-describedby="signup-password-hint"
        />
        <div className="field-hint" id="signup-password-hint">At least 8 characters.</div>
        <FieldError>{error}</FieldError>
        <SubmitButton busy={submitting} busyLabel="Creating account…">Create account</SubmitButton>
      </form>
      <div className="auth-links">
        Already have an account? <Link to="/login">Sign in</Link>
      </div>
    </AuthShell>
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

  const title = status === "ok" ? "Email verified" : status === "error" ? "Verification failed" : "Verifying…";

  return (
    <AuthShell title={title}>
      {status === "verifying" && <FieldNotice>One moment…</FieldNotice>}
      {status === "ok" && <FieldNotice>{message}</FieldNotice>}
      {status === "error" && <FieldError>{message}</FieldError>}
      <div className="auth-links">
        <Link to="/login">Go to sign in</Link>
      </div>
    </AuthShell>
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
    if (submitting) return;
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
    // Only shown after the server accepted the request.
    return (
      <AuthShell title="Check your email" subtitle="If that email has an account, a reset link is on its way.">
        <div className="auth-links">
          <Link to="/login">Back to sign in</Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Reset your password" subtitle="Enter your account email and we'll send a reset link.">
      <form className="auth-form" onSubmit={handleSubmit}>
        <TextField
          id="forgot-email"
          label="Email address"
          type="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />
        <FieldError>{error}</FieldError>
        <SubmitButton busy={submitting} busyLabel="Sending…">Send reset link</SubmitButton>
      </form>
      <div className="auth-links">
        <Link to="/login">Back to sign in</Link>
      </div>
    </AuthShell>
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
    if (submitting) return;
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
      <AuthShell title="Password updated" subtitle="Redirecting you to sign in…">
        <div className="auth-links">
          <Link to="/login">Go to sign in now</Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Set a new password">
      <form className="auth-form" onSubmit={handleSubmit}>
        <PasswordField
          id="reset-password"
          label="New password"
          placeholder="At least 8 characters"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
        />
        <PasswordField
          id="reset-confirm"
          label="Confirm new password"
          placeholder="Repeat the new password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
        />
        <FieldError>{error}</FieldError>
        <SubmitButton busy={submitting} busyLabel="Updating…">Update password</SubmitButton>
      </form>
      <div className="auth-links">
        <Link to="/login">Back to sign in</Link>
      </div>
    </AuthShell>
  );
}
