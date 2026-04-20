# WSL + vLLM(Qwen2.5-3B) 部署说明

默认按下面假设：

- Ubuntu 22.04 运行在 WSL2
- NVIDIA 显卡可被 WSL 识别
- 模型使用 `Qwen/Qwen2.5-3B-Instruct`
- 本项目和 vLLM 都运行在同一个 WSL 环境里

## 1. 创建 Python 环境

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip build-essential git

python3.11 -m venv ~/venvs/docfill
source ~/venvs/docfill/bin/activate
python -m pip install --upgrade pip wheel setuptools
```

## 2. 安装项目依赖

```bash
cd /mnt/d/AAA/program1
pip install -r requirements.txt
```

如果你要切到 `BGE`：

```bash
pip install FlagEmbedding
```

如果你要切到 `FAISS`：

```bash
pip install faiss-cpu
```

## 3. 安装并启动 vLLM

```bash
pip install vllm
```

启动服务：

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-3B-Instruct \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype auto \
  --max-model-len 8192
```

如果你已经有本地模型目录，也可以把 `--model` 改成本地路径，例如：

```bash
python -m vllm.entrypoints.openai.api_server \
  --model /data/models/Qwen2.5-3B-Instruct \
  --host 0.0.0.0 \
  --port 8000
```

## 4. 修改项目配置

编辑 `config/config.yaml`：

```yaml
llm:
  url: http://localhost:8000/v1/chat/completions
  model: Qwen/Qwen2.5-3B-Instruct
  api_key: ""
```

如果你的 vLLM 启动时加了 `--api-key xxx`，把 `api_key` 一并填上。

## 5. 验证 vLLM 是否正常

```bash
curl http://localhost:8000/v1/models
```

如果返回模型列表，说明服务已起来。

## 6. 运行项目

字段抽取：

```bash
python main.py query \
  --dir example/2025年中国城市经济百强全景报告 \
  --query "提取城市名、GDP总量（亿元）、常住人口（万）、人均GDP（元）、一般公共预算收入（亿元）" \
  --fields "城市名" "GDP总量（亿元）" "常住人口（万）" "人均GDP（元）" "一般公共预算收入（亿元）"
```

模板填表：

```bash
python main.py run \
  --dir example/2025山东省环境空气质量监测数据信息 \
  --template example/2025山东省环境空气质量监测数据信息/2025山东省环境空气质量监测数据信息-模板.docx \
  --query-file example/2025山东省环境空气质量监测数据信息/用户要求.txt
```

## 7. 常见建议

- 如果应用和 vLLM 都在同一个 WSL 中，优先用 `localhost`
- 如果应用在 Windows、vLLM 在 WSL，建议把应用也迁到 WSL，最省事
- 3B 模型更适合抽取任务，但复杂融合解释不如更大模型稳定
- 抽取任务建议 `temperature: 0.0 ~ 0.1`
