"""AI 服务层。

- ``llm_adapter``      统一 chat_completion 适配层（大模型兼容协议，服务商可配置）
- ``prompt_templates`` 场景化 Prompt 模板（外部 YAML 可配）
- ``guardrails``       输入/输出安全护栏
- ``context_builder``  把量化评估结果组装成 LLM 上下文
- ``fallback``         LLM 不可用时的规则引擎降级回复
- ``strategy``         策略结构化解析与降级生成
- ``chat_engine``      对话编排（多轮上下文 + SSE 流式 + 降级）
"""

from .llm_adapter import (
    ChatResult,
    LLMError,
    LLMMessage,
    LLMUnavailableError,
    get_chat_adapter,
    get_embedding_adapter,
    llm_status,
    reset_adapters,
    set_chat_adapter,
)
from .prompt_templates import (
    PromptTemplate,
    PromptTemplateError,
    clear_prompt_cache,
    load_prompt_templates,
)

__all__ = [
    "ChatResult",
    "LLMError",
    "LLMMessage",
    "LLMUnavailableError",
    "get_chat_adapter",
    "get_embedding_adapter",
    "llm_status",
    "reset_adapters",
    "set_chat_adapter",
    "PromptTemplate",
    "PromptTemplateError",
    "clear_prompt_cache",
    "load_prompt_templates",
]
