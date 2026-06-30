"""Review-engine dispatcher. A card's `engine` selects which LLM CLI runs the
review/verify stages: 'claude' (Claude Code) or 'codex' (OpenAI Codex)."""
from . import claude_runner, codex_runner

ENGINES = ("claude", "codex")


class EngineError(RuntimeError):
    pass


def run(prompt: str, engine: str = "claude", **kw) -> str:
    if engine == "codex":
        return codex_runner.run(prompt, **kw)
    return claude_runner.run(prompt, **kw)


def run_json(prompt: str, engine: str = "claude", **kw):
    text = run(prompt, engine=engine, **kw)
    return claude_runner.parse_json(text)
