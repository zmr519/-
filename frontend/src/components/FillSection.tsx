import { useState } from "react";
import { Card, Upload, Button, Input, Space, message, Divider, Typography } from "antd";
import { FileAddOutlined, FileTextOutlined, SendOutlined } from "@ant-design/icons";
import type { UploadFile } from "antd";
import { CONFIG } from "../config";
import { fillTemplate } from "../api";
import type { LogEntry } from "../types";

const { TextArea } = Input;
const { Text } = Typography;

interface Props {
  onLog: (entry: LogEntry) => void;
}

/** 读取 txt 文件内容 */
function readTextFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsText(file, "utf-8");
  });
}

export default function FillSection({ onLog }: Props) {
  const [templateFile, setTemplateFile] = useState<UploadFile | null>(null);
  const [queryText, setQueryText] = useState("");
  const [queryFile, setQueryFile] = useState<UploadFile | null>(null);
  const [loading, setLoading] = useState(false);

  const handleFill = async () => {
    if (!templateFile?.originFileObj) {
      message.warning("请先上传模板文件");
      return;
    }

    // 拼接最终 query：txt 文件内容 + 手动输入
    let finalQuery = "";

    if (queryFile?.originFileObj) {
      try {
        const fileContent = await readTextFile(queryFile.originFileObj);
        finalQuery += fileContent.trim();
      } catch {
        message.error("读取需求文件失败");
        return;
      }
    }

    if (queryText.trim()) {
      if (finalQuery) finalQuery += "\n";
      finalQuery += queryText.trim();
    }

    if (!finalQuery) {
      message.warning("请上传需求文件或输入填写需求（至少填一项）");
      return;
    }

    setLoading(true);
    try {
      const { blob, filename } = await fillTemplate(
        templateFile.originFileObj,
        finalQuery
      );

      // 触发浏览器下载
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      message.success("模板填写完成，文件已开始下载");
      onLog({
        time: new Date().toLocaleTimeString(),
        type: "fill",
        message: `填写成功，已下载文件：${filename}`,
      });
    } catch (err: unknown) {
      const msg = extractError(err);
      message.error(msg);
      onLog({
        time: new Date().toLocaleTimeString(),
        type: "error",
        message: `填写失败：${msg}`,
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card title="模板填写">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* 模板文件上传 */}
        <Upload
          accept={CONFIG.TEMPLATE_ACCEPT}
          maxCount={1}
          fileList={templateFile ? [templateFile] : []}
          beforeUpload={() => false}
          onChange={({ fileList }) =>
            setTemplateFile(fileList.length > 0 ? fileList[0] : null)
          }
        >
          <Button icon={<FileAddOutlined />}>选择模板文件（.docx / .xlsx）</Button>
        </Upload>

        <Divider style={{ margin: "4px 0" }}>
          <Text type="secondary" style={{ fontSize: 13 }}>填写需求（以下两种方式至少选一种，也可同时使用）</Text>
        </Divider>

        {/* 方式一：上传 txt 需求文件 */}
        <Upload
          accept=".txt"
          maxCount={1}
          fileList={queryFile ? [queryFile] : []}
          beforeUpload={() => false}
          onChange={({ fileList }) =>
            setQueryFile(fileList.length > 0 ? fileList[0] : null)
          }
        >
          <Button icon={<FileTextOutlined />}>上传需求文件（.txt）</Button>
        </Upload>

        {/* 方式二：手动输入 */}
        <TextArea
          rows={3}
          placeholder="手动输入填写需求，例如：根据已导入的文档数据，自动填写模板中的所有字段"
          value={queryText}
          onChange={(e) => setQueryText(e.target.value)}
        />

        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={loading}
          onClick={handleFill}
          block
        >
          开始填写
        </Button>
      </Space>
    </Card>
  );
}

function extractError(err: unknown): string {
  if (
    typeof err === "object" &&
    err !== null &&
    "response" in err
  ) {
    const resp = (err as { response: { data?: Blob | { detail?: string } } }).response;
    if (resp.data instanceof Blob) {
      return "服务端返回错误，请检查是否已导入文档且模板格式正确";
    }
    if (typeof resp.data === "object" && resp.data !== null && "detail" in resp.data) {
      return (resp.data as { detail: string }).detail;
    }
  }
  if (err instanceof Error) return err.message;
  return "未知错误";
}
