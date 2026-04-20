/** 统一配置 */
export const CONFIG = {
  /** 后端 API 基础路径（开发环境通过 vite proxy 转发） */
  API_BASE_URL: "/api",

  /** 允许上传的原始文档类型 */
  INGEST_ACCEPT: ".txt,.md,.docx,.xlsx",

  /** 允许上传的模板文件类型 */
  TEMPLATE_ACCEPT: ".docx,.xlsx",

  /** 项目名称 */
  APP_TITLE: "基于大语言模型的文档理解与多源数据融合系统",

  /** 项目简介 */
  APP_SUBTITLE:
    "上传原始文档构建知识库，再上传模板文件自动填写，实现端到端智能文档处理",
} as const;
