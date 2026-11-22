#!/usr/bin/env python3
"""Build a private Questions candidate; never publish."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/src"))
from fminer.questions_export import cli

if __name__ == "__main__":
    cli()
