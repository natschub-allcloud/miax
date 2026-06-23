"use client";

import { useState, useRef, DragEvent } from "react";
import "./admin.css";
import { queryAgent, uploadSingleFile, fileToBase64 } from "../../src/lib/api";

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

type UploadTab = "single" | "bulk";

export default function AdminPanel({ onBack }: AdminPanelProps) {
  // Permissions state
  const [selectedPermissions, setSelectedPermissions] = useState<string[]>([]);

  // Upload tab state
  const [activeTab, setActiveTab] = useState<UploadTab>("single");

  // Bulk upload state
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const [bulkDragOver, setBulkDragOver] = useState(false);
  const [bulkUploading, setBulkUploading] = useState(false);
  const [bulkStatus, setBulkStatus] = useState<string | null>(null);
  const bulkFileInputRef = useRef<HTMLInputElement>(null);

  // Single file upload state
  const [singleFile, setSingleFile] = useState<File | null>(null);
  const [singleDragOver, setSingleDragOver] = useState(false);
  const [singleDescription, setSingleDescription] = useState("");
  const [singleUploading, setSingleUploading] = useState(false);
  const [singleStatus, setSingleStatus] = useState<string | null>(null);
  const singleFileInputRef = useRef<HTMLInputElement>(null);

  // Chat state
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: "bot",
      content:
        "Welcome to Miax - how can I assist you today?",
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

  // --- Bulk file upload ---
  function handleBulkFiles(files: FileList | null) {
    if (!files) return;
    const newFiles: UploadedFile[] = Array.from(files).map((file) => ({
      file,
      id: `${file.name}-${Date.now()}-${Math.random()}`,
    }));
    setUploadedFiles((prev) => [...prev, ...newFiles]);
  }

  function handleBulkDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setBulkDragOver(false);
    handleBulkFiles(e.dataTransfer.files);
  }

  function handleBulkDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setBulkDragOver(true);
  }

  function handleBulkDragLeave() {
    setBulkDragOver(false);
  }

  function removeFile(id: string) {
    setUploadedFiles((prev) => prev.filter((f) => f.id !== id));
  }

  function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  async function handleBulkSubmit() {
    if (uploadedFiles.length === 0 || selectedPermissions.length === 0) return;
    setBulkUploading(true);
    setBulkStatus(null);

    const permGroup = selectedPermissions[0].toLowerCase().replace(/\s+/g, "_");
    let successCount = 0;
    let errorCount = 0;

    for (const item of uploadedFiles) {
      try {
        const base64 = await fileToBase64(item.file);
        await uploadSingleFile({
          filename: item.file.name,
          permission_group: permGroup,
          content_base64: base64,
        });
        successCount++;
      } catch {
        errorCount++;
      }
    }

    setBulkUploading(false);
    setBulkStatus(
      `Uploaded ${successCount} file(s)${errorCount > 0 ? `, ${errorCount} failed` : ""}.`
    );
    if (successCount > 0) {
      setUploadedFiles([]);
    }
  }

  // --- Single file upload ---
  function handleSingleFileDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setSingleDragOver(false);
    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      setSingleFile(files[0]);
      setSingleStatus(null);
    }
  }

  function handleSingleFileDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setSingleDragOver(true);
  }

  function handleSingleFileDragLeave() {
    setSingleDragOver(false);
  }

  async function handleSingleFileSubmit() {
    if (!singleFile || selectedPermissions.length === 0) return;
    setSingleUploading(true);
    setSingleStatus(null);

    const permGroup = selectedPermissions[0].toLowerCase().replace(/\s+/g, "_");

    try {
      const base64 = await fileToBase64(singleFile);
      await uploadSingleFile({
        filename: singleFile.name,
        permission_group: permGroup,
        content_base64: base64,
      });
      setSingleStatus(`Uploaded "${singleFile.name}" successfully.`);
      setSingleFile(null);
      setSingleDescription("");
    } catch (err) {
      setSingleStatus(
        `Upload failed: ${err instanceof Error ? err.message : "Unknown error"}`
      );
    } finally {
      setSingleUploading(false);
    }
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
      const group = selectedPermissions[0]?.toLowerCase().replace(/\s+/g, "_") || "permissions_group_a";
      const response = await queryAgent({
        username: "admin@miax.com",
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
          <h1>Miax Hub</h1>
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
        {/* LEFT: Permissions + Uploads */}
        <div className="admin-left">
          {/* Permissions Context */}
          <section className="admin-section">
            <h2>Permissions Context</h2>
            <div className="permissions-grid">
              {PERMISSION_GROUPS.map((group) => {
                const isSelected = selectedPermissions.includes(group);
                return (
                  <label key={group} className="permission-toggle-item">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => togglePermission(group)}
                      className="permission-toggle-input"
                    />
                    <span className="permission-toggle-label">{group}</span>
                    <span className={`permission-toggle-switch ${isSelected ? "active" : ""}`}>
                      <span className="permission-toggle-knob" />
                    </span>
                  </label>
                );
              })}
            </div>
          </section>

          {/* Upload Container with Tabs */}
          <section className="admin-section">
            <h2>Upload Container</h2>

            {/* Tabs */}
            <div className="upload-tabs">
              <button
                className={`upload-tab ${activeTab === "single" ? "active" : ""}`}
                onClick={() => setActiveTab("single")}
              >
                [ SINGLE FILE UPLOAD ]
              </button>
              <button
                className={`upload-tab ${activeTab === "bulk" ? "active" : ""}`}
                onClick={() => setActiveTab("bulk")}
              >
                [ BULK FILE UPLOAD ]
              </button>
            </div>

            {/* Single File Upload Tab */}
            {activeTab === "single" && (
              <div className="upload-tab-content">
                <div
                  className={`upload-zone ${singleDragOver ? "drag-over" : ""}`}
                  onDrop={handleSingleFileDrop}
                  onDragOver={handleSingleFileDragOver}
                  onDragLeave={handleSingleFileDragLeave}
                  onClick={() => singleFileInputRef.current?.click()}
                >
                  <div className="upload-icon">&#128196;</div>
                  <p>Drag a single file here (max 6MB)</p>
                </div>
                <input
                  ref={singleFileInputRef}
                  type="file"
                  accept=".csv,.xlsx,.xls,.pdf,.doc,.docx,.txt"
                  style={{ display: "none" }}
                  onChange={(e) => {
                    if (e.target.files && e.target.files.length > 0) {
                      setSingleFile(e.target.files[0]);
                      setSingleStatus(null);
                    }
                  }}
                />

                {singleFile && (
                  <div className="file-list">
                    <div className="file-item">
                      <div className="file-item-info">
                        <span className="file-item-icon">&#128196;</span>
                        <span className="file-item-name">{singleFile.name}</span>
                        <span className="file-item-size">{formatFileSize(singleFile.size)}</span>
                      </div>
                      <button
                        className="file-remove-btn"
                        onClick={() => { setSingleFile(null); setSingleStatus(null); }}
                        aria-label="Remove file"
                      >
                        &times;
                      </button>
                    </div>
                  </div>
                )}

                <div className="single-file-meta">
                  <label className="single-file-label" htmlFor="single-file-desc">
                    File Description
                  </label>
                  <input
                    id="single-file-desc"
                    type="text"
                    className="single-file-input"
                    placeholder="Enter single file notes..."
                    value={singleDescription}
                    onChange={(e) => setSingleDescription(e.target.value)}
                  />
                </div>

                <div className="upload-submit-row">
                  <button
                    className="admin-submit-btn"
                    onClick={handleSingleFileSubmit}
                    disabled={!singleFile || selectedPermissions.length === 0 || singleUploading}
                  >
                    {singleUploading ? "Uploading..." : "Process & Upload Single File \u2192"}
                  </button>
                </div>
                {singleStatus && <p className="upload-status">{singleStatus}</p>}
              </div>
            )}

            {/* Bulk File Upload Tab */}
            {activeTab === "bulk" && (
              <div className="upload-tab-content">
                <div
                  className={`upload-zone ${bulkDragOver ? "drag-over" : ""}`}
                  onDrop={handleBulkDrop}
                  onDragOver={handleBulkDragOver}
                  onDragLeave={handleBulkDragLeave}
                  onClick={() => bulkFileInputRef.current?.click()}
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
                      bulkFileInputRef.current?.click();
                    }}
                  >
                    Browse Files
                  </button>
                </div>
                <input
                  ref={bulkFileInputRef}
                  type="file"
                  multiple
                  accept=".csv,.xlsx,.xls,.pdf,.doc,.docx"
                  style={{ display: "none" }}
                  onChange={(e) => handleBulkFiles(e.target.files)}
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

                <div className="upload-submit-row">
                  <button
                    className="admin-submit-btn"
                    onClick={handleBulkSubmit}
                    disabled={uploadedFiles.length === 0 || selectedPermissions.length === 0 || bulkUploading}
                  >
                    {bulkUploading ? "Uploading..." : "Process & Upload Bulk Files \u2192"}
                  </button>
                </div>
                {bulkStatus && <p className="upload-status">{bulkStatus}</p>}
              </div>
            )}
          </section>
        </div>

        {/* RIGHT: Chat */}
        <div className="admin-right">
          <div className="hub-chat">
            <div className="hub-chat-header">
              <span className="chat-logo"><strong>MIAX</strong> CHAT</span>
            </div>

            <div className="hub-chat-hero">
              <h2>Your Retrieval Assistant</h2>
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
                <button type="submit" className="hub-chat-send-btn" disabled={chatLoading}>
                  {chatLoading ? "..." : "ASK MIAX \u2192"}
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
