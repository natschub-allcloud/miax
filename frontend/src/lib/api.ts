/**
 * MIAX API client — calls Next.js API routes which proxy to the backend.
 * No CORS issues, API key stays server-side.
 */

export interface QueryRequest {
  username: string;
  permission_group: string;
  prompt: string;
}

export interface Citation {
  content: string;
  source_uri: string | null;
  location_type: string | null;
  score: number | null;
  permission_group: string | null;
  metadata: Record<string, unknown>;
}

export interface QueryResponse {
  answer: string;
  permission_group: string;
  citations: Citation[];
  citation_count: number;
  usage: { tokens_in: number; tokens_out: number };
  latency_ms: number;
}

export interface SingleFileRequest {
  filename: string;
  permission_group: string;
  content_base64: string;
}

export interface SingleFileResponse {
  filename: string;
  permission_group: string;
  source_key: string;
}

export interface BulkIngestRequest {
  batch_id: string;
  manifest_csv?: string;
}

export interface BulkIngestResponse {
  batch_id: string;
  processed: Array<{
    filename: string;
    permission_group: string;
    source_key: string;
  }>;
  errors: Array<{
    filename?: string;
    reason: string;
  }>;
}

export interface ApiError {
  error: string;
  allowed_permission_groups?: string[];
}

/**
 * Send a prompt to the RAG query endpoint and return the answer + citations.
 */
export async function queryAgent(request: QueryRequest): Promise<QueryResponse> {
  const res = await fetch("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  const data = await res.json();

  if (!res.ok) {
    throw new Error((data as ApiError).error || `Request failed (${res.status})`);
  }

  return data as QueryResponse;
}

/**
 * Upload a single file with its permission group. The file is sent as base64.
 */
export async function uploadSingleFile(request: SingleFileRequest): Promise<SingleFileResponse> {
  const res = await fetch("/api/single-file", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  const data = await res.json();

  if (!res.ok) {
    throw new Error((data as ApiError).error || `Upload failed (${res.status})`);
  }

  return data as SingleFileResponse;
}

/**
 * Trigger bulk ingestion for a batch of files already staged in S3.
 */
export async function bulkIngest(request: BulkIngestRequest): Promise<BulkIngestResponse> {
  const res = await fetch("/api/bulk-ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  const data = await res.json();

  if (!res.ok) {
    throw new Error((data as ApiError).error || `Bulk ingest failed (${res.status})`);
  }

  return data as BulkIngestResponse;
}

/**
 * Convert a File object to a base64 string.
 */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      // Remove the data:...;base64, prefix
      const base64 = result.split(",")[1];
      resolve(base64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
