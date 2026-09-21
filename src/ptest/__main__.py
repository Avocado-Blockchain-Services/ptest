"""Module entrypoint for ``python -m ptest``."""
from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
