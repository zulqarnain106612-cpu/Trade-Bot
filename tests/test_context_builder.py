from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_context_builder():
    path = Path(__file__).resolve().parents[1] / ".project-intel/scripts/context_builder.py"
    spec = spec_from_file_location("context_builder", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_summarize_source_file_uses_compact_ast_summary(tmp_path):
    module = load_context_builder()
    target = tmp_path / "sample.py"
    target.write_text(
        """
import os


def compute(x):
    \"\"\"Compute a value for the strategy.\"\"\"
    return x + 1

class Worker:
    \"\"\"Simple worker wrapper.\"\"\"
    pass
""".strip()
    )

    summary = module.summarize_source_file(target, query="compute")

    assert "compute" in summary.lower()
    assert "worker" in summary.lower()
    assert "return x + 1" not in summary
    assert "import os" not in summary
