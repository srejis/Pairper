export type PaperStatus = 'registered' | 'processing' | 'complete' | 'review_required' | 'failed';
export interface Paper {
  id: string; title: string; filename: string; page_count: number;
  status: PaperStatus; has_view: boolean; current_view_id: string | null;
  created_at?: string; original_available?: boolean; error?: string | null;
}
export type BlockType = 'title' | 'authors' | 'heading' | 'paragraph' | 'list' | 'equation' |
  'figure' | 'table' | 'caption' | 'footnote' | 'reference' | 'source_region';
export interface TextRun { kind: 'text'; prefix: string; text: string }
export interface MathRun { kind: 'math'; prefix: string; latex: string;
  source_verified: boolean; image_url: string }
export type Run = TextRun | MathRun;
export interface Cell {
  row: number; column: number; rowspan: number; colspan: number; header: boolean; runs: Run[];
}
export interface BlockContent {
  runs: Run[]; items: Run[][]; cells: Cell[]; level: number; ordered: boolean;
  source_verified: boolean; image_url: string | null; latex: string | null;
  number: string; caption_id: string | null;
}
export interface DocumentBlock {
  source_regions?: SourceRegionInfo[]; source_error?: string | null;
  id: string; type: BlockType; order: number; validation_status: string; content: BlockContent;
  source_trace: { id: string; page: number; text: string; bbox: number[] }[];
}
export interface SourceRegionInfo { id: string; page: number; bbox: number[] }
export interface SourceComparison { image_url: string; page: number; region_id: string }
export interface DisplayDocument {
  display_schema_version: '1.0'; source_schema_version: '1.0';
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
  sourceRegion(paperId: string, viewId: string, blockId: string, regionId: string): Promise<SourceComparison>;
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
