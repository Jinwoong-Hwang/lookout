"""Load prompt templates and fill {TOKEN} placeholders without touching the
literal JSON braces in the template body."""
import os

from . import config

PROMPTS_DIR = config.path("prompts")


def render(name: str, **tokens) -> str:
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as f:
        text = f.read()
    for key, val in tokens.items():
        text = text.replace("{" + key + "}", str(val))
    return text
