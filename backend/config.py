"""应用配置。

- 评分相关：维度 / 因子 / 权重 / 规则等业务配置在 ``backend/scoring_config.yaml``
  。
- LLM 相关：API Key / Base URL / 模型名走 ``.env``；
  Prompt 文案在 ``backend/prompt_templates.yaml``。
"""

import logging
import os


logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# .env 仅开发期使用；未安装 python-dotenv 时静默跳过，只读进程环境变量
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


def _load_secret_key() -> bytes | None:
    """加载密钥解密的 Fernet 密钥：优先 CH_SECRET_KEY 环境变量，其次密钥文件。

    密钥文件路径取 SECRET_KEY_FILE；路径缺失时回退到仓库内默认 ./.ch_secret
    与容器固定挂载点 /run/secrets/ch_secret（兼容本地开发与 compose 默认挂载）。
    文件缺失或不可读返回 None（调用方按“未加密”处理，不阻断启动）。
    """
    env_key = os.getenv("CH_SECRET_KEY", "").strip()
    if env_key:
        return env_key.encode()
    candidates: list[str] = []
    configured = os.getenv("SECRET_KEY_FILE", "").strip()
    if configured:
        if os.path.exists(configured) and not os.path.isfile(configured):
            logger.warning(
                "配置的密钥文件路径存在但不是普通文件（可能被 Docker 挂载成了目录）: %s",
                configured,
            )
        candidates.append(configured)
    candidates.extend(("./.ch_secret", "/run/secrets/ch_secret"))
    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read().strip()
        except OSError as exc:
            logger.warning("读取密钥文件失败（%s）: %s", exc, path)
    return None


def _decrypt_env(raw: str, name: str = "") -> str:
    """支持 enc: 前缀的加密值（Fernet）。非加密值或解密失败时原样返回。

    解密失败（密钥缺失 / 密钥错误）会记 warning，方便部署时排查。
    """
    if not raw.startswith("enc:"):
        return raw
    key = _load_secret_key()
    if key is None:
        logger.warning(
            "检测到 enc: 前缀配置，但未找到 Fernet 密钥，%s将按原样使用",
            f"「{name}」" if name else "该密钥",
        )
        return raw
    try:
        from cryptography.fernet import Fernet

        return Fernet(key).decrypt(raw[4:].encode()).decode()
    except Exception as exc:  # noqa: BLE001 - 解密失败统一走“原样返回”降级
        logger.warning(
            "enc: 配置解密失败，%s将按原样使用: %s",
            f"「{name}」" if name else "该密钥",
            exc,
        )
        return raw


# ══════════════════════════ 评分引擎══════════════════════════════

# 评分策略：rule_based（配置驱动，默认）/ config（同义）/ ml（预留模型接入）
SCORING_STRATEGY = os.getenv("SCORING_STRATEGY", "rule_based")

# 评分配置文件路径，可用环境变量覆盖（便于多环境使用不同权重）
SCORING_CONFIG_PATH = os.getenv(
    "SCORING_CONFIG_PATH",
    os.path.join(BASE_DIR, "scoring_config.yaml"),
)


# ══════════════════════════ LLM 对话════════════════════════════
# 统一走大模型兼容协议（/chat/completions），换服务商只需改 BASE_URL / MODEL / API_KEY。

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "default")
# 大模型兼容端点（不带 /v1，适配器自行拼接 /chat/completions）；未配置则视为不可用、自动降级
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = _decrypt_env(os.getenv("LLM_API_KEY", ""), "LLM_API_KEY")
# 对话模型标识由 .env 指定；未配置则视为不可用、自动降级
LLM_MODEL = os.getenv("LLM_MODEL", "")

LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.3)
# 对话输出 token 上限：作为 prompt_templates.yaml 中各模板 max_tokens 的硬上限
# （实际生效值 = min(模板 max_tokens, LLM_MAX_TOKENS)），默认与模板对齐为 8000
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 8000)
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
# DeepSeek V4 默认开启 thinking。交互对话优先首字速度，PDF 报告优先分析质量。
LLM_CHAT_THINKING_ENABLED = _env_bool("LLM_CHAT_THINKING_ENABLED", False)
LLM_REPORT_THINKING_ENABLED = _env_bool("LLM_REPORT_THINKING_ENABLED", True)
# 进程内 Chat LLM 全局并发上限。所有同步/流式调用（含报告 AI）共用该额度。
CHAT_LLM_GLOBAL_CONCURRENCY = max(1, _env_int("CHAT_LLM_GLOBAL_CONCURRENCY", 8))
# 公网匿名部署的轻量突发保护：按客户端 IP 汇总 AI 重型接口请求。
AI_RATE_LIMIT_PER_MINUTE = max(1, _env_int("AI_RATE_LIMIT_PER_MINUTE", 60))


# ══════════════════════════ Embedding═══════════════════════════
# 向量化走大模型兼容 /embeddings 端点，服务商与模型由 .env 指定。

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "default")
# 优先读 LLM_EMBEDDING_*，兼容旧的 EMBEDDING_* 变量
EMBEDDING_BASE_URL = (
    os.getenv("LLM_EMBEDDING_BASE_URL")
    or os.getenv("EMBEDDING_BASE_URL")
    or ""
)
EMBEDDING_API_KEY = _decrypt_env(
    os.getenv("LLM_EMBEDDING_API_KEY")
    or os.getenv("EMBEDDING_API_KEY")
    or "",
    "EMBEDDING_API_KEY",
)
EMBEDDING_MODEL = (
    os.getenv("LLM_EMBEDDING_MODEL") or os.getenv("EMBEDDING_MODEL") or ""
)
EMBEDDING_DIM = _env_int("EMBEDDING_DIM", 1024)
EMBEDDING_BATCH_SIZE = _env_int("EMBEDDING_BATCH_SIZE", 16)
EMBEDDING_GLOBAL_CONCURRENCY = max(1, _env_int("EMBEDDING_GLOBAL_CONCURRENCY", 1))
# 嵌入可用性独立于 LLM 总开关：只要配置了 EMBEDDING_API_KEY 即可用
# （即使 LLM_ENABLED=False，RAG 检索仍应可用，不应被 LLM 开关绑死）
EMBEDDING_ENABLED = _env_bool("EMBEDDING_ENABLED", bool(EMBEDDING_API_KEY))


# ══════════════════════════ 对话编排════════════════════════════

PROMPT_TEMPLATE_PATH = os.getenv(
    "PROMPT_TEMPLATE_PATH",
    os.path.join(BASE_DIR, "prompt_templates.yaml"),
)

# 多轮上下文窗口：最多带入最近 N 条消息、总字符预算上限（控制 Token 成本，）
CHAT_MAX_CONTEXT_MESSAGES = _env_int("CHAT_MAX_CONTEXT_MESSAGES", 12)
CHAT_CONTEXT_CHAR_BUDGET = _env_int("CHAT_CONTEXT_CHAR_BUDGET", 8000)
# 单条用户输入长度上限（安全护栏，）
CHAT_MAX_INPUT_CHARS = _env_int("CHAT_MAX_INPUT_CHARS", 4000)
# 趋势上下文带入的历史评估条数
CHAT_TREND_POINTS = _env_int("CHAT_TREND_POINTS", 6)
# 降级模式下模拟流式的分片大小（字符）
CHAT_DEGRADED_CHUNK_SIZE = _env_int("CHAT_DEGRADED_CHUNK_SIZE", 24)


# 异步 PDF 导出任务（内存态）：进程内最多保留的任务数，超出后清理最早完成的
# ready/error 任务（不误删 running）；并发上限用于防止线程/内存被任务打满；
# TTL 兜底清理疑似挂死的任务（running 超时同样回收）。均可经环境变量覆盖。
PDF_JOB_MAX = _env_int("PDF_JOB_MAX", 50)
PDF_JOB_MAX_CONCURRENT = max(1, _env_int("PDF_JOB_MAX_CONCURRENT", 1))
PDF_JOB_TTL = max(60, _env_int("PDF_JOB_TTL", 3600))


