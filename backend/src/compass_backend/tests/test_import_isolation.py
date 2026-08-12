"""Import isolation checks for the active Compass backend package."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_compass_backend_package_exposes_active_modules() -> None:
    import compass_backend.api.app

    assert compass_backend.api.app.create_app is not None


def test_src_path_does_not_reintroduce_archived_legacy_package() -> None:
    root = Path(__file__).resolve().parents[3]
    script = """
import sys
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root / "src"))
import compass_backend
import compass_backend.config
resolved = Path(compass_backend.__file__).resolve()
assert resolved.is_relative_to(root / "src" / "compass_backend"), resolved
assert not resolved.is_relative_to(root / "src" / "archive"), resolved
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
