"""中文感知切片。

替代字符级切分，按句末标点（。！？；等）/换行断句，再用滑动窗口打包为 chunk，
窗口之间保留 ``overlap`` 个字符重叠，避免把一句话从中间切断。
chunk 大小按**中文字符数**评估（建议 300–800 字，overlap 10–15%）。
"""

from __future__ import annotations

import re

# 句末标点（中文句号/叹号/问号/分号 + 英文对应 + 换行）
_SENT_END = "。！？!?；;\n"
_SENT_SPLIT = re.compile(r"[^" + _SENT_END + r"]+[" + _SENT_END + r"]?")


def _split_sentences(text: str) -> list[str]:
    return [m.group().strip() for m in _SENT_SPLIT.finditer(text) if m.group().strip()]


def chunk_text(
    text: str,
    *,
    chunk_size: int = 480,
    overlap: int = 60,
) -> list[str]:
    """把长文本切成若干带重叠的窗口。空文本返回 []。"""
    if not text or not text.strip():
        return []
    sentences = _split_sentences(text)
    if not sentences:
        return []

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    for sent in sentences:
        # 超长单句（如一段没标点的长文）强制按字符截断，避免单个 chunk 爆掉
        if len(sent) > chunk_size:
            if buf:
                chunks.append("".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(sent), chunk_size):
                chunks.append(sent[i : i + chunk_size])
            continue

        if buf_len + len(sent) > chunk_size and buf:
            chunks.append("".join(buf))
            tail = "".join(buf)
            tail = tail[-overlap:] if len(tail) > overlap else tail
            buf = [tail] if tail else []
            buf_len = len(buf[0]) if buf else 0

        buf.append(sent)
        buf_len += len(sent)

    if buf:
        chunks.append("".join(buf))

    return [c.strip() for c in chunks if c.strip()]
