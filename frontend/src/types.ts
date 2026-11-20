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
  metadata?: {
    provider?: string;
    model?: string;
    files_used?: string[];
    files_skipped?: string[];
    masked_token_count?: number;
    security_override?: boolean;
    usage?: ProviderUsage;
    error?: ProviderErrorMetadata;
    sources?: DocumentSource[];
  };
}

export interface DocumentSource {
  reference: string;
  file_id: string;
  display_filename: string;
  heading_path: string[];
  source_id: string;
  kind: string;
  order: number;
  page_number: number | null;
}

export interface ProviderErrorMetadata {
  category: string;
  retryable: boolean;
  retry_after_seconds?: number | null;
}

export interface ProviderUsage {
  provider: string;
  model: string;
  source: "provider_reported" | "estimated";
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_input_tokens?: number | null;
  reasoning_tokens?: number | null;
  tool_tokens?: number | null;
  total_tokens?: number | null;
  estimated_input_tokens?: number | null;
  estimated_output_tokens?: number | null;
  estimated_cost?: number | null;
}

export interface ModelCapabilities {
  context_window_tokens: number | null;
  max_output_tokens: number | null;
  streaming: boolean | null;
  file_input: boolean | null;
  persistent_resources: boolean | null;
  cached_context: boolean | null;
  tool_calling: boolean | null;
  structured_output: boolean | null;
  token_usage_reporting: boolean | null;
  modalities: string[];
}

export interface ColumnInfo {
  name: string;
  display_name?: string | null;
  worksheet_name?: string | null;
  worksheet_id?: string | null;
  worksheet_visibility?: string | null;
  column_id?: string | null;
  type: string | null;
  enabled: boolean;
  recommended_action: PrivacyAction;
  sensitive: boolean;
  reason: string;
  action: PrivacyAction;
  share_confirmed: boolean;
}

export type PrivacyAction = "share" | "mask" | "exclude";

export interface WorksheetInfo {
  worksheet_id: string;
  name: string;
  position: number;
  row_count: number;
  column_count: number;
  visibility: string;
  has_header: boolean;
  is_empty: boolean;
  is_irregular: boolean;
}

export interface ColumnPrivacyChoice {
  action: PrivacyAction;
  share_confirmed: boolean;
}

export interface PrivacyPolicy {
  version?: number;
  columns: Record<string, ColumnPrivacyChoice>;
}

export interface UploadPreviewResult {
  filename: string;
  row_count: number;
  columns: ColumnInfo[];
  worksheet_count: number;
  worksheets: WorksheetInfo[];
  file_format?: string;
  section_count?: number;
  warnings?: string[];
}

export interface ChatFileInfo {
  file_id: string;
  filename: string;
  row_count: number;
  truncated: boolean;
  masked_count: number;
  columns: ColumnInfo[];
  privacy_policy: Record<string, ColumnPrivacyChoice>;
  worksheet_count: number;
  worksheets: WorksheetInfo[];
  file_format?: string;
  processing_status?: string;
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
  privacy_policy: PrivacyPolicy;
  worksheet_count: number;
  worksheets: WorksheetInfo[];
  file_format?: string;
  section_count?: number;
  warnings?: string[];
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
  role: "admin" | "user" | "guest";
  created_at: number;
  last_login_at: number;
}


export interface ModelOption {
  id: string;
  provider: string;
  model: string;
  label: string;
  description: string;
  capabilities?: ModelCapabilities;
}

export interface ModelCatalogResponse {
  models: ModelOption[];
  default_model_id: string | null;
}
