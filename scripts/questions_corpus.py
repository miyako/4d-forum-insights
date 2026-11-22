#!/usr/bin/env python3
"""Explicit private all-source-language Questions execution; no site export."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/src"))
from fminer.questions_corpus import cli


if __name__ == "__main__":
    cli()
