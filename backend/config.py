"""应用配置。

- 评分相关：维度 / 因子 / 权重 / 规则等业务配置在 ``backend/scoring_config.yaml``
  （SOW §3.0 M0：改配置即生效，无需改代码）。
- LLM 相关：API Key / Base URL / 模型名走 ``.env``（SOW §3.2.3）；
  Prompt 文案在 ``backend/prompt_templates.yaml``（SOW §7 可维护性：Prompt 外部可配）。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# .env 仅开发期使用（SOW §7）；未安装 python-dotenv 时静默跳过，只读进程环境变量
try:  # pragma: no cover - 依赖是否安装与业务逻辑无关
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:  # pragma: no cover
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ══════════════════════════ 评分引擎（SOW §3.0）══════════════════════════════

# 评分策略：rule_based（配置驱动，默认）/ config（同义）/ ml（预留模型接入）
SCORING_STRATEGY = os.getenv("SCORING_STRATEGY", "rule_based")

# 评分配置文件路径，可用环境变量覆盖（便于多环境使用不同权重）
SCORING_CONFIG_PATH = os.getenv(
    "SCORING_CONFIG_PATH",
    os.path.join(BASE_DIR, "scoring_config.yaml"),
)


# ══════════════════════════ LLM 对话（SOW §3.2.3）════════════════════════════
# 统一走 OpenAI 兼容协议，换供应商只需改 BASE_URL / MODEL / API_KEY（SOW §2.2）。

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
# 官方 OpenAI 兼容端点（不带 /v1，适配器自行拼接 /chat/completions）
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
# DeepSeek-V4-Flash 正式版 API 已于 2026-07-31 上线（版本号 DeepSeek-V4-Flash-0731）；
# 官方 API 模型标识为小写 deepseek-v4-flash，该标识始终指向最新版本
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.3)
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 2048)
LLM_TIMEOUT = _env_float("LLM_TIMEOUT", 60.0)
LLM_CONNECT_TIMEOUT = _env_float("LLM_CONNECT_TIMEOUT", 8.0)
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 2)
LLM_RETRY_BACKOFF = _env_float("LLM_RETRY_BACKOFF", 0.6)

# 总开关：置 false 可强制走规则引擎降级（演示环境 / 断网自测用）
LLM_ENABLED = _env_bool("LLM_ENABLED", True)
# 流式请求是否附带 usage 统计（部分网关不支持，握手失败会自动关闭）
LLM_STREAM_USAGE = _env_bool("LLM_STREAM_USAGE", True)
# 是否启用函数调用工具（customer_compare / knowledge_search）。
# 关闭后对话仍可正常生成，只是不再主动检索与对比；不兼容 function calling
# 的供应商会在适配器层收到 400 后自动去掉 tools 重试，无需改这里。
LLM_TOOLS_ENABLED = _env_bool("LLM_TOOLS_ENABLED", True)


# ══════════════════════════ Embedding（SOW §3.3.2）═══════════════════════════
# 本期固定智谱 GLM embedding-3，同样走 OpenAI 兼容 /embeddings。

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "zhipu")
# 优先读 .env.example / README 约定的 LLM_EMBEDDING_*，兼容旧的 EMBEDDING_* 与 ZHIPU_API_KEY
EMBEDDING_BASE_URL = (
    os.getenv("LLM_EMBEDDING_BASE_URL")
    or os.getenv("EMBEDDING_BASE_URL")
    or "https://open.bigmodel.cn/api/paas/v4"
)
EMBEDDING_API_KEY = (
    os.getenv("LLM_EMBEDDING_API_KEY")
    or os.getenv("EMBEDDING_API_KEY")
    or os.getenv("ZHIPU_API_KEY", "")
)
EMBEDDING_MODEL = (
    os.getenv("LLM_EMBEDDING_MODEL") or os.getenv("EMBEDDING_MODEL") or "embedding-3"
)
EMBEDDING_DIM = _env_int("EMBEDDING_DIM", 1024)
EMBEDDING_BATCH_SIZE = _env_int("EMBEDDING_BATCH_SIZE", 16)
# 嵌入可用性独立于 LLM 总开关：只要配置了 EMBEDDING_API_KEY 即可用
# （即使 LLM_ENABLED=False，RAG 检索仍应可用，不应被 LLM 开关绑死）
EMBEDDING_ENABLED = _env_bool("EMBEDDING_ENABLED", bool(EMBEDDING_API_KEY))


# ══════════════════════════ 对话编排（SOW §3.2.1）════════════════════════════

PROMPT_TEMPLATE_PATH = os.getenv(
    "PROMPT_TEMPLATE_PATH",
    os.path.join(BASE_DIR, "prompt_templates.yaml"),
)

# 多轮上下文窗口：最多带入最近 N 条消息、总字符预算上限（控制 Token 成本，SOW §10）
CHAT_MAX_CONTEXT_MESSAGES = _env_int("CHAT_MAX_CONTEXT_MESSAGES", 12)
CHAT_CONTEXT_CHAR_BUDGET = _env_int("CHAT_CONTEXT_CHAR_BUDGET", 8000)
# 单条用户输入长度上限（安全护栏，SOW §3.2.1）
CHAT_MAX_INPUT_CHARS = _env_int("CHAT_MAX_INPUT_CHARS", 4000)
# 趋势上下文带入的历史评估条数
CHAT_TREND_POINTS = _env_int("CHAT_TREND_POINTS", 6)
# 降级模式下模拟流式的分片大小（字符）
CHAT_DEGRADED_CHUNK_SIZE = _env_int("CHAT_DEGRADED_CHUNK_SIZE", 24)


# ══════════════════════════ 知识库 RAG（SOW §3.3）═══════════════════════
# 向量库：chroma（生产，需 chromadb）/ memory（开发自测，纯内存无需依赖）。
# 生产环境 chromadb 未安装时自动回退 memory 并打日志，保证基础功能可用。
KNOWLEDGE_VECTOR_STORE = os.getenv("KNOWLEDGE_VECTOR_STORE", "chroma")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", os.path.join(BASE_DIR, "data", "chroma"))
KNOWLEDGE_DATA_DIR = os.getenv("KNOWLEDGE_DATA_DIR", os.path.join(BASE_DIR, "data", "knowledge"))
KNOWLEDGE_COLLECTION = os.getenv("KNOWLEDGE_COLLECTION", "customer_health_kb")

# 中文按标点分句切片（SentenceWindow 思路）：chunk 大小按中文字符评估
CHUNK_SIZE = _env_int("CHUNK_SIZE", 480)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 60)
# 检索：先召回 RAG_RECALL_K 候选，再 Rerank 到 RAG_TOP_K（SOW §3.3.2 Top-K=5）
RAG_TOP_K = _env_int("RAG_TOP_K", 5)
RAG_RECALL_K = _env_int("RAG_RECALL_K", 20)
# 命中切片窗口扩展：把相邻切片一起拼进上下文，缓解跨切片信息截断（0=关闭）
RAG_WINDOW = _env_int("RAG_WINDOW", 1)
RERANKER = os.getenv("RERANKER", "metadata")  # metadata（默认，无依赖）/ bge（本地 CrossEncoder）
BGE_MODEL = os.getenv("BGE_MODEL", "BAAI/bge-reranker-v2-m3")
# 检索时优先提升的分类权重（内部规范 / 指标 > 外部趋势，SOW §3.3.2 分类权重）
# key 必须与 models.KNOWLEDGE_CATEGORIES 保持一致
RAG_CATEGORY_WEIGHTS = {
    "内部规范": 1.3,
    "内部指标": 1.1,
    "外部指标": 1.0,
    "对话沉淀": 1.1,
}
