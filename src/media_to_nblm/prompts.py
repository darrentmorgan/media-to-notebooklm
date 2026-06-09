"""Default prompt pack run against a freshly populated notebook."""
from __future__ import annotations

import logging
import pathlib
from typing import Any

from . import notebook as nb

log = logging.getLogger(__name__)

# Prepended to every prompt to keep answers grounded in the actual sources and
# stop NotebookLM from dragging in its own marketing/about content.
GROUNDING = (
    "Use only the provided video sources to answer. Do not reference NotebookLM, "
    "Google, or any external context. If the sources do not contain the answer, "
    "say so explicitly."
)

VIDEO_PROMPTS: list[tuple[str, str]] = [
    (
        "core_idea",
        "Summarize the single core idea or thesis of this video in 3-5 sentences. "
        "Be specific and concrete — quote or paraphrase the speaker's exact framing.",
    ),
    (
        "actionable",
        "List the 5-10 most actionable takeaways from this video. One per line, "
        "imperative voice (e.g. 'Do X', 'Avoid Y'). Cite timestamps if present.",
    ),
    (
        "counterpoints",
        "What are the limitations, caveats, or counterpoints to the speaker's claims? "
        "If the speaker addresses them, summarize. If not, note the gap.",
    ),
]

PLAYLIST_PROMPTS: list[tuple[str, str]] = [
    (
        "through_line",
        "In 5 bullets: what is this playlist's curatorial through-line? "
        "What arc or progression do the videos follow as a set?",
    ),
    (
        "top_ideas",
        "List the 10 most important ideas across the playlist. One line each. "
        "Cite which video each idea comes from.",
    ),
    (
        "synthesis",
        "If a viewer watched the entire playlist back-to-back, what 3-sentence "
        "synthesis captures the cumulative takeaway?",
    ),
]

CHANNEL_PROMPTS: list[tuple[str, str]] = [
    (
        "overview",
        "In 5 bullets: what is this channel about, what themes recur, and who is the audience?",
    ),
    (
        "top_ideas",
        "List the 10 most interesting or actionable ideas across these videos. One line each. "
        "Cite which video each idea comes from.",
    ),
    (
        "outlier",
        "Which single video is the most distinctive or highest-signal? Why? "
        "Summarize the video's core claim in 3 sentences.",
    ),
]

PROMPT_PACKS: dict[str, list[tuple[str, str]]] = {
    "video": VIDEO_PROMPTS,
    "playlist": PLAYLIST_PROMPTS,
    "channel": CHANNEL_PROMPTS,
}

# Backwards-compat: callers that still import DEFAULT_PROMPTS get the channel pack.
DEFAULT_PROMPTS = CHANNEL_PROMPTS


def _answer_text(resp: dict[str, Any]) -> str:
    for key in ("answer", "response", "text", "content"):
        val = resp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return str(resp)


import re as _re

# Matches NotebookLM citation markers: [1], [1, 2], [1-3], [1, 3-5], etc.
_CITATION_RE = _re.compile(r"\[((?:\d+[\s,\-–]*)+)\]")


def _expand_citation_numbers(group: str) -> list[int]:
    """Expand a citation group like '1, 3-5' into [1, 3, 4, 5]."""
    nums: list[int] = []
    for token in _re.split(r"[,\s]+", group.strip()):
        if not token:
            continue
        if "-" in token or "–" in token:
            parts = _re.split(r"[-–]", token)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                a, b = int(parts[0]), int(parts[1])
                if a <= b and b - a < 50:  # sanity cap
                    nums.extend(range(a, b + 1))
                    continue
        if token.isdigit():
            nums.append(int(token))
    return nums


def _linkify_citations(
    answer: str,
    references: list[dict[str, Any]] | None,
    source_map: dict[str, str] | None,
) -> str:
    """Replace [N] / [N, M] / [N-M] markers with Markdown links to source URLs.

    Builds citation_number → url mapping from the ask response's `references`
    (each has source_id + citation_number) joined to source_map (source_id → url).
    Falls back to leaving the marker untouched if the URL can't be resolved.
    """
    if not references or not source_map:
        return answer

    cite_to_url: dict[int, str] = {}
    for ref in references:
        if not isinstance(ref, dict):
            continue
        n = ref.get("citation_number")
        sid = ref.get("source_id")
        if isinstance(n, int) and isinstance(sid, str):
            url = source_map.get(sid)
            if url:
                cite_to_url[n] = url

    if not cite_to_url:
        return answer

    def repl(m: _re.Match) -> str:
        nums = _expand_citation_numbers(m.group(1))
        parts: list[str] = []
        for n in nums:
            url = cite_to_url.get(n)
            if url:
                parts.append(f"[{n}]({url})")
            else:
                parts.append(f"[{n}]")
        return " ".join(parts) if parts else m.group(0)

    return _CITATION_RE.sub(repl, answer)


def run_default_pack(
    notebook_id: str,
    out_path: pathlib.Path,
    *,
    mode: str = "channel",
    custom_ask: str | None = None,
    source_map: dict[str, str] | None = None,
) -> str:
    """Run a prompt pack against the populated notebook and write a markdown digest.

    - mode: "video" | "playlist" | "channel" (selects the default pack).
    - custom_ask: if provided, REPLACES the default pack with a single ask
      containing the user's literal question (still grounded).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sections: list[str] = [f"# Digest for notebook `{notebook_id}`\n"]

    if custom_ask and custom_ask.strip():
        prompts = [("user_question", custom_ask.strip())]
    else:
        prompts = PROMPT_PACKS.get(mode, CHANNEL_PROMPTS)

    for slug, raw_prompt in prompts:
        grounded = f"{GROUNDING}\n\n{raw_prompt}"
        log.info("asking: %s", slug)
        try:
            resp = nb.ask(notebook_id, grounded)
            answer = _answer_text(resp)
            refs = resp.get("references") if isinstance(resp, dict) else None
            answer = _linkify_citations(answer, refs, source_map)
        except Exception as e:
            log.warning("prompt %s failed: %s", slug, e)
            answer = f"_prompt failed: {e}_"
        sections.append(f"## {slug}\n\n**Prompt:** {raw_prompt}\n\n{answer}\n")
    digest = "\n".join(sections)
    out_path.write_text(digest)
    return digest
