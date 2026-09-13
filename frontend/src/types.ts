export type OpportunityStatus = 'NEW' | 'SCREENING' | 'DEAD' | 'WATCH' | 'NEAR' | 'REVIEW';
export type ConstraintKind = 'structural' | 'economic';
export type Actor = 'agent' | 'system' | 'human';

export interface DashboardStats {
  encountered: number;
  dead: number;
  watch: number;
  near: number;
  review: number;
  conditions_changed_7d: number;
  threshold_crossings_7d: number;
  human_interruptions_7d: number;
  open_diligence_requests: number;
  policy_version: string;
}

export interface WatchlistItem {
  opportunity_id: string;
  deal_number: number | null;
  display_name: string;
  status: OpportunityStatus;
  current_asking_price: number | null;
  max_viable_price: number | null;
  distance_pct: number | null;
  binding_constraints: string[];
  reason_summary: string | null;
  updated_at: string;
}

export interface TenantClaim {
  name: string;
  suite: string | null;
  sqft: number | null;
  annual_rent: number | null;
  lease_end: string | null;
  notes: string | null;
}

export interface ExpenseClaims {
  property_tax: number | null;
  insurance: number | null;
  repairs_maintenance: number | null;
  utilities: number | null;
  management: number | null;
  cam_other: number | null;
  total: number | null;
}

export interface WorkingValues {
  asking_price: number;
  gross_scheduled_income: number;
  other_income: number;
  stated_vacancy_pct: number;
  stated_expenses: ExpenseClaims;
  stated_noi: number | null;
  building_sqft: number | null;
  tenant_count: number | null;
  largest_tenant_pct: number | null;
  occupancy_pct: number | null;
  tenants: TenantClaim[];
  property_type: string | null;
  provenance: Record<string, string>;
  conflicts: string[];
}

export interface ExpenseLine {
  name: string;
  broker: number | null;
  normalized: number;
  basis: string;
}

export interface NormalizedEconomics {
  gross_potential_rent: number;
  other_income: number;
  vacancy_loss: number;
  effective_gross_income: number;
  expenses: ExpenseLine[];
  total_expenses: number;
  noi: number;
  broker_noi: number | null;
  broker_cap_rate: number | null;
  normalized_cap_rate: number;
  price_per_sqft: number | null;
  noi_per_sqft: number | null;
}

export interface FinancingResult {
  purchase_price: number;
  closing_costs: number;
  total_acquisition_cost: number;
  equity_deployed: number;
  loan_amount: number;
  ltv: number;
  interest_rate: number;
  amortization_years: number;
  monthly_debt_service: number;
  annual_debt_service: number;
  dscr: number;
  cash_flow_after_debt: number;
  cash_on_cash: number;
  year1_principal_paydown: number;
  immediate_capex?: number | null;
  all_in_basis?: number | null;
}

export interface StressResult {
  scenario: string;
  noi: number;
  dscr: number;
  cash_flow_after_debt: number;
  covers_debt: boolean;
}

export interface GateResult {
  gate: string;
  kind: ConstraintKind;
  description: string;
  comparator: '>=' | '<=' | '==' | 'between';
  threshold: number | null;
  actual: number | null;
  passed: boolean;
  price_dependent: boolean;
}

export interface ViabilityPath {
  variable: string;
  current_value: number;
  required_value: number;
  description: string;
}

export interface ViabilityFrontier {
  current_price: number;
  max_viable_price: number | null;
  distance_pct: number | null;
  binding_constraints: string[];
  paths: ViabilityPath[];
  structural_failures: string[];
}

export interface ComparisonRow {
  metric: string;
  broker: string | null;
  dealsieve: string;
  note: string | null;
}

export interface UnderwritingResult {
  run_id: string;
  opportunity_id: string;
  policy_version: string;
  created_at: string;
  trigger_event_id: string | null;
  inputs: WorkingValues;
  normalized: NormalizedEconomics;
  financing: FinancingResult;
  stress: StressResult[];
  gates: GateResult[];
  status: OpportunityStatus;
  viability: ViabilityFrontier;
  comparison: ComparisonRow[];
  failure_summary: string;
}

export interface Opportunity {
  opportunity_id: string;
  deal_number: number | null;
  property_id: string;
  display_name: string;
  status: OpportunityStatus;
  previous_status: OpportunityStatus | null;
  current_asking_price: number | null;
  working_values: WorkingValues | null;
  latest_run_id: string | null;
  viability: ViabilityFrontier | null;
  broker_email: string | null;
  broker_name: string | null;
  broker_property_ref: string | null;
  listing_url: string | null;
  reason_summary: string | null;
  human_attention_required: boolean;
  created_at: string;
  updated_at: string;
}

export interface Property {
  property_id: string;
  canonical_address: string;
  normalized_address: string;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  apn: string | null;
  building_sqft: number | null;
  property_type: string | null;
  created_at: string;
}

export type EventType = 'DEAL_DISCOVERED' | 'MESSAGE_RECEIVED' | 'DOCUMENT_ADDED' | 'CLAIMS_EXTRACTED' | 'ASKING_PRICE_CHANGED' | 'NOI_CHANGED' | 'RENT_ROLL_UPDATED' | 'FINANCING_CHANGED' | 'POLICY_CHANGED' | 'UNDERWRITING_COMPLETED' | 'STATUS_CHANGED' | 'SKEPTIC_REVIEW_COMPLETED' | 'HUMAN_NOTIFIED' | 'BROKER_DRAFT_CREATED' | 'HUMAN_APPROVED_DRAFT' | 'HUMAN_REJECTED_DRAFT' | 'BROKER_MESSAGE_SENT' | 'NOTE';

