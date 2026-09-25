"""Package entrypoint — delegates to :mod:`src.workers.trader`.

Enables ``python -m src.workers``, matching the ``python -m src.api``
convention used by :mod:`src.api.__main__`.
"""

from __future__ import annotations

from src.workers.trader import main

if __name__ == "__main__":
    main()
