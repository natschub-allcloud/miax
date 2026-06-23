"use client";

import { useState, useRef, DragEvent } from "react";
import "./admin.css";
import { queryAgent } from "../../src/lib/api";

interface AdminPanelProps {
  onBack?: () => void;
}

const PERMISSION_GROUPS = [
  "Permissions Group A",
  "Permissions Group B",
  "Permissions Group C",
];

interface UploadedFile {
  file: File;
  id: string;
}

interface ChatMessage {
  role: "bot" | "user";
  content: string;
}

export default function AdminPanel({ onBack }: AdminPanelProps) {
  // Permissions state
  const [selectedPermissions, setSelectedPermissions] = useState<string[]>([]);

  // Upload state
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Chat state
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: "bot",
      content:
        "Welcome to MIAX. How can I assist you with market data or multi-asset class technology today?",
    },
  ]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);

  // --- Permissions ---
  function togglePermission(permission: string) {
    setSelectedPermissions((prev) =>
      prev.includes(permission)
        ? prev.filter((p) => p !== permission)
        : [...prev, permission]
    );
  }

  // --- File upload ---
  function handleFiles(files: FileList | null) {
    if (!files) return;
    const newFiles: UploadedFile[] = Array.from(files).map((file) => ({
      file,
      id: `${file.name}-${Date.now()}-${Math.random()}`,
    }));
    setUploadedFiles((prev) => [...prev, ...newFiles]);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    handleFiles(e.dataTransfer.files);
  }

  function handleDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(true);
  }

  function handleDragLeave() {
    setDragOver(false);
  }

  function removeFile(id: string) {
    setUploadedFiles((prev) => prev.filter((f) => f.id !== id));
  }

  function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function handleSubmit() {
    console.log("Permissions:", selectedPermissions);
    console.log("Files:", uploadedFiles.map((f) => f.file.name));
    alert(
      `Submitting ${uploadedFiles.length} file(s) with permissions: ${selectedPermissions.join(", ") || "None selected"}`
    );
  }

  // --- Chat ---
  async function handleChatSend(e: React.FormEvent) {
    e.preventDefault();
    if (!chatInput.trim() || chatLoading) return;

    const userMsg: ChatMessage = { role: "user", content: chatInput };
    setMessages((prev) => [...prev, userMsg]);
    setChatInput("");
    setChatLoading(true);

    try {
      // Use the first selected permission group, or default
      const group = selectedPermissions[0]?.toLowerCase().replace(/\s+/g, "_") || "permissions_group_a";
      const response = await queryAgent({
        username: "admin@miax.com", // TODO: replace with real user from auth
        permission_group: group,
        prompt: chatInput,
      });

      const botMsg: ChatMessage = { role: "bot", content: response.answer };
      setMessages((prev) => [...prev, botMsg]);
    } catch (err) {
      const errorMsg: ChatMessage = {
        role: "bot",
        content: `Sorry, something went wrong: ${err instanceof Error ? err.message : "Unknown error"}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setChatLoading(false);
    }
  }

  return (
    <div className="admin-hub">
      {/* Header */}
      <div className="admin-hub-header">
        <div>
          <h1>Admin &mdash; MIAX Hub</h1>
          <p className="admin-subtitle">Permissions, Uploads &amp; MIAX Chat</p>
        </div>
        {onBack && (
          <button className="admin-back-btn" onClick={onBack}>
            &larr; Back
          </button>
        )}
      </div>

      {/* Two-column layout */}
      <div className="admin-hub-layout">
        {/* LEFT: Permissions + Upload */}
        <div className="admin-left">
          {/* Permissions */}
          <section className="admin-section">
            <h2>Permissions</h2>
            <div className="permissions-grid">
              {PERMISSION_GROUPS.map((group) => {
                const isSelected = selectedPermissions.includes(group);
                return (
                  <div
                    key={group}
                    className={`permission-item ${isSelected ? "selected" : ""}`}
                    onClick={() => togglePermission(group)}
                    role="checkbox"
                    aria-checked={isSelected}
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        togglePermission(group);
                      }
                    }}
                  >
                    <div className="permission-checkbox">
                      {isSelected && <span className="permission-checkbox-icon">✓</span>}
                    </div>
                    <span className="permission-label">{group}</span>
                  </div>
                );
              })}
            </div>
          </section>

          {/* Upload */}
          <section className="admin-section">
            <h2>Bulk Upload (CSV / Documents)</h2>
            <div
              className={`upload-zone ${dragOver ? "drag-over" : ""}`}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onClick={() => fileInputRef.current?.click()}
            >
              <div className="upload-icon">&#128194;</div>
              <p>Drag &amp; drop files here</p>
              <p className="upload-hint">
                Supports CSV, XLSX, PDF, DOC, DOCX &mdash; up to 50MB per file
              </p>
              <button
                type="button"
                className="upload-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  fileInputRef.current?.click();
                }}
              >
                Browse Files
              </button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".csv,.xlsx,.xls,.pdf,.doc,.docx"
              style={{ display: "none" }}
              onChange={(e) => handleFiles(e.target.files)}
            />

            {uploadedFiles.length > 0 && (
              <div className="file-list">
                <h3>Queued Files ({uploadedFiles.length})</h3>
                {uploadedFiles.map((item) => (
                  <div key={item.id} className="file-item">
                    <div className="file-item-info">
                      <span className="file-item-icon">&#128196;</span>
                      <span className="file-item-name">{item.file.name}</span>
                      <span className="file-item-size">
                        {formatFileSize(item.file.size)}
                      </span>
                    </div>
                    <button
                      className="file-remove-btn"
                      onClick={() => removeFile(item.id)}
                      aria-label={`Remove ${item.file.name}`}
                    >
                      &times;
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Submit */}
          <button
            className="admin-submit-btn"
            onClick={handleSubmit}
            disabled={uploadedFiles.length === 0 && selectedPermissions.length === 0}
          >
            Submit Upload &rarr;
          </button>
        </div>

        {/* RIGHT: Chat */}
        <div className="admin-right">
          <div className="hub-chat">
            <div className="hub-chat-header">
              <span className="chat-logo"><strong>MIAX</strong> CHAT</span>
            </div>

            <div className="hub-chat-hero">
              <h2>Your Institutional Market Assistant</h2>
              <p>AI-powered global market insights and technology support, available 24/7.</p>
            </div>

            <div className="hub-chat-messages">
              {messages.map((msg, i) => (
                <div key={i} className={`hub-chat-bubble ${msg.role}`}>
                  <p>{msg.content}</p>
                </div>
              ))}
            </div>

            <form className="hub-chat-input-area" onSubmit={handleChatSend}>
              <div className="hub-chat-input-wrapper">
                <input
                  type="text"
                  placeholder="Type your market query here..."
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  className="hub-chat-input"
                />
                <div className="hub-chat-input-actions">
                  <button type="button" className="hub-input-icon-btn" aria-label="Attach">&#128206;</button>
                </div>
                <button type="submit" className="hub-chat-send-btn">
                  ASK MIAX &rarr;
                </button>
                <button type="button" className="hub-chat-settings-btn" aria-label="Settings">&#9881;</button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
