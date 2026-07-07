from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_smart_read():
    path = Path(__file__).resolve().parents[1] / ".project-intel/scripts/smart_read.py"
    spec = spec_from_file_location("smart_read", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_smart_read_returns_compact_summary(tmp_path):
    module = load_smart_read()
    target = tmp_path / "sample.py"
    target.write_text(
        """

def compute(x):
    \"\"\"Compute a value.\"\"\"
    return x + 1
""".strip()
    )

    summary = module.build_summary(target, query="compute")
    assert "compute" in summary.lower()
    assert "return x + 1" not in summary
