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
export interface DisplayDocument {
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
