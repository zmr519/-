import axios from "axios";
import { CONFIG } from "../config";

const client = axios.create({
  baseURL: CONFIG.API_BASE_URL,
  timeout: 600_000, // 文档处理可能较慢，给 10 分钟
});

export default client;
