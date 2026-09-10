"""
The folklore gate for :mod:`mathcore`.

``gate`` turns every ``folklore`` registry entry from documentation into a
runtime refusal; ``detectors`` maps a strategy's feature names onto those
entries so the gate cannot be dodged by aliasing. Together they own the
enforcement of the registry's folklore verdict -- the one part of the roadmap
that prevents a loss rather than detecting one.
"""

from .detectors import (
    FOLKLORE_PATTERNS,
    assert_feature_not_folklore,
    detect_folklore,
)
from .gate import (
    MULTIPLE_TESTING_CORRECTIONS,
    FolkloreError,
    ValidationEvidence,
    assert_not_folklore,
)

__all__ = [
    "FOLKLORE_PATTERNS",
    "MULTIPLE_TESTING_CORRECTIONS",
    "FolkloreError",
    "ValidationEvidence",
    "assert_feature_not_folklore",
    "assert_not_folklore",
    "detect_folklore",
]
