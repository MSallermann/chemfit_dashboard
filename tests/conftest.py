import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from chemfit import EvaluateContext, component, pipeline
from chemfit.fitter_callbacks import SaveMetaData
from PySide6.QtWidgets import QApplication

from chemfit_dashboard.source import JsonFileSource


@pytest.fixture(scope="session", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def chemfit_context():
    @component()
    def compute_quantities(parameters):
        return {"energy": parameters["x"] ** 2}

    @component()
    def compute_loss(quantities):
        return abs(quantities["energy"] - 1.0)

    objective = pipeline(compute_quantities, compute_loss)
    context = EvaluateContext()
    objective({"x": 2.0}, context)
    return context


@pytest.fixture
def chemfit_json_path(tmp_path, chemfit_context):
    SaveMetaData(tmp_path)(8, [chemfit_context])
    return tmp_path / "step_8_ctx_0.json"


@pytest.fixture
def chemfit_document(chemfit_json_path):
    source = JsonFileSource()
    record = source.discover([chemfit_json_path])[0]
    return source.load(record)
