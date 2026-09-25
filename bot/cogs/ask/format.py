"""Pure helpers for rd-ask: building the conversation from a Discord reply
chain, and fitting an answer into Discord messages. No discord.py objects
are needed here beyond simple attributes, so these are unit-tested alone."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Discord's limit is 2000 characters per message; leave room for the footer.
CHUNK = 1900
# How far back a follow-up looks along the reply chain.
MAX_CHAIN = 10


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str
    images: list[str] = field(default_factory=list)  # data: URLs (user turns)


def strip_command(text: str, prefix: str) -> str:
    """'rd-ask what is DNS' -> 'what is DNS' (also 'rd-ask\\n...')."""
    cmd = f"{prefix}ask"
    t = text.lstrip()
    if t.lower().startswith(cmd.lower()):
        t = t[len(cmd) :]
    return t.strip()


def strip_footer(text: str) -> str:
    """Drop the bot's '-# ...' footer and source lines from an answer chunk."""
    lines = text.split("\n")
    while lines and (lines[-1].startswith("-# ") or not lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def to_messages(turns: list[Turn], limit: int = 4) -> list[dict]:
    """OpenAI-style messages, oldest first. Consecutive turns of the same role
    (an answer split over several Discord messages) are merged. Images: the
    newest ones first, `limit` in total."""
    merged: list[Turn] = []
    for t in turns:
        if merged and merged[-1].role == t.role:
            merged[-1] = Turn(
                t.role,
                f"{merged[-1].text}\n\n{t.text}".strip(),
                merged[-1].images + t.images,
            )
        else:
            merged.append(Turn(t.role, t.text, list(t.images)))
    budget = limit
    keep: dict[int, list[str]] = {}
    for i in range(len(merged) - 1, -1, -1):
        imgs = merged[i].images[:budget]
        keep[i] = imgs
        budget -= len(imgs)
    out = []
    for i, t in enumerate(merged):
        if t.role == "user" and keep.get(i):
            parts: list[dict] = [{"type": "text", "text": t.text or "(image)"}]
            parts += [{"type": "image_url", "image_url": {"url": u}} for u in keep[i]]
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": t.role, "content": t.text})
    return out


def split_message(text: str, size: int = CHUNK) -> list[str]:
    """Split on paragraph, then line, then space boundaries. A split inside a
    ``` code block closes the fence and reopens it in the next chunk."""
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut <= 0:
            cut = size
        head, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n")
        fences = head.count("```")
        if fences % 2 == 1:  # inside a code block: close it, reopen with its language
            lang = head[head.rfind("```") + 3 :].split("\n", 1)[0].strip()
            head += "\n```"
            rest = f"```{lang}\n{rest}"
        chunks.append(head)
    if rest:
        chunks.append(rest)
    return chunks


def format_sources(sources: list[dict]) -> str:
    """'-# [1] Title <https://…>' lines. The angle brackets stop Discord from
    unfurling a preview for every link."""
    lines = []
    for s in sources[:8]:
        title = (s.get("title") or s.get("url") or "").strip().replace("\n", " ")[:80]
        url = s.get("url") or ""
        if url:
            lines.append(f"-# [{s.get('n')}] {title} <{url}>")
        else:  # a document passage: no link
            loc = f" › {s['location']}" if s.get("location") else ""
            lines.append(f"-# [{s.get('n')}] {title}{loc}")
    return "\n".join(lines)


def answer_chunks(content: str, sources: list[dict], model: str) -> list[str]:
    """The Discord messages for an answer: the text split to fit, then the
    sources and a footer on the last one (or a message of their own)."""
    body = content.strip() or "(no answer)"
    if not sources:  # a model citing [n] with nothing behind it: drop the marks
        body = re.sub(r" ?\[\d{1,2}\]", "", body)
    chunks = split_message(body)
    tail = "\n".join(
        x
        for x in [format_sources(sources), f"-# dozai · {model}" if model else ""]
        if x
    )
    if tail:
        if len(chunks[-1]) + len(tail) + 1 <= 2000:
            chunks[-1] = f"{chunks[-1]}\n{tail}"
        else:
            chunks.append(tail)
    return chunks
