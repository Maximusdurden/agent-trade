"""The chart module must import cleanly even when charting deps are absent.

On 2026-11-09 the 11/9 post shipped with no chart. The cause was not a missing
trade -- it was that ``core/charts.py`` called ``mpf.make_marketcolors()`` at
module level, while the import block above it sets ``mpf = None`` when
matplotlib/mplfinance are unavailable. Importing the module therefore raised
AttributeError, which the caller caught and logged as:

    Chart render/upload skipped for AMD: 'NoneType' object has no attribute
    'make_marketcolors'

...and the post published without a chart. A missing optional dependency must
degrade to "no chart", never to "module cannot be imported".
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_module_imports_without_charting_deps():
    """Simulate the deps being absent and confirm the import still succeeds."""
    code = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.split('.')[0] in ('matplotlib', 'mplfinance'):\n"
        "            return self\n"
        "        return None\n"
        "    def load_module(self, name):\n"
        "        raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import core.charts as c\n"
        "assert c._MPL_AVAILABLE is False, 'expected deps to be blocked'\n"
        "assert c.CHART_STYLE is None\n"
        "assert c.generate_trade_chart('AMD', []) is None\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"import failed without charting deps:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_generate_returns_none_for_empty_trips():
    """No round-trips means no chart, and that must not raise."""
    from core import charts

    assert charts.generate_trade_chart("AMD", []) is None


def test_style_is_defined_when_deps_present():
    """When the deps ARE installed, the style must actually be built."""
    from core import charts

    if not charts._MPL_AVAILABLE:
        pytest.skip("charting deps not installed in this environment")
    assert charts.CHART_STYLE is not None
    assert charts.mc is not None
