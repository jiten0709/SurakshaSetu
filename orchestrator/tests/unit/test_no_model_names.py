"""Application code names routes, never a model or provider (TDD §1.5): those live in infra/."""

import re
from pathlib import Path

import surakshasetu

# All of src/: surakshasetu and surakshasetu_ingest (Step 11) alike.
SRC = Path(surakshasetu.__file__).parents[1]
MODEL_NAMES = re.compile(
    r"llama|gpt|claude|qwen|mistral|mixtral|gemma|gemini|bge|e5-|gte-|mgte|minilm|deepseek"
    r"|phi-?\d|anthropic|openai(?!-compatible)|cohere|voyage|huggingface",
    re.IGNORECASE,
)


def test_no_source_file_names_a_model_or_provider() -> None:
    hits = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if MODEL_NAMES.search(line)
    ]

    assert not hits, "\n".join(hits)


def test_the_pattern_catches_what_it_should() -> None:
    for named in ("BAAI/bge-m3", "Qwen3-Embedding", "gpt-4o", "gte-multilingual", "phi-3"):
        assert MODEL_NAMES.search(named), named
    assert not MODEL_NAMES.search("an OpenAI-compatible gateway")
