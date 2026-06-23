"use client";

import { useState, useRef, DragEvent } from "react";
import "./admin.css";
import Sidebar, { SidebarView } from "../sidebar/sidebar";
import { queryAgent, uploadSingleFile, fileToBase64 } from "../../src/lib/api";

interface AdminPanelProps {
  onBack?: () => void;
}

const PERMISSION_GROUPS = [
  {
    id: "Permissions Group A",
    label: "Permissions Group A",
    description: "General access, shared across the organization",
  },
  {
    id: "Permissions Group B",
    label: "Permissions Group B",
    description: "Shared with your immediate team only",
  },
  {
    id: "Permissions Group C",
    label: "Permissions Group C",
    description: "Restricted, limited to named collaborators",
  },
];

interface UploadedFile {
  file: File;
  id: string;
  status?: "pending" | "uploaded" | "failed";
}

interface ChatMessage {
  role: "bot" | "user";
  content: string;
}

type UploadTab = "single" | "bulk";

export default function AdminPanel({ onBack }: AdminPanelProps) {
  // Navigation
  const [activeView, setActiveView] = useState<SidebarView>("assistant");

  // Permissions state
  const [selectedPermission, setSelectedPermission] = useState<string>("");
  const [permDropdownOpen, setPermDropdownOpen] = useState(false);

  // Keep selectedPermissions array in sync for the API calls
  const selectedPermissions = selectedPermission ? [selectedPermission] : [];

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
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);

  // Uploaded file history
  const [completedFiles, setCompletedFiles] = useState<string[]>([]);

  // --- Permissions ---
  const selectedGroupObj = PERMISSION_GROUPS.find((g) => g.id === selectedPermission);

  // Format markdown-style bold (**text**) to HTML
  function formatMessage(text: string): string {
    return text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  // --- Bulk file upload ---
  function handleBulkFiles(files: FileList | null) {
    if (!files) return;
    const newFiles: UploadedFile[] = Array.from(files).map((file) => ({
      file,
      id: `${file.name}-${Date.now()}-${Math.random()}`,
      status: "pending" as const,
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
        setCompletedFiles((prev) => [...prev, item.file.name]);
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
      setCompletedFiles((prev) => [...prev, singleFile.name]);
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
    <div className="admin-layout">
      {/* Sidebar */}
      <Sidebar activeView={activeView} onNavigate={setActiveView} onBack={onBack} />

      {/* Main content */}
      <main className="admin-main">
        {/* Top bar */}
        <div className="admin-topbar">
          <div className="admin-topbar-left">
            <h1>Miax Document Assistant</h1>
            <p>Upload, organize, and chat with your knowledge base</p>
          </div>
          <div className="admin-topbar-right">
            <span className="indexed-badge">● {completedFiles.length} indexed</span>
          </div>
        </div>

        {/* Content area */}
        <div className="admin-content">
          {/* Left panel — Upload */}
          <div className="admin-upload-panel">
            <h2>Upload documents</h2>
            <p className="upload-subtitle">Set an access level, then add the files you want to chat with.</p>

            {/* Permission group */}
            <div className="permission-section">
              <label className="permission-section-label">Permission group</label>
              <div className="permission-custom-dropdown">
                <button
                  className="permission-dropdown-trigger"
                  onClick={() => setPermDropdownOpen(!permDropdownOpen)}
                  type="button"
                >
                  <span className="permission-dropdown-text">
                    {selectedGroupObj?.label || "Select a group..."}
                  </span>
                  <span className={`permission-dropdown-arrow ${permDropdownOpen ? "open" : ""}`}>
                    &#8964;
                  </span>
                </button>
                {permDropdownOpen && (
                  <div className="permission-dropdown-menu">
                    {PERMISSION_GROUPS.map((group) => (
                      <button
                        key={group.id}
                        className={`permission-dropdown-option ${selectedPermission === group.id ? "selected" : ""}`}
                        onClick={() => {
                          setSelectedPermission(group.id);
                          setPermDropdownOpen(false);
                        }}
                        type="button"
                      >
                        <div className="permission-option-text">
                          <span className="permission-option-label">{group.label}</span>
                          <span className="permission-option-desc">{group.description}</span>
                        </div>
                        {selectedPermission === group.id && (
                          <span className="permission-option-check">✓</span>
                        )}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Upload tabs */}
            <div className="upload-tabs">
              <button
                className={`upload-tab ${activeTab === "single" ? "active" : ""}`}
                onClick={() => setActiveTab("single")}
              >
                Single File
              </button>
              <button
                className={`upload-tab ${activeTab === "bulk" ? "active" : ""}`}
                onClick={() => setActiveTab("bulk")}
              >
                Bulk Upload
              </button>
            </div>

            {/* Single File Tab */}
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
                  <p className="upload-zone-title">Drag &amp; drop a file or click to browse</p>
                  <p className="upload-hint">Supports PDF, DOCX, TXT, CSV and Markdown - up to 6MB</p>
                  <button
                    type="button"
                    className="upload-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      singleFileInputRef.current?.click();
                    }}
                  >
                    Select file
                  </button>
                </div>
                <input
                  ref={singleFileInputRef}
                  type="file"
                  accept=".csv,.xlsx,.xls,.pdf,.doc,.docx,.txt,.md"
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

                <button
                  className="admin-submit-btn"
                  onClick={handleSingleFileSubmit}
                  disabled={!singleFile || selectedPermissions.length === 0 || singleUploading}
                >
                  {singleUploading ? "Uploading..." : "Process & Upload →"}
                </button>
                {singleStatus && <p className="upload-status">{singleStatus}</p>}
              </div>
            )}

            {/* Bulk File Tab */}
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
                  <p className="upload-zone-title">Drag &amp; drop files or click to browse</p>
                  <p className="upload-hint">Supports PDF, DOCX, TXT, CSV - up to 6MB each</p>
                  <button
                    type="button"
                    className="upload-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      bulkFileInputRef.current?.click();
                    }}
                  >
                    Select files
                  </button>
                </div>
                <input
                  ref={bulkFileInputRef}
                  type="file"
                  multiple
                  accept=".csv,.xlsx,.xls,.pdf,.doc,.docx,.txt,.md"
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
                          <span className="file-item-size">{formatFileSize(item.file.size)}</span>
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

                <button
                  className="admin-submit-btn"
                  onClick={handleBulkSubmit}
                  disabled={uploadedFiles.length === 0 || selectedPermissions.length === 0 || bulkUploading}
                >
                  {bulkUploading ? "Uploading..." : "Process & Upload All →"}
                </button>
                {bulkStatus && <p className="upload-status">{bulkStatus}</p>}
              </div>
            )}

            {/* Uploaded files section */}
            <div className="uploaded-files-section">
              <div className="uploaded-files-header">
                <span>Uploaded files</span>
                <span className="uploaded-files-count">{completedFiles.length}</span>
              </div>
              {completedFiles.length === 0 ? (
                <div className="uploaded-files-empty">
                  <p>No documents yet</p>
                  <p className="uploaded-files-hint">Uploaded files will appear here.</p>
                </div>
              ) : (
                <div className="uploaded-files-list">
                  {completedFiles.map((name, i) => (
                    <div key={i} className="uploaded-file-item">
                      <span className="file-item-icon">&#128196;</span>
                      <span className="file-item-name">{name}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Right panel — Document Assistant Chat */}
          <div className="admin-chat-panel">
            <div className="chat-panel-header">
              <div>
                <h2>Document Assistant</h2>
                <p>Ask questions across your uploaded content</p>
              </div>
              <span className="chat-docs-badge">● {completedFiles.length} docs</span>
            </div>

            <div className="chat-panel-messages">
              {messages.length === 0 ? (
                <div className="chat-empty-state">
                  <div className="chat-empty-icon">&#128196;</div>
                  <h3>Chat with your documents</h3>
                  <p>Upload at least one document to start asking questions.</p>
                </div>
              ) : (
                messages.map((msg, i) => (
                  <div key={i} className={`chat-panel-bubble ${msg.role}`}>
                    <p dangerouslySetInnerHTML={{ __html: formatMessage(msg.content) }} />
                  </div>
                ))
              )}
            </div>

            <form className="chat-panel-input-area" onSubmit={handleChatSend}>
              <div className="chat-panel-input-wrapper">
                <input
                  type="text"
                  placeholder="Upload a document to begin..."
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  className="chat-panel-input"
                />
                <button type="submit" className="chat-panel-send-btn" disabled={chatLoading}>
                  {chatLoading ? "..." : "✈ Send"}
                </button>
              </div>
              <p className="chat-panel-context">📎 {completedFiles.length} documents in context</p>
            </form>
          </div>
        </div>
      </main>
    </div>
  );
}
