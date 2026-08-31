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

export interface ChatFileInfo {
  file_id: string;
  filename: string;
  row_count: number;
  truncated: boolean;
  masked_count: number;
  columns: ColumnInfo[];
}

export interface UploadResult {
  chat_id: string;
  file_id: string;
  filename: string;
  row_count: number;
  truncated: boolean;
  columns: ColumnInfo[];
  masked_count: number;
  preview_csv: string;
}

export interface AdminConfig {
  model: string;
  api_key_set: boolean;
  api_key_preview: string;
  common_models: string[];
}

export interface CurrentUser {
  sub: string;
  email: string | null;
  display_name: string | null;
  role: "admin" | "user";
  created_at: number;
  last_login_at: number;
}


export interface ModelOption {
  id: string;
  provider: string;
  model: string;
  label: string;
  description: string;
}

export interface ModelCatalogResponse {
  models: ModelOption[];
  default_model_id: string | null;
}
