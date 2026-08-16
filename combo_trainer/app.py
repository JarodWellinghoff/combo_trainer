"""
app.py — Entry point. Run with:  python -m combo_trainer.app

Windows note: python-mpv needs libmpv-2.dll at runtime. Either place it next
to this package (we add that directory below), put it on PATH, or set the
MPV_DLL_DIR environment variable to its folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepare_mpv_dll() -> None:
    """Make libmpv discoverable BEFORE main_window imports mpv."""
    if sys.platform != "win32":
        return
    candidates = [
        os.environ.get("MPV_DLL_DIR"),
        str(Path(__file__).resolve().parent.parent),  # project root
        str(Path(__file__).resolve().parent),
    ]
    for folder in filter(None, candidates):
        p = Path(folder)
        if (p / "libmpv-2.dll").exists() or (p / "mpv-2.dll").exists():
            os.add_dll_directory(str(p))
            return


def main() -> int:
    _prepare_mpv_dll()

    from PyQt6.QtWidgets import QApplication

    from .main_window import MainWindow  # imports mpv (needs DLL path above)

    app = QApplication(sys.argv)
    app.setApplicationName("Tokon Combo Trainer")
    app.setOrganizationName("ComboTrainer")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
