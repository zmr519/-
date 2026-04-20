/** /status 响应 */
export interface StatusResponse {
  ingested: boolean;
  document_count: number;
  chunk_count: number;
  documents: string[];
}

/** /ingest 响应 */
export interface IngestResponse {
  document_count: number;
  chunk_count: number;
  documents: string[];
  message: string;
}

/** /reset 响应 */
export interface ResetResponse {
  message: string;
}

/** 日志条目 */
export interface LogEntry {
  time: string;
  type: "ingest" | "fill" | "reset" | "error";
  message: string;
}
