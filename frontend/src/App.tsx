import { useState, useCallback } from "react";
import { Layout, Row, Col, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import Header from "./components/Header";
import IngestSection from "./components/IngestSection";
import StatusSection from "./components/StatusSection";
import FillSection from "./components/FillSection";
import LogSection from "./components/LogSection";
import ResetSection from "./components/ResetSection";
import type { LogEntry } from "./types";
import "./App.css";

const { Content, Footer } = Layout;

export default function App() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [statusKey, setStatusKey] = useState(0);

  const addLog = useCallback((entry: LogEntry) => {
    setLogs((prev) => [entry, ...prev]);
  }, []);

  const refreshStatus = useCallback(() => {
    setStatusKey((k) => k + 1);
  }, []);

  const handleResetDone = useCallback(() => {
    refreshStatus();
  }, [refreshStatus]);

  return (
    <ConfigProvider locale={zhCN}>
      <Layout className="app-layout">
        <Content className="app-content">
          <Header />

          <div className="app-sections">
            <Row gutter={[24, 24]}>
              {/* 左列：操作区 */}
              <Col xs={24} lg={14}>
                <Row gutter={[0, 24]}>
                  <Col span={24}>
                    <IngestSection
                      onLog={addLog}
                      onIngestDone={refreshStatus}
                    />
                  </Col>
                  <Col span={24}>
                    <FillSection onLog={addLog} />
                  </Col>
                </Row>
              </Col>

              {/* 右列：状态与日志 */}
              <Col xs={24} lg={10}>
                <Row gutter={[0, 24]}>
                  <Col span={24}>
                    <StatusSection refreshKey={statusKey} />
                  </Col>
                  <Col span={24}>
                    <LogSection logs={logs} />
                  </Col>
                  <Col span={24}>
                    <ResetSection
                      onLog={addLog}
                      onResetDone={handleResetDone}
                    />
                  </Col>
                </Row>
              </Col>
            </Row>
          </div>
        </Content>

        <Footer style={{ textAlign: "center", color: "#999" }}>
          文档理解与多源数据融合系统 Demo
        </Footer>
      </Layout>
    </ConfigProvider>
  );
}
