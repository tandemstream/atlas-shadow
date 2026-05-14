"""Module entry — ``python -m atlas_shadow.ingest_daemon``."""

from .entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
