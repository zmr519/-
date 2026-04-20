import { Typography } from "antd";
import { CONFIG } from "../config";

const { Title, Text } = Typography;

export default function Header() {
  return (
    <div style={{ textAlign: "center", padding: "32px 0 16px" }}>
      <Title level={2} style={{ marginBottom: 8 }}>
        {CONFIG.APP_TITLE}
      </Title>
      <Text type="secondary" style={{ fontSize: 16 }}>
        {CONFIG.APP_SUBTITLE}
      </Text>
    </div>
  );
}
