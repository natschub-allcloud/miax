"use client";

import { useState } from "react";
import "./homepage.css";
import Waves from "../../src/component/Waves";
import AccountModal from "../account_signup_creating/account";

interface HomepageProps {
  onSignIn?: () => void;
  onAdmin?: () => void;
}

export default function Homepage({ onSignIn, onAdmin }: HomepageProps) {
  const [showAuth, setShowAuth] = useState(false);

  return (
    <div className="homepage">
      {/* Background wave effect */}
      <div className="bg-wave-container">
        <Waves
          lineColor="rgba(200, 130, 50, 0.3)"
          backgroundColor="transparent"
          waveSpeedX={0.02}
          waveSpeedY={0.01}
          waveAmpX={40}
          waveAmpY={20}
          friction={0.9}
          tension={0.01}
          maxCursorMove={120}
          xGap={12}
          yGap={36}
        />
      </div>

      {/* Navigation */}
      <nav className="navbar">
        <div className="nav-logo">MIAX</div>
        <ul className="nav-links">
          <li><a href="#" className="nav-link">Global Markets</a></li>
          <li><a href="#" className="nav-link">Asset Classes</a></li>
          <li><a href="#" className="nav-link">Regulatory</a></li>
          <li><a href="#" className="nav-link">About</a></li>
        </ul>
        <button className="nav-cta" onClick={onAdmin}>Client Access</button>
      </nav>

      {/* Hero Section */}
      <section className="hero">
        <h1 className="hero-title">
          Institutional<br />Trading Solution
        </h1>
        <p className="hero-description">
          A leading financial exchange holding company providing
          transparent global market data and execution
        </p>
        <button className="hero-btn" onClick={() => setShowAuth(true)}>
          Sign Up / Sign In <span className="btn-arrow">&rarr;</span>
        </button>
      </section>

      {/* Preview Section */}
      <section className="preview">
        <div className="preview-wrapper">
          {/* Back card (left/behind) */}
          <div className="preview-card preview-card-back">
            <div className="preview-card-header">
              <span className="preview-logo">MIAX</span>
              <div className="preview-nav">
                <span>Global Markets</span>
                <span>Asset Classes</span>
                <span>Regulatory</span>
                <span>About</span>
              </div>
              <span className="preview-client">Client Access</span>
            </div>
            <div className="preview-card-body">
              <div className="preview-screen"></div>
            </div>
          </div>

          {/* Front card (right/in front) */}
          <div className="preview-card preview-card-front">
            <div className="preview-card-header">
              <span className="preview-logo">MIAX</span>
              <div className="preview-nav">
                <span>Global Markets</span>
                <span>Asset Classes</span>
                <span>Regulatory</span>
                <span>About</span>
              </div>
              {/* <span className="preview-client">Client Access</span> */}
            </div>
            <div className="preview-card-body">
              <h3>Advanced multi-asset class<br />trading technology.</h3>
            </div>
          </div>
        </div>
      </section>

      {/* Auth Modal */}
      <AccountModal isOpen={showAuth} onClose={() => setShowAuth(false)} onSignIn={onSignIn} />
    </div>
  );
}
