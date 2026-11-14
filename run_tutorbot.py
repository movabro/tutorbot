""" Entrypoint script so `python3 run_tutorbot.py ...` works with clean prefix matching."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from tutorbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
