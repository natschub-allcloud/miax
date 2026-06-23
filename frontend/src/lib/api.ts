/**
 * MIAX API client — thin wrapper around the API Gateway endpoints.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

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

export interface ApiError {
  error: string;
  allowed_permission_groups?: string[];
}

/**
 * Send a prompt to the RAG query endpoint and return the answer + citations.
 */
export async function queryAgent(request: QueryRequest): Promise<QueryResponse> {
  const res = await fetch(`${API_URL}query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": API_KEY,
    },
    body: JSON.stringify(request),
  });

  const data = await res.json();

  if (!res.ok) {
    throw new Error((data as ApiError).error || `Request failed (${res.status})`);
  }

  return data as QueryResponse;
}
