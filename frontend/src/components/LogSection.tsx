import { Card, Timeline, Empty } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  FileTextOutlined,
  DeleteOutlined,
} from "@ant-design/icons";
import type { LogEntry } from "../types";

interface Props {
  logs: LogEntry[];
}

const iconMap = {
  ingest: <FileTextOutlined style={{ color: "#1677ff" }} />,
  fill: <CheckCircleOutlined style={{ color: "#52c41a" }} />,
  error: <CloseCircleOutlined style={{ color: "#ff4d4f" }} />,
  reset: <DeleteOutlined style={{ color: "#faad14" }} />,
};

export default function LogSection({ logs }: Props) {
  return (
    <Card title="操作日志">
      {logs.length === 0 ? (
        <Empty description="暂无操作记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Timeline
          items={logs.map((log, i) => ({
            key: i,
            dot: iconMap[log.type],
            children: (
              <span>
                <span style={{ color: "#999", marginRight: 8 }}>{log.time}</span>
                {log.message}
              </span>
            ),
          }))}
        />
      )}
    </Card>
  );
}
