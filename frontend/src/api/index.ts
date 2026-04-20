import client from "./client";
import type { StatusResponse, IngestResponse, ResetResponse } from "../types";

/** 获取系统状态 */
export async function fetchStatus(): Promise<StatusResponse> {
  const res = await client.get<StatusResponse>("/status");
  return res.data;
}

/** 上传原始文档并 ingest */
export async function ingestFiles(files: File[]): Promise<IngestResponse> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  const res = await client.post<IngestResponse>("/ingest", form);
  return res.data;
}

/** 上传模板并填写，返回 Blob 及文件名 */
export async function fillTemplate(
  template: File,
  query: string
): Promise<{ blob: Blob; filename: string }> {
  const form = new FormData();
  form.append("template", template);
  form.append("query", query);

  const res = await client.post("/fill", form, {
    responseType: "blob",
  });

  // 从 Content-Disposition 中提取文件名
  const disposition = res.headers["content-disposition"] as string | undefined;
  let filename = "result";
  if (disposition) {
    // 优先取 filename*=UTF-8'' 编码的文件名
    const utf8Match = disposition.match(/filename\*=UTF-8''(.+)/i);
    if (utf8Match) {
      filename = decodeURIComponent(utf8Match[1]);
    } else {
      const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
      if (plainMatch) {
        filename = decodeURIComponent(plainMatch[1]);
      }
    }
  }

  return { blob: res.data as Blob, filename };
}

/** 重置系统 */
export async function resetSystem(): Promise<ResetResponse> {
  const res = await client.post<ResetResponse>("/reset");
  return res.data;
}
