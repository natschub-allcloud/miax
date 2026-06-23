"use client";

import { useState } from "react";
import "./account.css";

interface AccountModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSignIn?: () => void;
}

export default function AccountModal({ isOpen, onClose, onSignIn }: AccountModalProps) {
  const [activeTab, setActiveTab] = useState<"signin" | "signup">("signin");

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container" onClick={(e) => e.stopPropagation()}>
        {/* Close button */}
        <button className="modal-close" onClick={onClose}>&times;</button>

        {/* Tabs */}
        <div className="auth-tabs">
          <button
            className={`auth-tab ${activeTab === "signup" ? "active" : ""}`}
            onClick={() => setActiveTab("signup")}
          >
            SIGN UP
          </button>
          <button
            className={`auth-tab ${activeTab === "signin" ? "active" : ""}`}
            onClick={() => setActiveTab("signin")}
          >
            SIGN IN
          </button>
        </div>
        <div className="tab-indicator">
          <div className={`indicator-bar ${activeTab}`}></div>
        </div>

        {/* Sign In Form */}
        {activeTab === "signin" && (
          <form className="auth-form" onSubmit={(e) => { e.preventDefault(); onSignIn?.(); }}>
            <input
              type="text"
              placeholder="Email or Username"
              className="auth-input"
            />
            <input
              type="password"
              placeholder="Password"
              className="auth-input"
            />
            <button type="submit" className="auth-submit">Sign In</button>
            <a href="#" className="forgot-link">Forgot Password?</a>
          </form>
        )}

        {/* Sign Up Form */}
        {activeTab === "signup" && (
          <form className="auth-form" onSubmit={(e) => e.preventDefault()}>
            <input
              type="text"
              placeholder="Full Name"
              className="auth-input"
            />
            <input
              type="email"
              placeholder="Email"
              className="auth-input"
            />
            <input
              type="password"
              placeholder="Password"
              className="auth-input"
            />
            <input
              type="password"
              placeholder="Confirm Password"
              className="auth-input"
            />
            <button type="submit" className="auth-submit">Sign Up</button>
          </form>
        )}

        {/* Divider */}
        <div className="auth-divider">
          <span>or, Continue with:</span>
        </div>

        {/* Social buttons */}
        <div className="social-buttons">
          <button className="social-btn google-btn">
            <span className="social-icon">G</span> Google
          </button>
          <button className="social-btn apple-btn">
            <span className="social-icon">&#63743;</span> Apple
          </button>
        </div>

        {/* Footer */}
        <div className="auth-footer">
          {activeTab === "signin" ? (
            <p>Need an Account? <a href="#" onClick={() => setActiveTab("signup")}>Sign Up Now</a></p>
          ) : (
            <p>Already have an account? <a href="#" onClick={() => setActiveTab("signin")}>Sign In</a></p>
          )}
        </div>
      </div>
    </div>
  );
}