export interface OpportunityEvent {
  event_id: string;
  opportunity_id: string;
  seq: number | null;
  type: EventType;
  occurred_at: string;
  actor: Actor;
  source_message_id: string | null;
  summary: string;
  payload: Record<string, any>;
}

export interface Evidence {
  evidence_id: string;
  field: string;
  value: any;
  source_document: string;
  location: string | null;
  quote: string | null;
  source_timestamp: string | null;
  confidence: number;
  observed_by: string;
}

export interface SkepticConcern {
  topic: string;
  severity: 'low' | 'medium' | 'high';
  why_it_matters: string;
  evidence_status: 'missing' | 'weak' | 'contradicted' | 'unverified';
  question_for_broker: string | null;
}

export interface SkepticReport {
  report_id: string;
  opportunity_id: string;
  run_id: string;
  verdict: 'proceed' | 'proceed_with_questions' | 'reject';
  summary: string;
  concerns: SkepticConcern[];
  created_at: string;
}

export type OutboundKind = 'information_request' | 'follow_up' | 'credit_request' | 'offer' | 'other';

export type MemoryKind = 'decision' | 'broker' | 'alert' | 'note';

export interface MemoryEvent {
  memory_event_id: string;
  namespace: string;
  kind: MemoryKind;
  actor: string;
  opportunity_id: string | null;
  deal_number: number | null;
  broker_email: string | null;
  text: string;
  payload: Record<string, any>;
  created_at: string;
  store: string | null;
  external_id: string | null;
}

export interface MemoryHit {
  memory_event_id: string;
  namespace: string;
  kind: MemoryKind;
  text: string;
  score: number;
  created_at: string;
  payload: Record<string, any>;
}

export interface OutboundDraft {
  draft_id: string;
  opportunity_id: string;
  kind: OutboundKind;
  to_email: string | null;
  subject: string;
  body: string;
  questions: string[];
  request_ids: string[];
  requires_approval: boolean;
  status: 'pending' | 'approved' | 'rejected' | 'sent';
  in_reply_to_message_id: string | null;
  delivery_ref: string | null;
  created_at: string;
  decided_at: string | null;
  sent_at: string | null;
}

export interface NotificationAction {
  label: string;
  action: 'review' | 'draft_questions' | 'ignore' | 'approve' | 'edit' | 'reject';
}

export interface Notification {
  notification_id: string;
  opportunity_id: string;
  kind: 'threshold_crossed' | 'fell_below_threshold' | 'diligence_stalled' | 'structural_dead' | 'status_update' | 'draft_pending';
  channel: string;
  title: string;
  body: string;
  actions: NotificationAction[];
  created_at: string;
  delivered: boolean;
  delivery_ref: string | null;
}

export interface DiligenceRequest {
  request_id: string;
  opportunity_id: string;
  topic: string;
  question: string;
  category: 'document' | 'disclosure' | 'clarification';
  source_concern: string | null;
  status: 'draft' | 'sent' | 'answered' | 'overdue' | 'stalled' | 'withdrawn';
  created_at: string;
  sent_at: string | null;
  due_at: string | null;
  last_follow_up_at: string | null;
  follow_up_count: number;
  answered_at: string | null;
  answer_summary: string | null;
  answer_evidence_ids: string[];
  answered_by_document: string | null;
}

export interface CapexItem {
  item: string;
  low: number;
  high: number;
  urgency: 'immediate' | 'near_term' | 'deferred';
  source_document: string;
  location: string | null;
  evidence_id: string | null;
}

export interface DocumentFinding {
  topic: string;
  value: string;
  detail: string | null;
  severity: 'info' | 'low' | 'medium' | 'high';
  confidence: number;
  page: number | null;
  image_ref: string | null;
}

export interface RequestAnswer {
  request_topic: string;
  answer: string;
  resolves: boolean;
}

export interface DocumentAnalysis {
  analysis_id: string;
  opportunity_id: string;
  message_id: string;
  filename: string;
  document_type: 'inspection_report' | 'roof_report' | 'phase_i' | 'cam_statement' | 'rent_roll' | 'lease' | 'offering_memorandum' | 'other';
  summary: string;
  findings: DocumentFinding[];
  answers: RequestAnswer[];
  capex_items: CapexItem[];
  red_flags: string[];
  images_reviewed: number;
  image_paths: string[];
  text_chars: number;
  model_backend: string | null;
  created_at: string;
}

export interface Attachment {
  filename: string;
  content_type: string;
  sha256?: string;
  size_bytes: number;
  text?: string | null;
  stored_path?: string | null;
  image_paths?: string[];
}

export interface InboundMessage {
  message_id: string;
  channel: string;
  received_at: string;
  sender: string | null;
  sender_name: string | null;
  subject: string | null;
  body_text: string;
  attachments: Attachment[];
  urls: string[];
  in_reply_to: string | null;
  thread_id: string | null;
  raw_ref: string | null;
}

export interface OpportunityDetail {
  opportunity: Opportunity;
  property: Property;
  latest_run: UnderwritingResult | null;
  runs: UnderwritingResult[];
  events: OpportunityEvent[];
  evidence: Evidence[];
  skeptic_reports: SkepticReport[];
  drafts: OutboundDraft[];
  notifications: Notification[];
  diligence_requests: DiligenceRequest[];
  document_analyses: DocumentAnalysis[];
  inbound_messages: InboundMessage[];
  memories: MemoryHit[];
}
