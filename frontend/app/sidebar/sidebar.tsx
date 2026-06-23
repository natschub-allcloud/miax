"use client";

import "./sidebar.css";

export type SidebarView = "overview" | "documents" | "assistant" | "permissions";

interface SidebarProps {
  activeView: SidebarView;
  onNavigate: (view: SidebarView) => void;
  onBack?: () => void;
}

const NAV_ITEMS: { id: SidebarView; label: string }[] = [
  { id: "assistant", label: "Assistant" },
];

export default function Sidebar({ activeView, onNavigate, onBack }: SidebarProps) {
  return (
    <aside className="sidebar">
      {/* Logo */}
      <div className="sidebar-logo">
        <div className="sidebar-logo-icon">M</div>
        <div className="sidebar-logo-text">
          <span className="sidebar-brand">Miax</span>
          <span className="sidebar-tagline">Document AI</span>
        </div>
      </div>

      {/* Navigation */}
      <nav className="sidebar-nav">
        <p className="sidebar-nav-label">WORKSPACE</p>
        {NAV_ITEMS.map((item) => (
          <button
            key={item.id}
            className={`sidebar-nav-item ${activeView === item.id ? "active" : ""}`}
            onClick={() => onNavigate(item.id)}
          >
            <svg className="sidebar-nav-icon-svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              <path d="M8 10h.01" />
              <path d="M12 10h.01" />
              <path d="M16 10h.01" />
            </svg>
            <span className="sidebar-nav-text">{item.label}</span>
            {activeView === item.id && <span className="sidebar-nav-dot" />}
          </button>
        ))}
      </nav>

      {/* Bottom section */}
      <div className="sidebar-bottom">
        {onBack && (
          <button className="sidebar-nav-item" onClick={onBack}>
            <span className="sidebar-nav-icon">←</span>
            <span className="sidebar-nav-text">Sign Out</span>
          </button>
        )}
      </div>
    </aside>
  );
}
