import { Card, Button, Popconfirm, message } from "antd";
import { ClearOutlined } from "@ant-design/icons";
import { useState } from "react";
import { resetSystem } from "../api";
import type { LogEntry } from "../types";

interface Props {
  onLog: (entry: LogEntry) => void;
  onResetDone: () => void;
}

export default function ResetSection({ onLog, onResetDone }: Props) {
  const [loading, setLoading] = useState(false);

  const handleReset = async () => {
    setLoading(true);
    try {
      const res = await resetSystem();
      message.success(res.message || "系统已重置");
      onLog({
        time: new Date().toLocaleTimeString(),
        type: "reset",
        message: "系统已重置，所有数据已清空",
      });
      onResetDone();
    } catch {
      message.error("重置失败");
      onLog({
        time: new Date().toLocaleTimeString(),
        type: "error",
        message: "系统重置失败",
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <Popconfirm
        title="确认重置系统？"
        description="重置将清空所有已导入的文档和知识库数据，此操作不可撤销。"
        onConfirm={handleReset}
        okText="确认重置"
        cancelText="取消"
        okButtonProps={{ danger: true }}
      >
        <Button danger icon={<ClearOutlined />} loading={loading} block>
          重置系统
        </Button>
      </Popconfirm>
    </Card>
  );
}
