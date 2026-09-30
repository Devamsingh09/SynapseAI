import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import { API_BASE } from "./apiConfig";

const AuthContext = createContext(null);

async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    // Required: the session lives in an httpOnly cookie, not a token we
    // hold in JS — every request must explicitly opt in to sending it.
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty body is fine for some endpoints */
  }
  if (!res.ok) {
    throw new Error((data && data.detail) || `Request failed (${res.status})`);
  }
  return data;
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const refreshUser = useCallback(async () => {
    try {
      const me = await apiFetch("/auth/me");
      setUser(me);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshUser();
  }, [refreshUser]);

  const login = useCallback(async (email, password) => {
    const data = await apiFetch("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setUser(data);
    return data;
  }, []);

  const signup = useCallback(
    (email, password) =>
      apiFetch("/auth/signup", { method: "POST", body: JSON.stringify({ email, password }) }),
    []
  );

  const logout = useCallback(async () => {
    try {
      await apiFetch("/auth/logout", { method: "POST" });
    } finally {
      setUser(null);
    }
  }, []);

  const forgotPassword = useCallback(
    (email) => apiFetch("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email }) }),
    []
  );

  const resetPassword = useCallback(
    (token, new_password) =>
      apiFetch("/auth/reset-password", { method: "POST", body: JSON.stringify({ token, new_password }) }),
    []
  );

  const verifyEmail = useCallback(
    (token) => apiFetch(`/auth/verify-email?token=${encodeURIComponent(token)}`),
    []
  );

  const resendVerification = useCallback(
    (email) => apiFetch("/auth/resend-verification", { method: "POST", body: JSON.stringify({ email }) }),
    []
  );

  const value = {
    user,
    loading,
    isAuthenticated: !!user,
    login,
    signup,
    logout,
    forgotPassword,
    resetPassword,
    verifyEmail,
    resendVerification,
    refreshUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
