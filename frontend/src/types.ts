export type PaperStatus = 'registered' | 'processing' | 'complete' | 'review_required' | 'failed';
export interface Paper {
  id: string; title: string; filename: string; page_count: number;
  status: PaperStatus; has_view: boolean; current_view_id: string | null;
  created_at?: string; original_available?: boolean; error?: string | null;
}
export type BlockType = 'title' | 'authors' | 'heading' | 'paragraph' | 'list' | 'equation' |
  'figure' | 'table' | 'caption' | 'footnote' | 'reference' | 'source_region' | 'code' | 'group';
export type GroupRole = 'section' | 'front_matter' | 'theorem' | 'definition' | 'lemma' |
  'fact' | 'remark' | 'proof' | 'prompt' | 'example' | 'checklist' | 'checklist_item' |
  'algorithm' | 'figure_group' | 'table_group' | 'list_item';
export interface TextRun { kind: 'text'; prefix: string; text: string; superscript?: boolean;
  bold?: boolean; italic?: boolean; underline?: boolean }
export interface MathRun { kind: 'math'; prefix: string; latex: string;
  source_verified: boolean; image_url: string }
export type Run = TextRun | MathRun;
export interface Cell {
  row: number; column: number; rowspan: number; colspan: number; header: boolean; runs: Run[];
}
export interface BlockContent {
  translation_labels?: { source: string; target: string }[];
  runs: Run[]; items: Run[][]; cells: Cell[]; level: number; ordered: boolean;
  code_lines?: { indent: number; runs: Run[] }[];
  source_verified: boolean; image_url: string | null; latex: string | null;
  number: string; caption_id: string | null;
  group_role?: GroupRole | null; language?: string | null;
}
export interface DocumentBlock {
  id: string; type: BlockType; order: number; validation_status: string; content: BlockContent;
  parent_id?: string | null;
  relations?: { kind: 'caption_of' | 'proof_of' | 'footnote_for' | 'refers_to'; target_id: string }[];
}
export interface SourceIdentity {source_pdf_sha256: string; view_id: string; view_sha256: string; source_snapshot_sha256: string}
export interface DisplayDocument {
  source_identity?: SourceIdentity;
  translation?: { status: 'draft' | 'published'; complete: boolean; missing_unit_count: number };
  display_schema_version: '1.0'; source_schema_version: '1.0' | '1.1';
  paper_id: string; view_id: string; blocks: DocumentBlock[];
  normalization_audit: unknown[];
}
export interface ConnectionStatus {
  state: string; message: string; checked_at: string | null; last_healthy_at: string | null;
  healthy: boolean; ready: boolean; admin_url: string | null; chatgpt_verified: boolean;
  last_tunnel_tool_at?: string; last_tunnel_tool?: string;
  changed_at?: string; retry_in_seconds?: number | null;
}
export interface PairperApi {
  getExplanations?(paperId: string, translationSessionId?: string): Promise<ExplanationBundle>;
  listTranslations?(paperId: string): Promise<{ session_id: string; status: 'draft' | 'published'; revision: number }[]>;
  getTranslationDocument?(sessionId: string): Promise<DisplayDocument>;
  copyUpdateRequest(paperId: string): Promise<{copied: boolean}>;
  autostartStatus(): Promise<{ enabled: boolean }>;
  setAutostart(enabled: boolean): Promise<{ enabled: boolean }>;
  listPapers(): Promise<Paper[]>;
  getDocument(id: string): Promise<DisplayDocument>;
  connectionStatus(): Promise<ConnectionStatus>;
  importPdfs(): Promise<{ imported: number; failed: number }>;
  openDiagnostics(): Promise<ConnectionStatus>;
  reconnectTunnel?(): Promise<ConnectionStatus>;
  onConnectionChanged?(listener: () => void): () => void;
  onLibraryChanged(listener: () => void): () => void;
}
export type ExplanationSegment = {kind: 'text'; text: string} | {kind: 'math'; latex: string};
export interface ExplanationAnchor {
  block_id: string; field: string; run_index: number | null; start: number | null; end: number | null;
  source_text: string;
}
export interface ExplanationTarget extends ExplanationAnchor {
  id: string; kind: 'term' | 'math'; display: boolean; concept_key?: string;
  anchor_type?: 'text_math'; anchor_text?: string;
  text_parts?: {run_index: number; start: number; end: number; text: string}[];
  localized?: (Omit<ExplanationAnchor, 'source_text'> & {text: string})[];
}
export type Explanation = {target_id: string; kind: 'term'; category: string; description: ExplanationSegment[]}
  | {target_id: string; kind: 'math'; math_role?: 'symbol' | 'formula' | 'literal' | 'notation'; exclusion_reason?: string; symbols: {symbol: string; meaning: string}[];
     meaning: ExplanationSegment[]; process: ExplanationSegment[]};
export interface ExplanationBundle {
  paper_id: string; source_identity: SourceIdentity; fingerprint: string;
  targets: ExplanationTarget[]; explanations: Explanation[]; revision: number; status: string;
}
