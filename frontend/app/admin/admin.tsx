"use client";

import { useState, useRef, useEffect, DragEvent } from "react";
import ReactMarkdown from "react-markdown";
import "./admin.css";
import Sidebar, { SidebarView } from "../sidebar/sidebar";
import { queryAgent, uploadViaPresign, getStats } from "../../src/lib/api";

interface AdminPanelProps {
  onBack?: () => void;
}

const PERMISSION_GROUPS = [
  { id: "Permissions Group A", label: "Permissions Group A", description: "General access, shared across the organization" },
  { id: "Permissions Group B", label: "Permissions Group B", description: "Shared with your immediate team only" },
  { id: "Permissions Group C", label: "Permissions Group C", description: "Restricted, limited to named collaborators" },
];

// Map a UI label like "Permissions Group A" to the backend value "permissions_group_a".
function toGroupId(label: string): string {
  return label.toLowerCase().replace(/\s+/g, "_");
}

interface UploadedFile {
  file: File;
  id: string;
}

interface ChatMessage {
  role: "bot" | "user";
  content: string;
  sources?: string[];
}

/** Readable document name from an S3 URI / location. */
function docNameFromUri(uri: string | null): string | null {
  if (!uri) return null;
  const clean = uri.split("?")[0].replace(/\/+$/, "");
  const name = clean.substring(clean.lastIndexOf("/") + 1);
  return name || null;
}

/** Minimal CSV parser that handles quoted fields containing commas. */
function parseCsv(text: string): Record<string, string>[] {
  const rows: string[][] = [];
  let field = "";
  let row: string[] = [];
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') inQuotes = false;
      else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (field !== "" || row.length) { row.push(field); rows.push(row); row = []; field = ""; }
      if (c === "\r" && text[i + 1] === "\n") i++;
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  if (rows.length === 0) return [];
  const headers = rows[0].map((h) => h.trim().toLowerCase());
  return rows.slice(1)
    .filter((r) => r.some((c) => c.trim() !== ""))
    .map((r) => {
      const obj: Record<string, string> = {};
      headers.forEach((h, idx) => { obj[h] = (r[idx] || "").trim(); });
      return obj;
    });
}

type UploadTab = "single" | "bulk";

