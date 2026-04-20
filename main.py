"""
=====================================================
 main.py — 整个系统的 CLI（命令行）入口
=====================================================
本系统是一个「端到端文档理解与模板填表系统」，其完整工作流程为：
  1. 解析多种格式的源文档（txt/md/docx/xlsx）
  2. 将文档切分成 Chunk 并建立向量索引
  3. 根据用户查询 / 字段定义，通过向量检索定位相关片段
  4. 使用规则/NER + LLM 两条路线抽取候选值
  5. 对多个候选值做融合决策
  6. 将最终结果回填到 Word/Excel 模板中

本文件只负责解析命令行参数，然后委托给 Pipeline 类执行。
提供四种子命令：
  - ingest : 仅做文档解析 + 向量化，不抽取不填表
  - query  : 解析 + 检索 + 抽取，返回抽取结果 JSON
  - fill/run : 解析 + 检索 + 抽取 + 填表，输出填好的模板文件
  - serve  : 启动 HTTP 服务（先上传文档解析，再逐个上传模板填写）
"""

import argparse
import json
import sys

from app.logging_config import setup_logging   # 日志初始化
from app.pipeline import Pipeline               # 核心流水线
from app.settings import Settings               # 配置加载器


def main():
    """CLI 主入口：解析命令行参数，根据子命令调用 Pipeline 对应方法。"""
    parser = argparse.ArgumentParser(description="端到端文档理解与模板填表系统")
    subparsers = parser.add_subparsers(dest="command", required=True, help="可用命令")

    # ---- 子命令 1：ingest（文档注入） ----
    ingest_parser = subparsers.add_parser("ingest", help="文档注入模式")
    ingest_parser.add_argument("--dir", required=True, help="输入文档目录")

    # ---- 子命令 2：query（字段查询抽取） ----
    query_parser = subparsers.add_parser("query", help="文档查询模式")
    query_parser.add_argument("--dir", required=True, help="输入文档目录")
    query_parser.add_argument("--query", help="用户查询或字段抽取需求")
    query_parser.add_argument("--query-file", help="需求文件路径")
    query_parser.add_argument("--field-specs", help="FieldSpec YAML/JSON 配置文件")
    query_parser.add_argument("--fields", nargs="*", help="需要抽取的字段列表")

    # ---- 子命令 3：fill / run（模板填表） ----
    fill_parser = subparsers.add_parser("fill", aliases=["run"], help="模板填表模式")
    fill_parser.add_argument("--dir", required=True, help="输入文档目录")
    fill_parser.add_argument("--template", required=True, help="模板文件路径")
    fill_parser.add_argument("--query", help="用户查询或字段抽取需求")
    fill_parser.add_argument("--query-file", help="需求文件路径")
    fill_parser.add_argument("--output", help="输出文件路径")
    fill_parser.add_argument("--field-specs", help="FieldSpec YAML/JSON 配置文件")

    # ---- 子命令 4：serve（启动 HTTP 服务） ----
    serve_parser = subparsers.add_parser("serve", help="启动 HTTP 服务（先上传文档，再逐个上传模板填写）")
    serve_parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    serve_parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")

    args = parser.parse_args()

    # ---- serve 模式单独处理（不需要提前创建 Pipeline） ----
    if args.command == "serve":
        import uvicorn
        from app.api.server import app as fastapi_app
        print(f"启动 HTTP 服务: http://{args.host}:{args.port}")
        print("接口说明:")
        print("  POST /ingest  — 上传数据文档（多文件），解析建索引")
        print("  POST /fill    — 上传一个模板，立即返回已填文件")
        print("  GET  /status  — 查看当前状态")
        print("  POST /reset   — 清空数据，重新开始")
        print("  GET  /docs    — Swagger 交互文档")
        uvicorn.run(fastapi_app, host=args.host, port=args.port)
        return

    # 加载配置文件 config/config.yaml
    settings = Settings()
    # 初始化日志
    setup_logging(settings)
    # 创建核心流水线对象
    pipeline = Pipeline(settings)

    try:
        if args.command == "ingest":
            result = pipeline.ingest(args.dir)
            print(json.dumps([doc.to_dict() for doc in result], ensure_ascii=False, indent=2))

        elif args.command == "query":
            result = pipeline.query(
                query=args.query,
                query_file=args.query_file,
                directory=args.dir,
                fields=args.fields,
                field_specs_path=args.field_specs,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command in {"fill", "run"}:
            result = pipeline.run(
                directory=args.dir,
                template=args.template,
                query=args.query,
                query_file=args.query_file,
                output=args.output,
                field_specs_path=args.field_specs,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
