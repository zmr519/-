import { useState } from "react";
import { Card, Upload, Button, message, Space } from "antd";
import { UploadOutlined, CloudUploadOutlined } from "@ant-design/icons";
import type { UploadFile } from "antd";
import { CONFIG } from "../config";
import { ingestFiles } from "../api";
import type { LogEntry } from "../types";

interface Props {
  onLog: (entry: LogEntry) => void;
  onIngestDone: () => void;
}

export default function IngestSection({ onLog, onIngestDone }: Props) {
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [loading, setLoading] = useState(false);

  const handleIngest = async () => {
    if (fileList.length === 0) {
      message.warning("请先选择要上传的文档");
      return;
    }

    const files = fileList
      .map((f) => f.originFileObj)
      .filter((f): f is File => !!f);

    setLoading(true);
    try {
      const res = await ingestFiles(files);
      message.success(res.message || "文档导入成功");
      onLog({
        time: new Date().toLocaleTimeString(),
        type: "ingest",
        message: `导入成功：${res.document_count} 个文档，${res.chunk_count} 个片段`,
      });
      onIngestDone();
      setFileList([]);
    } catch (err: unknown) {
      const msg = extractError(err);
      message.error(msg);
      onLog({ time: new Date().toLocaleTimeString(), type: "error", message: `导入失败：${msg}` });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card
      title="原始文档上传"
      extra={
        <Button
          type="primary"
          icon={<CloudUploadOutlined />}
          loading={loading}
          onClick={handleIngest}
        >
          开始导入
        </Button>
      }
    >
      <Upload.Dragger
        multiple
        accept={CONFIG.INGEST_ACCEPT}
        fileList={fileList}
        beforeUpload={() => false}
        onChange={({ fileList: fl }) => setFileList(fl)}
      >
        <Space direction="vertical" size={4}>
          <UploadOutlined style={{ fontSize: 32, color: "#1677ff" }} />
          <p>点击或拖拽上传原始文档</p>
          <p style={{ color: "#999", fontSize: 12 }}>
            支持 .docx / .xlsx / .md / .txt，可多选
          </p>
        </Space>
      </Upload.Dragger>
    </Card>
  );
}

function extractError(err: unknown): string {
  if (
    typeof err === "object" &&
    err !== null &&
    "response" in err &&
    typeof (err as Record<string, unknown>).response === "object"
  ) {
    const resp = (err as { response: { data?: { detail?: string } } }).response;
    if (resp.data?.detail) return resp.data.detail;
  }
  if (err instanceof Error) return err.message;
  return "未知错误";
}
