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


export interface PresignRequest {
  filename: string;
  permission_group: string;
}

export interface PresignResponse {
  upload_url: string;
  key: string;
  // Headers (e.g. x-amz-meta-permissions_group) that MUST be sent on the PUT.
  headers: Record<string, string>;
}


/**
 * Get a presigned S3 URL for uploading a file to the staging bucket.
 */
export async function getPresignedUrl(request: PresignRequest): Promise<PresignResponse> {
  const res = await fetch("/api/presign", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  const data = await res.json();

  if (!res.ok) {
    throw new Error((data as ApiError).error || `Presign failed (${res.status})`);
  }

  return data as PresignResponse;
}

/**
 * Upload a file directly to S3 using a presigned URL.
 * The `headers` returned by /presign (e.g. x-amz-meta-permissions_group) MUST
 * be replayed here or S3 rejects the PUT (the signature covers them).
 */
export async function uploadToS3(
  presignedUrl: string,
  file: File,
  extraHeaders: Record<string, string> = {}
): Promise<void> {
  const res = await fetch(presignedUrl, {
    method: "PUT",
    body: file,
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      ...extraHeaders,
    },
  });

  if (!res.ok) {
    throw new Error(`S3 upload failed (${res.status})`);
  }
}

/**
 * Upload one file end-to-end via the presigned-PUT path (no size limit, no
 * base64, never touches API Gateway). The permission group is baked into the
 * presigned URL's metadata, and the ingest Lambda picks it up from S3.
 */
export async function uploadViaPresign(file: File, permissionGroup: string): Promise<void> {
  const { upload_url, headers } = await getPresignedUrl({
    filename: file.name,
    permission_group: permissionGroup,
  });
  await uploadToS3(upload_url, file, headers);
}


export interface StatsResponse {
  indexed: number;
  documents: string[];
}

/**
 * Fetch how many documents are currently in the knowledge base (source bucket).
 * Backs the "N indexed" badge with a real count.
 */
export async function getStats(): Promise<StatsResponse> {
  const res = await fetch("/api/stats", { method: "GET" });
  const data = await res.json();
  if (!res.ok) {
    throw new Error((data as ApiError).error || `Stats failed (${res.status})`);
  }
  return data as StatsResponse;
}

