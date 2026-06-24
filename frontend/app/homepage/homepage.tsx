"use client";

import "./homepage.css";
import Waves from "../../src/component/Waves";

interface HomepageProps {
  onSignIn?: () => void;
  onAdmin?: () => void;
}

/**
 * Minimal landing page for the POC: a title and a single "Sign In" button.
 * There is no real authentication/enforcement here yet - clicking Sign In just
 * opens the Document Assistant.
 */
export default function Homepage({ onSignIn }: HomepageProps) {
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
      </nav>

      {/* Hero Section */}
      <section className="hero">
        <h1 className="hero-title">MIAX RAG<br />Document Assistant</h1>
        <button className="hero-btn" onClick={onSignIn}>
          Sign In <span className="btn-arrow">&rarr;</span>
        </button>
      </section>
    </div>
  );
}
