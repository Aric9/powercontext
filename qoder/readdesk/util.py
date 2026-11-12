"""通用小工具：哈希、tag 字段名、slug。"""

from __future__ import annotations

import hashlib
import re

_UNSAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]")


def sha256_bytes(data: bytes) -> str:
    """返回字节串的 sha256 十六进制。"""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    """返回文件内容的 sha256 十六进制。"""
    return sha256_bytes(path.read_bytes())


def tag_field(tag: str) -> str:
    """把标签映射为 seekdb metadata 的安全字段名，如 user-study -> tag_user_study。"""
    safe = _UNSAFE.sub("_", tag.strip().lower())
    safe = re.sub(r"_+", "_", safe).strip("_")
    return f"tag_{safe}" if safe else "tag_"


def normalize_tag(tag: str) -> str:
    """标签规范化：小写、去首尾空白。"""
    return tag.strip().lower()


def split_list_value(raw: str) -> list[str]:
    """解析 `[a, b]` 或 `a, b`（支持中文逗号），返回去空字符串列表。"""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    out: list[str] = []
    for part in re.split(r"[,，]", text):
        item = part.strip().strip('\'"“”‘’《》<> ')
        if item:
            out.append(item)
    return out


def slugify(text: str, max_len: int = 40) -> str:
    """把主题转为文件名 slug，保留中文。"""
    slug = _UNSAFE.sub("-", text.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len] if slug else "brief"
