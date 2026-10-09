"""The workflow notebook executes end-to-end (here against SYNTHETIC demo fixtures, explicitly selected)."""
from pathlib import Path

import nbformat
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.slow
def test_notebook_runs_on_demo(tmp_path, monkeypatch):
    nbclient = pytest.importorskip("nbclient")
    monkeypatch.setenv("OCO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("oco.settings.PROJECT_ROOT", ROOT)
    from oco.demo import build
    build()
    nb = nbformat.read(ROOT / "notebooks" / "headline_to_evidence.ipynb", as_version=4)
    nb.cells[1].source = nb.cells[1].source.replace("USE_DEMO = False", "USE_DEMO = True")
    nbclient.NotebookClient(nb, timeout=300, kernel_name="python3", resources={"metadata": {"path": str(ROOT / "notebooks")}}).execute()
    outs = "".join(o.get("text", "") for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []))
    assert "SYNTHETIC DEMO — NOT LIVE" in outs
