"""Environment and business configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Loads .env next to this project, if present. Missing file is fine.
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    voice: str
    db_path: Path


def load_business_config() -> dict:
    with open(ROOT / "config" / "agent.json", encoding="utf-8") as fh:
        return json.load(fh)


SETTINGS = Settings(
    api_key=os.getenv("GEMINI_API_KEY", "").strip(),
    model=os.getenv("GEMINI_MODEL", "gemini-3.8-live").strip(),
    voice=os.getenv("GEMINI_VOICE", "Puck").strip(),
    db_path=Path(os.getenv("VOIAG_DB") or ROOT / "data" / "voiag.db"),
)
