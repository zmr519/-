"""
server.py — FastAPI HTTP 服务

流程：
  1. POST /ingest  上传数据文档（多文件），系统解析并建立向量索引
  2. POST /fill    上传一个模板文件 + 需求文本，返回已填写的文件
     可多次调用，每次上传一个模板，立即返回已填文件
  3. GET  /status  查看当前状态（是否已 ingest、文档列表）
  4. POST /reset   清空已解析数据，重新开始

启动：
  uvicorn app.api.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.logging_config import setup_logging
from app.pipeline import Pipeline
from app.settings import Settings

logger = logging.getLogger(__name__)

# ---------- 全局单例 ----------
_settings = Settings()
setup_logging(_settings)
_pipeline = Pipeline(_settings)

app = FastAPI(
    title="文档理解与模板填表系统",
    description="先上传数据文档 ingest，再逐个上传模板 fill",
    version="1.0.0",
)

# ingest 阶段保存上传文件的临时目录（服务重启或 reset 时清理）
_ingest_tmp_dir: str | None = None

SUPPORTED_SUFFIXES = {".txt", ".md", ".docx", ".xlsx"}


def _cleanup_ingest_tmp():
    """清理 ingest 临时目录。"""
    global _ingest_tmp_dir
    if _ingest_tmp_dir and Path(_ingest_tmp_dir).exists():
        shutil.rmtree(_ingest_tmp_dir, ignore_errors=True)
    _ingest_tmp_dir = None


# ---------- 接口 ----------

@app.get("/status")
def get_status():
    """查看当前 Pipeline 状态。"""
    return {
        "ingested": bool(_pipeline.documents),
        "document_count": len(_pipeline.documents),
        "chunk_count": len(_pipeline.chunks),
        "documents": [doc.name for doc in _pipeline.documents],
    }


@app.post("/ingest")
async def ingest(files: list[UploadFile] = File(..., description="数据文档（支持 txt/md/docx/xlsx，可多选）")):
    """
    上传数据文档，解析并建立向量索引。
    调用一次即可，之后可反复调用 /fill 上传不同模板。
    再次调用 /ingest 会清空旧数据重新解析。
    """
    global _ingest_tmp_dir

    # 校验文件
    if not files:
        raise HTTPException(status_code=400, detail="请上传至少一个数据文档。")

    invalid = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in SUPPORTED_SUFFIXES]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {invalid}，仅支持 {SUPPORTED_SUFFIXES}",
        )

    # 清理旧的临时目录和旧数据
    _cleanup_ingest_tmp()
    _pipeline.reset()

    # 保存上传文件到临时目录
    _ingest_tmp_dir = tempfile.mkdtemp(prefix="ingest_")
    for f in files:
        content = await f.read()
        dst = Path(_ingest_tmp_dir) / (f.filename or "unknown")
        dst.write_bytes(content)
        logger.info("已保存上传文件: %s (%d bytes)", dst.name, len(content))

    # 执行 ingest
    try:
        docs = _pipeline.ingest(_ingest_tmp_dir)
        return {
            "document_count": len(docs),
            "chunk_count": len(_pipeline.chunks),
            "documents": [doc.name for doc in docs],
            "message": f"成功解析 {len(docs)} 份文档，共 {len(_pipeline.chunks)} 个 chunk。现在可以上传模板进行填写。",
        }
    except Exception as exc:
        _cleanup_ingest_tmp()
        raise HTTPException(status_code=500, detail=f"解析失败: {exc}") from exc


@app.post("/fill")
async def fill(
    template: UploadFile = File(..., description="模板文件（.xlsx 或 .docx）"),
    query: str = Form("", description="用户需求文本（如：智能填表，将文件内容填入模板中）"),
    query_file: str = Form("", description="需求文件路径（服务器上已存在的文件，与 query 二选一）"),
):
    """
    上传一个模板文件，基于已解析的数据立即填写。
    返回已填写的文件，直接下载。

    必须先调用 /ingest 上传并解析数据文档。
    每次只上传一个模板，填写完成后再上传下一个。
    """
    # 检查是否已 ingest
    if not _pipeline.documents:
        raise HTTPException(
            status_code=400,
            detail="尚未上传并解析数据文档，请先调用 POST /ingest 上传数据文件。",
        )

    # 检查需求
    query_text = query.strip() if query else ""
    query_file_path = query_file.strip() if query_file else ""
    if not query_text and not query_file_path:
        raise HTTPException(
            status_code=400,
            detail="请提供 query（需求文本）或 query_file（需求文件路径）。",
        )

    # 校验模板格式
    suffix = Path(template.filename or "template.xlsx").suffix.lower()
    if suffix not in {".xlsx", ".docx"}:
        raise HTTPException(status_code=400, detail=f"模板格式不支持: {suffix}，仅支持 .xlsx 和 .docx")

    # 保存上传的模板到临时文件
    tmp_dir = tempfile.mkdtemp(prefix="fill_")
    # 保留原始文件名，方便输出文件命名
    safe_name = template.filename or f"template{suffix}"
    tmp_template = Path(tmp_dir) / safe_name
    try:
        content = await template.read()
        tmp_template.write_bytes(content)

        # 填写
        result = _pipeline.fill(
            template=str(tmp_template),
            query=query_text or None,
            query_file=query_file_path or None,
            directory=None,       # 数据已在 ingest 阶段加载
            output=None,
            field_specs_path=None,
        )

        output_path = result.get("output_path", "")
        if not output_path or not Path(output_path).exists():
            raise HTTPException(status_code=500, detail="填写完成但输出文件不存在")

        # 生成下载文件名
        stem = Path(safe_name).stem
        out_suffix = Path(output_path).suffix
        download_name = f"{stem}_已填{out_suffix}"

        return FileResponse(
            path=output_path,
            filename=download_name,
            media_type="application/octet-stream",
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("填写失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"填写失败: {exc}") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/reset")
def reset():
    """清空已解析数据和临时文件，重新开始。"""
    _cleanup_ingest_tmp()
    _pipeline.reset()
    return {"message": "已清空。请重新上传数据文档（POST /ingest）。"}
