// 后端响应契约（对应 backend/schemas.py）。前端类型与后端保持同名便于对照。

export interface CustomerResponse {
  id: number;
  customer_name: string;
  industry: string;
  contact_person: string;
  contact_phone: string;
  cooperation_years: number;
  contact_frequency: string;
  last_contact_date: string | null;
  customer_satisfaction: number;
  contract_amount: number;
  payment_status: string;
  risk_signals: string;
  competitor_involvement: boolean;
  growth_potential: string;
  notes: string;
  custom_fields: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface CustomerListResponse {
  items: CustomerResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface DimensionScore {
  key: string;
  name: string;
  score: number;
  max_score: number;
  details: string[];
}

export interface AlertItem {
  id: string;
  level: string; // high / medium / low
  message: string;
}

export interface AssessmentResponse {
  customer_id: number;
  customer_name: string;
  total_score: number;
  max_score: number;
  level: string;
  level_color: string;
  dimensions: DimensionScore[];
  risk_alerts: string[];
  alerts: AlertItem[];
  suggestions: string[];
  config_version: string;
  assessed_at: string;
}

export interface FactorInputSpec {
  type: string;
  options: string[];
  min: number | null;
  max: number | null;
  step: number | null;
  unit: string;
  placeholder: string;
}

export interface FactorConfigItem {
  field: string;
  label: string;
  weight: number;
  source: string;
  source_role: string;
  description: string;
  sub_dimension: string;
  rule_text: string;
  rule_type: string;
  editable: boolean;
  input: FactorInputSpec;
}

export interface DimensionConfigItem {
  key: string;
  name: string;
  max_score: number;
  enabled: boolean;
  description: string;
  factors: FactorConfigItem[];
}

export interface LevelConfigItem {
  name: string;
  min_score: number;
  color: string;
}

export interface FactorConfigResponse {
  version: string;
  updated_at: string;
  description: string;
  strategy: string;
  total_max_score: number;
  dimensions: DimensionConfigItem[];
  levels: LevelConfigItem[];
}

export interface FactorUpdateResponse {
  customer: CustomerResponse;
  assessment: AssessmentResponse;
  updated_fields: string[];
  ignored_fields: string[];
}

export interface AssessmentHistoryItem {
  id: number;
  customer_id: number;
  assessed_by: string;
  trigger: string;
  total_score: number;
  max_score: number;
  level: string;
  level_color: string;
  dimensions: DimensionScore[];
  risk_alerts: string[];
  factor_snapshot: Record<string, unknown>;
  strategy_snapshot: unknown[];
  config_version: string;
  assessed_at: string;
}

export interface AssessmentHistoryResponse {
  customer_id: number;
  customer_name: string;
  total: number;
  items: AssessmentHistoryItem[];
}

export interface TrendPoint {
  assessed_at: string;
  label: string;
  total_score: number;
  level: string;
  dimensions: Record<string, number>;
}

export interface AssessmentTrendResponse {
  customer_id: number;
  customer_name: string;
  max_score: number;
  points: TrendPoint[];
  latest_score: number;
  previous_score: number | null;
  delta: number;
  trend: string; // up / down / flat
  level: string;
  level_color: string;
  level_lines: LevelConfigItem[];
}

export interface StrategyItem {
  priority: string; // recommended / alternative / long_term
  title: string;
  urgency: string; // high / medium / low
  reason: string;
  action: string;
  expected_outcome: string;
  reference: string;
}

export interface KnowledgeReference {
  id?: string;
  document_id?: number;
  item_id?: number;
  chunk_id?: number;
  title: string;
  category: string;
  score: number;
  snippet: string;
}

export interface ChatMessageItem {
  id: number;
  session_id: number;
  role: string;
  content: string;
  references: KnowledgeReference[];
  strategy_items: StrategyItem[];
  tokens_used: number;
  feedback: string;
  degraded: boolean;
  created_at: string;
}

export interface ChatSessionItem {
  id: number;
  title: string;
  customer_id: number | null;
  customer_name: string;
  scenario: string;
  message_count: number;
  last_message: string;
  created_at: string;
  updated_at: string;
}

export interface ChatSessionDetail extends ChatSessionItem {
  system_prompt: string;
  messages: ChatMessageItem[];
}

export interface ChatSessionListResponse {
  items: ChatSessionItem[];
  total: number;
}

export interface ChatTurnResponse {
  session_id: number;
  message: ChatMessageItem | null;
  assessment: AssessmentResponse | null;
  trend: AssessmentTrendResponse | null;
  strategy_items: StrategyItem[];
  references: KnowledgeReference[];
  degraded: boolean;
  tokens_used: number;
  latency_ms: number;
  warnings: string[];
  error: string;
}

export interface LLMStatusResponse {
  available: boolean;
  degraded: boolean;
  provider: string;
  model: string;
  reason: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_available: boolean;
  prompt_version: string;
  scenarios: string[];
}

export interface KnowledgeItemResponse {
  id: number;
  document_id: number;
  title: string;
  category: string;
  tags: string[];
  summary: string;
  storage: string;
  status: string; // canonical / proposed
  adoption_count: number;
  hit_count: number;
  chunk_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeItemListResponse {
  items: KnowledgeItemResponse[];
  total: number;
}

export interface KnowledgeSearchResult {
  document_id: number;
  chunk_index: number;
  item_id: number;
  item_title: string;
  category: string;
  content: string;
  score: number;
}

export interface KnowledgeSearchResponse {
  query: string;
  results: KnowledgeSearchResult[];
}

export interface KnowledgeStatusResponse {
  vector_store: string;
  count: number;
  embedding_available: boolean;
  reranker: string;
  categories: string[];
}

export interface ChatEvent {
  type: string;
  data: Record<string, unknown>;
}
