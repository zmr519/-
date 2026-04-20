import os
from llama_cpp import Llama
from app.exceptions import ModelError

class LocalLLM:
    """
    本地大模型封装（单例）
    """

    _instance = None

    def __init__(self, model_path, n_ctx=4096):
        if not os.path.exists(model_path):
            raise ModelError(f"模型文件不存在: {model_path}")

        self.model = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            verbose=False
        )

    @classmethod
    def get_instance(cls, settings):
        if cls._instance is None:
            cls._instance = LocalLLM(
                settings.get("llm.model_path"),
                settings.get("llm.n_ctx", 4096)
            )
        return cls._instance

    def generate(self, prompt, max_tokens=512):
        try:
            output = self.model(
                prompt,
                max_tokens=max_tokens,
                temperature=0.2
            )
            return output["choices"][0]["text"]
        except Exception as e:
            raise ModelError(f"模型推理失败: {e}")