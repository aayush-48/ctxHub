import { accessToken, login } from "./auth";

export class ApiError extends Error {
  constructor(public status: number, message: string, public details?: unknown) {
    super(message);
  }
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await accessToken();
  const r = await fetch(`/api/v1${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (r.status === 401) return login();
  if (r.status === 204) return undefined as T;
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, body.message || `HTTP ${r.status}`, body.details);
  return body as T;
}

export type Item = {
  id: string; item_key: string; type: string; subject: string; content: string;
  reason?: string | null; status: string; importance: number; pinned: boolean;
  created_at: string;
};
export type Commit = {
  id: string; message: string; kind: string; author: string; parents: string[];
  source_client?: string | null; source_model?: string | null; created_at: string;
  active_items: number;
};
export type Branch = { name: string; head_commit_id: string };
