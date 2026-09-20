"""
Constant provenance for :mod:`mathcore`.

``nutms`` owns ``nutms-constants``: it re-derives every nothing-up-my-sleeve
constant a primitive relies on and confirms it matches the published value --
the honest mirror of the folklore gate.
"""

from .nutms import (
    blowfish_pi_words,
    md5_sine_table,
    sha1_round_constants,
    sha256_round_constants,
    tea_delta,
    verify_published_constants,
)

__all__ = [
    "blowfish_pi_words",
    "md5_sine_table",
    "sha1_round_constants",
    "sha256_round_constants",
    "tea_delta",
    "verify_published_constants",
]
