"use client";

import { useState, useRef, useEffect, DragEvent } from "react";
import ReactMarkdown from "react-markdown";
import "./admin.css";
import Sidebar, { SidebarView } from "../sidebar/sidebar";
import { queryAgent, uploadSingleFile, fileToBase64, getPresignedUrl, uploadToS3, generateBatchId, bulkIngest, getStats } from "../../src/lib/api";


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
  // Source document names that grounded a bot answer (deduped, in order).
  sources?: string[];
}

/** Turn an S3 URI / location into a readable document name. */
function docNameFromUri(uri: string | null): string | null {
  if (!uri) return null;
  // Strip query string, take the last path segment (the file name).
  const clean = uri.split("?")[0].replace(/\/+$/, "");
  const name = clean.substring(clean.lastIndexOf("/") + 1);
  return name || null;
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
  // The chat has its OWN permission group (independent of the upload dropdown),
  // so you can switch between groups A/B/C within a single conversation to test
  // that retrieval is correctly isolated per group.
  const [chatPermission, setChatPermission] = useState<string>("Permissions Group A");


  // Uploaded file history
  const [completedFiles, setCompletedFiles] = useState<string[]>([]);

  // Connection status to the backend / knowledge base. Determined by whether a
  // lightweight API call (the stats endpoint) succeeds with the configured API
  // key. null = checking, true = linked, false = disconnected.
  const [connected, setConnected] = useState<boolean | null>(null);

  async function checkConnection() {
    try {
      await getStats();
      setConnected(true);
    } catch {
      setConnected(false);
    }
  }

  // Check connectivity on load.
  useEffect(() => {
    checkConnection();
  }, []);


  // --- Permissions ---
  const selectedGroupObj = PERMISSION_GROUPS.find((g) => g.id === selectedPermission);


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
    if (uploadedFiles.length === 0) return;
    setBulkUploading(true);
    setBulkStatus(null);

    // Find the manifest CSV among the uploaded files
    const csvFiles = uploadedFiles.filter((f) => f.file.name.toLowerCase().endsWith(".csv"));
    const csvFile = csvFiles[0];
    const docFiles = uploadedFiles.filter((f) => !f.file.name.toLowerCase().endsWith(".csv"));

    // Enforce exactly one CSV manifest.
    if (csvFiles.length === 0) {
      setBulkStatus("No CSV manifest found. Include exactly one CSV with 'filename' and 'permissions' columns.");
      setBulkUploading(false);
      return;
    }
    if (csvFiles.length > 1) {
      setBulkStatus("Multiple CSV files found. Include exactly ONE CSV manifest.");
      setBulkUploading(false);
      return;
    }

    if (docFiles.length === 0) {

      setBulkStatus("No document files found. Include PDFs or other docs alongside the CSV manifest.");
      setBulkUploading(false);
      return;
    }

    const batchId = generateBatchId();

    // Step 1: Upload all document files to S3 staging
    for (const item of docFiles) {
      try {
        const { upload_url } = await getPresignedUrl({
          filename: item.file.name,
          batch_id: batchId,
        });
        await uploadToS3(upload_url, item.file);
      } catch {
        setBulkStatus(`Failed to stage "${item.file.name}" to S3.`);
        setBulkUploading(false);
        return;
      }
    }

    // Step 2: Upload the CSV as manifest.csv in the staging folder
    try {
      const { upload_url } = await getPresignedUrl({
        filename: "manifest.csv",
        batch_id: batchId,
      });
      await uploadToS3(upload_url, csvFile.file);
    } catch {
      setBulkStatus("Failed to upload manifest CSV to S3.");
      setBulkUploading(false);
      return;
    }

    // Step 3: Call bulk-ingest (Lambda reads manifest from S3)
    try {
      const result = await bulkIngest({ batch_id: batchId });

      const successCount = result.processed.length;
      const errorCount = result.errors.length;
      setBulkStatus(
        `Processed ${successCount} file(s)${errorCount > 0 ? `, ${errorCount} failed` : ""}.`
      );
      if (successCount > 0) {
        setUploadedFiles([]);
        setCompletedFiles((prev) => [...prev, ...result.processed.map((p) => p.filename)]);
        checkConnection();
      }
      if (errorCount > 0) {
        const errorDetails = result.errors.map((e) => `${e.filename || "?"}: ${e.reason}`).join("; ");
        setBulkStatus((prev) => `${prev} Errors: ${errorDetails}`);
      }
    } catch (err) {
      setBulkStatus(
        `Bulk ingest failed: ${err instanceof Error ? err.message : "Unknown error"}`
      );
    } finally {
      setBulkUploading(false);
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
      checkConnection();
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
      // Use the CHAT's own permission group (not the upload dropdown) so you can
      // switch groups mid-conversation and verify per-group retrieval isolation.
      const group = chatPermission.toLowerCase().replace(/\s+/g, "_");
      const response = await queryAgent({
        username: "admin@miax.com",
        permission_group: group,
        prompt: chatInput,
      });


      // A successful query proves we're connected (even if /stats isn't
      // deployed yet) - flip the connection flag green.
      setConnected(true);

      // Collect the source document names that grounded this answer.
      const sources = Array.from(
        new Set(
          (response.citations || [])
            .map((c) => docNameFromUri(c.source_uri))
            .filter((n): n is string => Boolean(n))
        )
      );

      const botMsg: ChatMessage = { role: "bot", content: response.answer, sources };
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
            {connected === false ? (
              <span className="conn-flag conn-flag-red">● Disconnected from AWS</span>
            ) : connected === true ? (
              <span className="conn-flag conn-flag-green">● Linked to Knowledge Base</span>
            ) : (
              <span className="conn-flag conn-flag-neutral">● Checking connection…</span>
            )}
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
                Multiple Uploads
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

                {/* CSV format explainer */}
                <div className="bulk-format-box">
                  <strong>Bulk upload format</strong>
                  <p>Include exactly <b>one CSV</b> manifest plus the document files it references. The CSV needs two columns:</p>
                  <pre>{`filename,permissions
q3-report.pdf,permissions_group_a
roadmap.docx,permissions_group_b`}</pre>
                  <p>Each row maps a file to its permission group. The permission comes from the CSV — not the dropdown above.</p>
                </div>
                <button
                  className="admin-submit-btn"
                  onClick={handleBulkSubmit}
                  disabled={uploadedFiles.length === 0 || bulkUploading}
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
              {/* Chat permission selector - independent of the upload dropdown.
                  Switch this between groups A/B/C to test isolation in one convo. */}
              <div className="chat-permission-select">
                <label htmlFor="chat-perm">Querying as</label>
                <select
                  id="chat-perm"
                  value={chatPermission}
                  onChange={(e) => setChatPermission(e.target.value)}
                >
                  {PERMISSION_GROUPS.map((g) => (
                    <option key={g.id} value={g.id}>{g.label}</option>
                  ))}
                </select>
              </div>
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
                    <ReactMarkdown>{msg.content}</ReactMarkdown>
                    {msg.role === "bot" && msg.sources && msg.sources.length > 0 && (
                      <div className="chat-sources">
                        <span className="chat-sources-label">Sources:</span>
                        {msg.sources.map((s, j) => (
                          <span key={j} className="chat-source-chip">📄 {s}</span>
                        ))}
                      </div>
                    )}
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
