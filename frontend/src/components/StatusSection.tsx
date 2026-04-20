import { useEffect, useState, useCallback } from "react";
import { Card, Descriptions, Button, Tag, Spin, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { fetchStatus } from "../api";
import type { StatusResponse } from "../types";

interface Props {
  refreshKey: number;
}

export default function StatusSection({ refreshKey }: Props) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchStatus();
      setStatus(data);
    } catch {
      message.error("获取系统状态失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  return (
    <Card
      title="系统状态"
      extra={
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
          刷新
        </Button>
      }
    >
      <Spin spinning={loading}>
        {status ? (
          <Descriptions column={2} bordered size="small">
            <Descriptions.Item label="知识库状态">
              {status.ingested ? (
                <Tag color="green">已就绪</Tag>
              ) : (
                <Tag color="orange">未导入</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="文档数量">
              {status.document_count}
            </Descriptions.Item>
            <Descriptions.Item label="分片数量">
              {status.chunk_count}
            </Descriptions.Item>
            <Descriptions.Item label="文档列表">
              {status.documents.length > 0
                ? status.documents.join("、")
                : "-"}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <p style={{ color: "#999" }}>暂无数据</p>
        )}
      </Spin>
    </Card>
  );
}
