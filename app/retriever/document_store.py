"""
document_store.py — 文档仓库

简单的文档加载类，遍历目录下的文件，调用 parse_file 解析，
再调用 chunk_document_blocks 切分，统一存储到 self.docs 和 self.chunks。
注意：实际项目中 Pipeline 直接实现了类似功能，本类主要用于简单场景。
"""

from app.chunker.simple_chunker import chunk_document_blocks
from app.parser.dispatcher import parse_file

class DocumentStore:
    """加载目录下的所有文档，解析并切分为 Chunk。"""

    def __init__(self):
        self.docs = []
        self.chunks = []

    def load_dir(self, directory):
        import os

        for file in os.listdir(directory):
            path = os.path.join(directory, file)

            doc = parse_file(path)
            self.docs.append(doc)

            chunks = chunk_document_blocks(doc.chunks)
            self.chunks.extend(chunks)

    def get_chunks(self):
        return self.chunks