# ══════════════════════════ 知识库 RAG═══════════════════════
# 向量库：chroma（生产，需 chromadb）/ memory（开发自测，纯内存无需依赖）。
# 生产环境 chromadb 未安装时自动回退 memory 并打日志，保证基础功能可用。
KNOWLEDGE_VECTOR_STORE = os.getenv("KNOWLEDGE_VECTOR_STORE", "chroma")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", os.path.join(BASE_DIR, "data", "chroma"))
KNOWLEDGE_DATA_DIR = os.getenv("KNOWLEDGE_DATA_DIR", os.path.join(BASE_DIR, "data", "knowledge"))
KNOWLEDGE_COLLECTION = os.getenv("KNOWLEDGE_COLLECTION", "customer_health_kb")

# 知识文档上传/解析资源护栏。限制在调用解析器和 Embedding 之前生效；
# Office 文件还会检查 ZIP 条目数与声明的解压后总大小。
UPLOAD_MAX_BYTES = max(1, _env_int("UPLOAD_MAX_BYTES", 20 * 1024 * 1024))
# multipart 请求包含边界与表单字段，入口请求体额度默认比文件额度多 1MiB。
UPLOAD_MAX_REQUEST_BYTES = max(
    UPLOAD_MAX_BYTES,
    _env_int("UPLOAD_MAX_REQUEST_BYTES", UPLOAD_MAX_BYTES + 1024 * 1024),
)
UPLOAD_GLOBAL_CONCURRENCY = max(1, _env_int("UPLOAD_GLOBAL_CONCURRENCY", 1))
UPLOAD_MAX_EXTRACTED_CHARS = max(1, _env_int("UPLOAD_MAX_EXTRACTED_CHARS", 500_000))
UPLOAD_MAX_CHUNKS = max(1, _env_int("UPLOAD_MAX_CHUNKS", 500))
UPLOAD_MAX_EMBEDDING_TOKENS = max(
    1, _env_int("UPLOAD_MAX_EMBEDDING_TOKENS", 200_000)
)
UPLOAD_MAX_PDF_PAGES = max(1, _env_int("UPLOAD_MAX_PDF_PAGES", 200))
UPLOAD_MAX_DECOMPRESSED_BYTES = max(
    1, _env_int("UPLOAD_MAX_DECOMPRESSED_BYTES", 100 * 1024 * 1024)
)
UPLOAD_MAX_ZIP_ENTRIES = max(1, _env_int("UPLOAD_MAX_ZIP_ENTRIES", 2_000))

# 中文按标点分句切片（SentenceWindow 思路）：chunk 大小按中文字符评估
CHUNK_SIZE = _env_int("CHUNK_SIZE", 480)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 60)
# 检索：先召回 RAG_RECALL_K 候选，再 Rerank 到 RAG_TOP_K
RAG_TOP_K = _env_int("RAG_TOP_K", 5)
RAG_RECALL_K = _env_int("RAG_RECALL_K", 20)
# 命中切片窗口扩展：把相邻切片一起拼进上下文，缓解跨切片信息截断（0=关闭）
RAG_WINDOW = _env_int("RAG_WINDOW", 1)
RERANKER = os.getenv("RERANKER", "metadata")  # metadata（默认，无依赖）/ bge（本地 CrossEncoder）
BGE_MODEL = os.getenv("BGE_MODEL", "BAAI/bge-reranker-v2-m3")
# 检索时优先提升的分类权重（内部规范 / 指标 > 外部趋势， 分类权重）
# key 必须与 models.KNOWLEDGE_CATEGORIES 保持一致
RAG_CATEGORY_WEIGHTS = {
    "内部规范": 1.3,
    "内部指标": 1.1,
    "外部指标": 1.0,
    "对话沉淀": 1.1,
}
