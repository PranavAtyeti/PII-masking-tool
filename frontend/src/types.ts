// Mirrors backend/app/schemas.py. Kept 1:1 on purpose -- when session 4
// wires this up to the real API, the shapes should already match exactly,
// so there's nothing to reconcile beyond the fetch calls themselves.

export interface Chat {
  chat_id: string;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  masked_count: number;
}

export interface ColumnInfo {
  name: string;
  type: string | null;
  enabled: boolean;
}

export interface UploadPreviewResult {
  filename: string;
  row_count: number;
  columns: ColumnInfo[];
}

export interface UploadResult {
  chat_id: string;
  filename: string;
  row_count: number;
  truncated: boolean;
  columns: ColumnInfo[];
  kept_private_count: number;
  preview_csv: string;
}