export default function AdminPanel({ onBack }: AdminPanelProps) {
  const [activeView, setActiveView] = useState<SidebarView>("assistant");

  // Permission dropdown (single-file upload only)
  const [selectedPermission, setSelectedPermission] = useState<string>("");
  const [permDropdownOpen, setPermDropdownOpen] = useState(false);

  const [activeTab, setActiveTab] = useState<UploadTab>("single");

  // Bulk upload
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const [bulkDragOver, setBulkDragOver] = useState(false);
  const [bulkUploading, setBulkUploading] = useState(false);
  const [bulkStatus, setBulkStatus] = useState<string | null>(null);
  const bulkFileInputRef = useRef<HTMLInputElement>(null);

  // Single upload
  const [singleFile, setSingleFile] = useState<File | null>(null);
  const [singleDragOver, setSingleDragOver] = useState(false);
  const [singleUploading, setSingleUploading] = useState(false);
  const [singleStatus, setSingleStatus] = useState<string | null>(null);
  const singleFileInputRef = useRef<HTMLInputElement>(null);

  // Chat
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [chatPermission, setChatPermission] = useState<string>("Permissions Group A");

  const [completedFiles, setCompletedFiles] = useState<string[]>([]);

  // Connection flag: green once a backend call succeeds.
  const [connected, setConnected] = useState<boolean | null>(null);

  async function checkConnection() {
    try { await getStats(); setConnected(true); } catch { setConnected(false); }
  }
  useEffect(() => { checkConnection(); }, []);

  const selectedGroupObj = PERMISSION_GROUPS.find((g) => g.id === selectedPermission);

  // --- Bulk ---
  function handleBulkFiles(files: FileList | null) {
    if (!files) return;
    const newFiles: UploadedFile[] = Array.from(files).map((file) => ({
      file,
      id: `${file.name}-${Date.now()}-${Math.random()}`,
    }));
    setUploadedFiles((prev) => [...prev, ...newFiles]);
  }
  function handleBulkDrop(e: DragEvent<HTMLDivElement>) { e.preventDefault(); setBulkDragOver(false); handleBulkFiles(e.dataTransfer.files); }
  function handleBulkDragOver(e: DragEvent<HTMLDivElement>) { e.preventDefault(); setBulkDragOver(true); }
  function handleBulkDragLeave() { setBulkDragOver(false); }
  function removeFile(id: string) { setUploadedFiles((prev) => prev.filter((f) => f.id !== id)); }

  function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  async function handleBulkSubmit() {
    if (uploadedFiles.length === 0) return;
    setBulkUploading(true);
    setBulkStatus(null);

    const csvFiles = uploadedFiles.filter((f) => f.file.name.toLowerCase().endsWith(".csv"));
    const docFiles = uploadedFiles.filter((f) => !f.file.name.toLowerCase().endsWith(".csv"));

    if (csvFiles.length !== 1) {
      setBulkStatus(csvFiles.length === 0
        ? "Include exactly one CSV manifest (columns: filename, permissions)."
        : "Multiple CSV files found — include exactly ONE CSV manifest.");
      setBulkUploading(false);
      return;
    }
    if (docFiles.length === 0) {
      setBulkStatus("No document files found. Add the files your CSV references.");
      setBulkUploading(false);
      return;
    }

    // Parse the manifest -> map filename to permission group.
    let rows: Record<string, string>[];
    try {
      rows = parseCsv(await csvFiles[0].file.text());
    } catch {
      setBulkStatus("Could not read the CSV manifest.");
      setBulkUploading(false);
      return;
    }
    const permByName = new Map<string, string>();
    for (const r of rows) {
      const name = (r["filename"] || r["file_name"] || "").trim();
      const perm = (r["permissions"] || r["permission_group"] || r["permissions_group"] || "").trim();
      if (name) permByName.set(name, perm);
    }

    const validGroups = new Set(PERMISSION_GROUPS.map((g) => toGroupId(g.id)));
    const errors: string[] = [];

    // Validate every uploaded doc has a matching, valid CSV row.
    for (const item of docFiles) {
      const perm = permByName.get(item.file.name.trim());
      if (!perm) errors.push(`"${item.file.name}" is not listed in the CSV.`);
      else if (!validGroups.has(perm)) errors.push(`"${item.file.name}" has invalid permission "${perm}".`);
    }
    // Warn about CSV rows with no matching uploaded file.
    Array.from(permByName.keys()).forEach((name) => {
      if (!docFiles.some((d) => d.file.name.trim() === name)) {
        errors.push(`CSV lists "${name}" but it wasn't uploaded.`);
      }
    });

    if (errors.length) {
      setBulkStatus(`Fix these before uploading: ${errors.join(" ")}`);
      setBulkUploading(false);
      return;
    }

    // Upload each file directly to S3 with its permission baked into metadata.
    let ok = 0;
    const failed: string[] = [];
    for (const item of docFiles) {
      const perm = permByName.get(item.file.name.trim())!;
      try {
        await uploadViaPresign(item.file, perm);
        ok++;
      } catch (err) {
        failed.push(`${item.file.name}: ${err instanceof Error ? err.message : "failed"}`);
      }
    }

    if (ok > 0) {
      setCompletedFiles((prev) => [...prev, ...docFiles.filter((d) => !failed.some((f) => f.startsWith(d.file.name))).map((d) => d.file.name)]);
      setUploadedFiles([]);
      checkConnection();
    }
    setBulkStatus(`Uploaded ${ok} file(s)${failed.length ? `, ${failed.length} failed: ${failed.join("; ")}` : ". They’ll be indexed shortly."}`);
    setBulkUploading(false);
  }

  // --- Single ---
  function handleSingleFileDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault(); setSingleDragOver(false);
    const files = e.dataTransfer.files;
    if (files && files.length > 0) { setSingleFile(files[0]); setSingleStatus(null); }
  }
  function handleSingleFileDragOver(e: DragEvent<HTMLDivElement>) { e.preventDefault(); setSingleDragOver(true); }
  function handleSingleFileDragLeave() { setSingleDragOver(false); }

  async function handleSingleFileSubmit() {
    if (!singleFile || !selectedPermission) return;
    setSingleUploading(true);
    setSingleStatus(null);
    try {
      await uploadViaPresign(singleFile, toGroupId(selectedPermission));
      setSingleStatus(`Uploaded "${singleFile.name}". It’ll be indexed shortly.`);
      setCompletedFiles((prev) => [...prev, singleFile.name]);
      checkConnection();
      setSingleFile(null);
    } catch (err) {
      setSingleStatus(`Upload failed: ${err instanceof Error ? err.message : "Unknown error"}`);
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
      const response = await queryAgent({
        username: "admin@miax.com",
        permission_group: toGroupId(chatPermission),
        prompt: chatInput,
      });
      setConnected(true);
      const sources = Array.from(new Set(
        (response.citations || [])
          .map((c) => docNameFromUri(c.source_uri))
          .filter((n): n is string => Boolean(n))
      ));
      setMessages((prev) => [...prev, { role: "bot", content: response.answer, sources }]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "bot", content: `Sorry, something went wrong: ${err instanceof Error ? err.message : "Unknown error"}` }]);
    } finally {
      setChatLoading(false);
    }
  }

  return (
    <div className="admin-layout">
      <Sidebar activeView={activeView} onNavigate={setActiveView} onBack={onBack} />

      <main className="admin-main">
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

        <div className="admin-content">
          {/* Left: Upload */}
          <div className="admin-upload-panel">
            <h2>Upload documents</h2>
            <p className="upload-subtitle">Set an access level, then add the files you want to chat with.</p>

            {/* Upload tabs */}
            <div className="upload-tabs">
              <button className={`upload-tab ${activeTab === "single" ? "active" : ""}`} onClick={() => setActiveTab("single")}>Single File</button>
              <button className={`upload-tab ${activeTab === "bulk" ? "active" : ""}`} onClick={() => setActiveTab("bulk")}>Multiple Uploads</button>
            </div>

            {/* Single File Tab */}
            {activeTab === "single" && (
              <div className="upload-tab-content">
                {/* Permission group (single only) */}
                <div className="permission-section">
                  <label className="permission-section-label">Permission group</label>
                  <div className="permission-custom-dropdown">
                    <button className="permission-dropdown-trigger" onClick={() => setPermDropdownOpen(!permDropdownOpen)} type="button">
                      <span className="permission-dropdown-text">{selectedGroupObj?.label || "Select a group..."}</span>
                      <span className={`permission-dropdown-arrow ${permDropdownOpen ? "open" : ""}`}>&#8964;</span>
                    </button>
                    {permDropdownOpen && (
                      <div className="permission-dropdown-menu">
                        {PERMISSION_GROUPS.map((group) => (
                          <button key={group.id}
                            className={`permission-dropdown-option ${selectedPermission === group.id ? "selected" : ""}`}
                            onClick={() => { setSelectedPermission(group.id); setPermDropdownOpen(false); }} type="button">
                            <div className="permission-option-text">
                              <span className="permission-option-label">{group.label}</span>
                              <span className="permission-option-desc">{group.description}</span>
                            </div>
                            {selectedPermission === group.id && <span className="permission-option-check">✓</span>}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                <div className={`upload-zone ${singleDragOver ? "drag-over" : ""}`}
                  onDrop={handleSingleFileDrop} onDragOver={handleSingleFileDragOver} onDragLeave={handleSingleFileDragLeave}
                  onClick={() => singleFileInputRef.current?.click()}>
                  <div className="upload-icon">&#128196;</div>
                  <p className="upload-zone-title">Drag &amp; drop a file or click to browse</p>
                  <p className="upload-hint">Supports PDF, DOCX, TXT, CSV and Markdown</p>
                  <button type="button" className="upload-btn" onClick={(e) => { e.stopPropagation(); singleFileInputRef.current?.click(); }}>Select file</button>
                </div>
                <input ref={singleFileInputRef} type="file" accept=".csv,.xlsx,.xls,.pdf,.doc,.docx,.txt,.md" style={{ display: "none" }}
                  onChange={(e) => { if (e.target.files && e.target.files.length > 0) { setSingleFile(e.target.files[0]); setSingleStatus(null); } }} />

                {singleFile && (
                  <div className="file-list">
                    <div className="file-item">
                      <div className="file-item-info">
                        <span className="file-item-icon">&#128196;</span>
                        <span className="file-item-name">{singleFile.name}</span>
                        <span className="file-item-size">{formatFileSize(singleFile.size)}</span>
                      </div>
                      <button className="file-remove-btn" onClick={() => { setSingleFile(null); setSingleStatus(null); }} aria-label="Remove file">&times;</button>
                    </div>
                  </div>
                )}

                <button className="admin-submit-btn" onClick={handleSingleFileSubmit} disabled={!singleFile || !selectedPermission || singleUploading}>
                  {singleUploading ? "Uploading..." : "Process & Upload →"}
                </button>
                {singleStatus && <p className="upload-status">{singleStatus}</p>}
              </div>
            )}

            {/* Bulk File Tab */}
            {activeTab === "bulk" && (
              <div className="upload-tab-content">
                <div className={`upload-zone ${bulkDragOver ? "drag-over" : ""}`}
                  onDrop={handleBulkDrop} onDragOver={handleBulkDragOver} onDragLeave={handleBulkDragLeave}
                  onClick={() => bulkFileInputRef.current?.click()}>
                  <div className="upload-icon">&#128194;</div>
                  <p className="upload-zone-title">Drag &amp; drop files or click to browse</p>
                  <p className="upload-hint">One CSV manifest + the document files it references</p>
                  <button type="button" className="upload-btn" onClick={(e) => { e.stopPropagation(); bulkFileInputRef.current?.click(); }}>Select files</button>
                </div>
                <input ref={bulkFileInputRef} type="file" multiple accept=".csv,.xlsx,.xls,.pdf,.doc,.docx,.txt,.md" style={{ display: "none" }}
                  onChange={(e) => handleBulkFiles(e.target.files)} />

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
                        <button className="file-remove-btn" onClick={() => removeFile(item.id)} aria-label={`Remove ${item.file.name}`}>&times;</button>
                      </div>
                    ))}
                  </div>
                )}

                <div className="bulk-format-box">
                  <strong>Bulk upload format</strong>
                  <p>Include exactly <b>one CSV</b> manifest plus the document files it references. The CSV needs two columns:</p>
                  <pre>{`filename,permissions
q3-report.pdf,permissions_group_a
roadmap.docx,permissions_group_b`}</pre>
                  <p>Each row maps a file to its permission group. Files upload directly to S3 (no size limit) with the permission baked in.</p>
                </div>
                <button className="admin-submit-btn" onClick={handleBulkSubmit} disabled={uploadedFiles.length === 0 || bulkUploading}>
                  {bulkUploading ? "Uploading..." : "Process & Upload All →"}
                </button>
                {bulkStatus && <p className="upload-status">{bulkStatus}</p>}
              </div>
            )}

            {/* Uploaded files */}
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

          {/* Right: Chat */}
          <div className="admin-chat-panel">
            <div className="chat-panel-header">
              <div>
                <h2>Document Assistant</h2>
                <p>Ask questions across your uploaded content</p>
              </div>
              <div className="chat-permission-select">
                <label htmlFor="chat-perm">Querying as</label>
                <select id="chat-perm" value={chatPermission} onChange={(e) => setChatPermission(e.target.value)}>
                  {PERMISSION_GROUPS.map((g) => (<option key={g.id} value={g.id}>{g.label}</option>))}
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
                        {msg.sources.map((s, j) => (<span key={j} className="chat-source-chip">📄 {s}</span>))}
                      </div>
                    )}
                  </div>
                ))
              )}
              {chatLoading && (
                <div className="chat-panel-bubble bot">
                  <span className="chat-spinner" aria-label="Assistant is thinking" />
                  <span className="chat-thinking-text">Thinking…</span>
                </div>
              )}
            </div>

            <form className="chat-panel-input-area" onSubmit={handleChatSend}>

              <div className="chat-panel-input-wrapper">
                <input type="text" placeholder="Ask a question about your documents..." value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)} className="chat-panel-input" />
                <button type="submit" className="chat-panel-send-btn" disabled={chatLoading}>
                  {chatLoading ? "..." : "✈ Send"}
                </button>
              </div>
            </form>
          </div>
        </div>
      </main>
    </div>
  );
}
