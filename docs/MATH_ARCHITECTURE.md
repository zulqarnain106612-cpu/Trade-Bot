# Mathematical core: architecture, wiring and contracts

Companion to `docs/MATH_FOUNDATIONS.md` (what the objects are and whether they
matter) and `docs/MATH_ROADMAP.md` (the order they get built in). This document
fixes **where each one lives, what its interface is, and what it is forbidden
from doing**.

Everything here is specification. No module below `src/mathcore/` exists yet
except `registry.py`; that is deliberate. Empty files with `pass` in them are
not an architecture, they are debt that looks like progress. The contracts are
written first so that when the code is written there is nothing left to decide.

---

## 1. Design laws

These are not preferences. A change that violates one is rejected in review.

**L1 — Verification only, never signing.**
`mathcore` may verify a signature, derive a public key, decompress a point and
analyse a chain. It must never hold a private key, produce a signature, or
broadcast a transaction. Signing lives behind `src/security/`, where the key
custody boundary already is. This keeps the largest and most experimental body
of new code entirely outside the blast radius of key material.

**L2 — Pure core, effectful edge.**
`fields/`, `numbertheory/`, `harmonic/`, `coding/`, `probability/` are pure:
no network, no filesystem, no clock, no global state. They take numbers and
return numbers. This is what makes them property-testable against known
answers and reproducible across runs, which is the whole reason for having
them in one place.

**L3 — Constant-time where secrets flow, and only there.**
Any routine that can be reached with secret input must be branch-free and
memory-access-uniform in that input, and must say so in its docstring. Any
routine that cannot must say that too. A module that is silent on the question
is treated as unsafe for secrets. `src/security/constant_time.py` already
holds the comparison primitives; new secret-touching code uses them rather
than reimplementing.

**L4 — Registry-anchored.**
Every module docstring names the `config/math_registry.json` entry ids it
owns. A module that owns nothing in the registry should not exist; an entry
with no owner is a gap, and both directions are checked in
`tests/test_math_registry.py`.

**L5 — Folklore never reaches an order.**
Anything with verdict `folklore` is inert by construction: it can be
described, detected and reported, but it cannot produce a value that a
position sizer consumes without passing `src/mathcore/folklore/gate.py`.

**L6 — Determinism.**
No routine draws randomness implicitly. Where randomness is genuinely needed
(sampling, Monte Carlo confirmation modelling) the caller passes a seed and
the result is reproducible from it. This is a hard requirement for backtests
that are comparable across runs.

**L7 — No cryptographic reimplementation in the hot path.**
Where a vetted library exists (`cryptography`, `coincurve`, the platform TLS
stack), the library is used in production and `mathcore` provides the analysis,
the audit and the test oracle. Hand-rolled field arithmetic exists to *check*
implementations and to reason about parameters, not to replace a hardened one.

---

## 2. Layering

Dependencies point downward only. A cycle here is the same defect the registry
loader already refuses in the data.

```
                    ┌──────────────────────────────────────────┐
   strategy /       │  src/strategies/   src/risk/             │
   execution        │  src/features/     src/execution/        │
                    └───────────────┬──────────────────────────┘
                                    │  consumes (gated)
                    ┌───────────────▼──────────────────────────┐
   application      │  src/ecc/        src/security/           │
   crypto           │  (on-chain analytics, key custody, TLS)  │
                    └───────────────┬──────────────────────────┘
                                    │  consumes
   ┌────────────────────────────────▼─────────────────────────────────┐
   │                        src/mathcore/                             │
   │                                                                   │
   │  folklore/    ← gate; depends on registry only                    │
   │  derivation/  ← bip32, nonces, threshold                          │
   │  commitments/ ← merkle                                            │
   │  lattice/     ← lll, hnp                                          │
   │  curves/      ← secp256k1, ed25519, bls12_381                     │
   │  coding/      ← reed_solomon, mds                                 │
   │  harmonic/    ← ntt consumers, walsh_hadamard, gaussian, spectral  │
   │  probability/ ← birthday, confirmations, entropy, committee, reorg │
   │  numbertheory/← primality, residues, crt, factorization, ...       │
   │  fields/      ← prime_field, binary_field, ntt, interpolation      │
   │  constants/   ← nutms                                             │
   │  registry.py  ← governance; depends on nothing in mathcore        │
   └───────────────────────────────────────────────────────────────────┘
```

`fields/` and `registry.py` are the only leaves. `numbertheory/` may import
`fields/`. `curves/` may import both. Nothing in `mathcore` imports from
`src/strategies/`, `src/execution/` or `src/api/` — enforced by a test.

---

## 3. Module contracts

Each entry gives the module, the registry ids it owns, its public surface, and
its explicit non-goals. Signatures are the contract; the roadmap phase decides
when they are filled in.

### 3.1 `src/mathcore/fields/`

**`src/mathcore/fields/__init__.py`** — owns `finite-fields`.

Finite fields are one algebraic object with two implementations here, one per
characteristic. Neither module below implements the other's half, so neither
can honestly be the sole owner and both cannot be owners — the registry allows
exactly one. The package is the owner; the two modules are its `component`
wiring points, held to the same existence check and re-exported from here.

**`src/mathcore/fields/prime_field.py`** — component of `finite-fields`; owns
`pseudo-mersenne-primes`.
```
class PrimeField:
    def __init__(self, modulus: int, *, name: str = "") -> None
    def add(self, a: int, b: int) -> int
    def mul(self, a: int, b: int) -> int
    def inv(self, a: int) -> int            # raises on a == 0
    def pow(self, a: int, e: int) -> int    # square-and-multiply, fixed window
    def sqrt(self, a: int) -> int | None    # delegates to residues.tonelli_shanks
SECP256K1_P: PrimeField    # 2**256 - 2**32 - 977
CURVE25519_P: PrimeField   # 2**255 - 19
```
Non-goals: not constant-time in v1 and the docstring says so, so it is an
analysis tool, not a signer. Constant-time variants arrive with a benchmark
that proves the claim rather than asserting it.

**`src/mathcore/fields/binary_field.py`** — component of `finite-fields`;
provides the `linear-algebra-f2` support and the GF(2^n) arithmetic behind
`galois-field-gcm` analysis.
```
class BinaryField:
    def __init__(self, degree: int, modulus_poly: int) -> None
    def mul(self, a: int, b: int) -> int
    def inv(self, a: int) -> int
GF2_8_AES: BinaryField     # x^8 + x^4 + x^3 + x + 1
GF2_128_GCM: BinaryField   # x^128 + x^7 + x^2 + x + 1
```
Non-goals: does not implement GCM. GCM is the TLS stack's job; this exists to
verify a GHASH trace when auditing one.

**`src/mathcore/fields/ntt.py`** — owns `ntt`.
```
def ntt(values: Sequence[int], field: PrimeField, root: int) -> list[int]
def intt(values: Sequence[int], field: PrimeField, root: int) -> list[int]
def negacyclic_ntt(values, field, root) -> list[int]   # the ML-KEM variant
def find_primitive_root(field: PrimeField, order: int) -> int
```
Non-goals: not a KEM. It is the transform, tested against schoolbook
convolution on random inputs, which is the only honest correctness oracle.

**`src/mathcore/fields/interpolation.py`** — owns `lagrange-interpolation`.
```
def interpolate(points: Sequence[tuple[int, int]], field: PrimeField) -> list[int]
def evaluate(coeffs: Sequence[int], x: int, field: PrimeField) -> int
def split_secret(secret: int, threshold: int, shares: int, field, *, seed: int) -> list
def recover_secret(shares: Sequence[tuple[int, int]], field: PrimeField) -> int
```
Non-goals: `split_secret` is for modelling and testing threshold schemes, not
for production custody. Production custody uses a reviewed MPC implementation;
this is how its output gets checked.

### 3.2 `src/mathcore/numbertheory/`

**`src/mathcore/numbertheory/primality.py`** — owns `primality-testing-bpsw`,
`lucas-sequences`; consumed by `safe-primes-dh`.
```
def miller_rabin(n: int, *, rounds: int = 64, seed: int = 0) -> bool
def strong_lucas(n: int) -> bool
def is_probable_prime(n: int) -> bool        # BPSW: MR base 2 + strong Lucas
def is_safe_prime(n: int) -> bool
```
This is the module where the Fibonacci family does real cryptographic work,
and its docstring says so, because that is the single most misunderstood point
in the whole registry.

**`src/mathcore/numbertheory/residues.py`** — owns `quadratic-residues`,
`tonelli-shanks`.
```
def legendre_symbol(a: int, p: int) -> int
def jacobi_symbol(a: int, n: int) -> int
def tonelli_shanks(a: int, p: int) -> int | None
def decompress_point_y(x: int, curve) -> tuple[int, int] | None
```

**`src/mathcore/numbertheory/crt.py`** — owns `chinese-remainder-theorem`.
```
def crt(residues: Sequence[int], moduli: Sequence[int]) -> tuple[int, int]
def rsa_crt_recombine(sig_p, sig_q, p, q, q_inv, n) -> int
def detect_crt_fault(signature: int, message: int, e: int, n: int) -> int | None
```
`detect_crt_fault` returns a recovered factor when a faulty CRT signature is
supplied. It exists as a defensive check: verify before release, and this is
the check.

**`src/mathcore/numbertheory/factorization.py`** — owns
`prime-factorisation-rsa`.
```
def pollard_rho(n: int, *, max_iterations: int) -> int | None
def pollard_p_minus_1(n: int, *, bound: int) -> int | None
def batch_gcd(moduli: Sequence[int]) -> dict[int, int]
```
`batch_gcd` is the shared-prime audit: run it over a population of public keys
and it names any that share a factor. Non-goal: no general-purpose factoring.

**`src/mathcore/numbertheory/continued_fractions.py`** — owns
`continued-fractions`.
```
def continued_fraction(numerator: int, denominator: int) -> list[int]
def convergents(cf: Sequence[int]) -> Iterator[tuple[int, int]]
def wiener_attack(e: int, n: int) -> int | None     # audit: is d too small?
```

**`src/mathcore/numbertheory/dlp_bounds.py`** — owns
`generic-dlp-algorithms`, `number-field-sieve`.
```
def generic_security_bits(group_order: int) -> float           # rho: half the bits
def pohlig_hellman_effective_bits(group_order: int) -> float   # largest prime factor
def nfs_equivalent_bits(modulus_bits: int) -> float            # RSA/FFDH
def assess_curve(curve) -> SecurityAssessment
```
This is how a key length stops being quoted as a security level. `assess_curve`
returns the achieved bits, the cofactor, and whether the order is prime.

### 3.3 `src/mathcore/curves/`

**`src/mathcore/curves/secp256k1.py`** — owns `elliptic-curves`,
`cyclic-groups-dlp`.
```
@dataclass(frozen=True)
class CurveParams: p, a, b, gx, gy, n, h, name
SECP256K1: CurveParams
def is_on_curve(x: int, y: int, curve: CurveParams = SECP256K1) -> bool
def decompress(compressed: bytes, curve=SECP256K1) -> tuple[int, int]
def verify_ecdsa(pubkey, msg_hash, r, s, *, low_s_required: bool = True) -> bool
def point_add(p1, p2, curve) -> tuple[int, int] | None
def scalar_mul(k: int, point, curve) -> tuple[int, int] | None
```
`is_on_curve` is called by every entry point that accepts a point, without
exception — that check is the difference between parsing and an invalid-curve
oracle. `low_s_required` defaults to on because malleable signatures are a
consensus hazard.

**`src/mathcore/curves/ed25519.py`** — owns `edwards-curves`.
```
ED25519: CurveParams
def verify_ed25519(pubkey: bytes, message: bytes, signature: bytes, *, strict: bool = True) -> bool
def is_small_order(point_bytes: bytes) -> bool
```
`strict` selects the cofactor-clearing convention. The parameter exists and is
documented because the conventions genuinely disagree, and a silent choice is
how two nodes reach different verdicts on the same signature.

**`src/mathcore/curves/bls12_381.py`** — owns `bilinear-pairings`.
```
BLS12_381: CurveParams
def verify_aggregate(pubkeys, message, signature) -> bool
def embedding_degree(curve) -> int          # MOV-reduction sanity check
```
Non-goal: no pairing implementation from scratch in v1; it wraps a vetted
library and provides the parameter audit.

### 3.4 `src/mathcore/harmonic/`

**`src/mathcore/harmonic/walsh_hadamard.py`** — owns `walsh-hadamard`.
```
def walsh_hadamard_transform(truth_table: Sequence[int]) -> list[int]
def nonlinearity(truth_table: Sequence[int]) -> int
def is_bent(truth_table: Sequence[int]) -> bool
def correlation_immunity(truth_table: Sequence[int]) -> int
```

**`src/mathcore/harmonic/gaussian_lattice.py`** — owns
`poisson-summation-smoothing`, `lattices-module-algebra`; consumed by
`pi-structural-uses`.
```
def discrete_gaussian_pdf(x: int, sigma: float, center: float = 0.0) -> float
def smoothing_parameter(dimension: int, epsilon: float) -> float
def validate_kem_parameters(n: int, q: int, eta: int, sigma: float) -> ParameterReport
```
`validate_kem_parameters` is the point of the module: it answers whether a
chosen width sits above the smoothing parameter, which is the check that a
scheme's proof still applies. The `pi` in the pdf is structural, not
decorative, and the docstring says why.

**`src/mathcore/harmonic/fft_spectral.py`** — owns `fft-side-channel`.
```
def spectrum(trace: Sequence[float], sample_rate: float) -> Spectrum
def correlation_power_analysis(traces, hypotheses) -> list[float]
def timing_leak_test(timings_by_secret: Mapping[Any, Sequence[float]]) -> LeakReport
```
`timing_leak_test` is what turns "this comparison is constant-time" from an
assertion into evidence: it is a distributional test over measured timings,
and it is wired into the existing `src/security/constant_time.py` test suite.

### 3.5 `src/mathcore/lattice/`

**`src/mathcore/lattice/lll.py`** — owns `lattice-reduction-lll`.
```
def lll_reduce(basis: Sequence[Sequence[int]], delta: float = 0.75) -> list[list[int]]
def gram_schmidt(basis) -> tuple[list, list]
def shortest_vector_estimate(basis) -> float
```

**`src/mathcore/lattice/hnp.py`** — owns `hidden-number-problem`. The most
operationally relevant module in this package.
```
@dataclass(frozen=True)
class SignatureSample: r: int; s: int; msg_hash: int; known_nonce_bits: int
def build_hnp_lattice(samples, curve_order) -> list[list[int]]
def recover_private_key(samples, curve_order, pubkey) -> int | None
def detect_nonce_bias(samples, curve_order) -> BiasReport
```
`detect_nonce_bias` is the defensive surface and is what
`src/ecc/ecdsa_scan.py` gains: that module already finds exact `r` reuse, which
is the easy case. Bias is the case that has actually drained wallets whose
owners saw no repeated `r` at all. `recover_private_key` is included because a
detector that cannot demonstrate the recovery cannot prove its own threshold is
right; it operates on supplied samples and never touches this project's keys.

### 3.6 `src/mathcore/coding/`

**`src/mathcore/coding/reed_solomon.py`** — owns `reed-solomon`.
```
def encode(data: Sequence[int], parity: int, field: PrimeField) -> list[int]
def decode(received, parity, field) -> list[int] | None
def sampling_confidence(samples: int, chunks: int, withheld: float) -> float
```
`sampling_confidence` is the data-availability question in the form the rest of
the system needs it: how many random samples make withholding implausible.

**`src/mathcore/coding/mds.py`** — owns `mds-matrices`, `linear-algebra-f2`.
```
def branch_number(matrix, field: BinaryField) -> int
def is_mds(matrix, field: BinaryField) -> bool
```

### 3.7 `src/mathcore/probability/`

**`src/mathcore/probability/birthday.py`** — owns `birthday-bound`.
```
def collision_probability(space_bits: int, samples: int) -> float
def safe_sample_limit(space_bits: int, max_probability: float) -> int
def nonce_reuse_budget(nonce_bits: int, max_probability: float = 2**-32) -> int
```

**`src/mathcore/probability/confirmations.py`** — owns
`poisson-block-arrival`, `gamblers-ruin-doublespend`. Directly load-bearing for
deposit and withdrawal handling.
```
def double_spend_probability(attacker_share: float, depth: int) -> float
def required_depth(attacker_share: float, max_risk: float) -> int
def confirmation_time_quantile(block_interval_s: float, depth: int, q: float) -> float
def settlement_risk(value: Decimal, attacker_share: float, depth: int) -> Decimal
```
`settlement_risk` returns expected loss in quote currency, which is the form
`src/risk/` can act on. A fixed six-confirmation rule is replaced by a function
of value at risk.

**`src/mathcore/probability/entropy.py`** — owns `shannon-entropy`.
```
def shannon_entropy(samples: Sequence[int]) -> float
def min_entropy(samples: Sequence[int]) -> float
def nist_repetition_test(samples) -> HealthResult
def nist_adaptive_proportion_test(samples) -> HealthResult
def audit_rng(source: Callable[[], int], draws: int) -> EntropyReport
```
Min-entropy is the headline number in every report because the adversary
guesses the most likely value first.

**`src/mathcore/probability/committee.py`** — owns
`chernoff-committee-bounds`.
```
def chernoff_upper_bound(n: int, p: float, threshold: float) -> float
def committee_failure_probability(size: int, adversary_share: float, quorum: float) -> float
```

**`src/mathcore/probability/reorg.py`** — owns `markov-selfish-mining`.
```
def selfish_mining_threshold(propagation_advantage: float) -> float
def reorg_depth_distribution(attacker_share: float, max_depth: int) -> list[float]
```

### 3.8 `src/mathcore/commitments/` and `derivation/`

**`src/mathcore/commitments/merkle.py`** — owns `merkle-trees`.
```
def merkle_root(leaves: Sequence[bytes], *, domain_separated: bool = True) -> bytes
def merkle_proof(leaves, index) -> list[bytes]
def verify_proof(leaf, proof, root, index) -> bool
```
`domain_separated` defaults to on. Bitcoin's duplicate-transaction quirk is the
canonical demonstration of what the other setting costs.

**`src/mathcore/derivation/bip32.py`** — owns `bip32-hd-derivation`.
```
def derive_public(xpub: str, path: str) -> str        # watch-only
def fingerprint(xpub: str) -> bytes
def validate_path(path: str) -> DerivationPolicy      # flags non-hardened accounts
```
Non-goal, and it is law L1: **no private derivation in this module.** The
xpub-plus-child-key key-recovery risk is exactly why the two must not meet in
one process.

**`src/mathcore/derivation/nonces.py`** — owns
`rfc6979-deterministic-nonces`.
```
def rfc6979_nonce(private_key: int, msg_hash: bytes, curve_order: int) -> int
def is_deterministic_signer(samples: Sequence[SignatureSample]) -> bool
def audit_signer(signer_id: str, samples) -> SignerReport
```
`audit_signer` is the preventive counterpart to `lattice/hnp.py`: detection
finds the wound, deterministic nonces stop it existing.

**`src/mathcore/derivation/threshold.py`** — owns `threshold-signatures`.
```
def frost_verify_share(share, commitment, index) -> bool
def musig2_aggregate_pubkey(pubkeys: Sequence[bytes]) -> bytes
def assess_concurrency_safety(scheme: str) -> SchemeAssessment
```
`assess_concurrency_safety` exists because the ROS attack against naive
concurrent Schnorr aggregation is the failure a reviewer must be reminded of
by the code itself.

### 3.9 `src/mathcore/constants/nutms.py`

Owns `nutms-constants`; consumed by `golden-ratio-nutms`,
`pi-structural-uses`.
```
def derive_sha256_constants() -> list[int]     # cube roots of the first 64 primes
def derive_sha1_constants() -> list[int]       # sqrt 2, 3, 5, 10
def derive_md5_constants() -> list[int]        # |sin(i)|
def derive_blowfish_p_array() -> list[int]     # hex digits of pi
def derive_golden_ratio_delta() -> int         # 0x9E3779B9, TEA and RC5
def verify_published_constants() -> VerificationReport
```
`verify_published_constants` re-derives each set from its formula and compares
against the published values. A mismatch means either the derivation is wrong
or the constant was substituted, and both are worth knowing. This is the module
that makes the "nothing up my sleeve" claim checkable here rather than merely
repeated.

### 3.10 `src/mathcore/folklore/`

The gate. Small, and the most likely of all these modules to save money.

**`src/mathcore/folklore/gate.py`**
```
class FolkloreGateError(RuntimeError): ...

def assert_not_folklore(feature_id: str) -> None
def require_validation(feature_id: str, evidence: ValidationEvidence) -> None
def scan_strategy_module(path: Path) -> list[FolkloreFinding]
```
`assert_not_folklore` raises when a feature id maps to a registry entry with
verdict `folklore` and no attached validation evidence.
`ValidationEvidence` requires: an out-of-sample window that was not used in
selection, the number of hypotheses tested, a multiple-testing correction, and
the corrected p-value. Absent any of those, the gate refuses.

**`src/mathcore/folklore/detectors.py`**
```
FOLKLORE_SIGNATURES: dict[str, list[str]]    # id -> source patterns
def detect(source: str) -> list[FolkloreFinding]
```
Static detection of Fibonacci retracement levels, Gann angles, Elliott wave
labelling and stock-to-flow fits appearing in strategy code. It reports; the
gate decides. The point is that nobody has to notice by eye during review.

---

## 4. Wiring points into the existing tree

Each row is a concrete edit to an existing file, not a new subsystem.

| Existing module | Gains | Registry entry | Why it matters here |
|---|---|---|---|
| `src/ecc/ecdsa_scan.py` | `lattice/hnp.detect_nonce_bias` | `hidden-number-problem` | It finds exact `r` reuse today. Bias is the case that empties wallets without any repeated `r`. |
| `src/ecc/schnorr_taproot.py` | `derivation/threshold.assess_concurrency_safety` | `threshold-signatures` | MuSig2 cosigner detection exists; the concurrency caveat should travel with it. |
| `src/ecc/secp256k1_cluster.py` | `curves/secp256k1.decompress`, `is_on_curve` | `elliptic-curves` | Clustering parses points; parsing without an on-curve check is the invalid-curve entry point. |
| `src/security/api_signer.py` | `derivation/nonces.audit_signer` | `rfc6979-deterministic-nonces` | Ed25519 is deterministic by construction; the audit proves it stays that way if the signer is ever swapped. |
| `src/security/constant_time.py` | `harmonic/fft_spectral.timing_leak_test` | `fft-side-channel` | Turns a constant-time claim into a measurement. |
| `src/security/pq_transport.py` | `harmonic/gaussian_lattice.validate_kem_parameters`, `fields/ntt` | `lattices-module-algebra`, `ntt` | The module is an explicit placeholder today. These are its prerequisites, and the registry says so rather than letting the placeholder read as a KEM. |
| `src/security/credential_vault.py` | `derivation/bip32.validate_path` | `bip32-hd-derivation` | Flags non-hardened account-level derivation before it becomes a custody incident. |
| `src/features/derivatives.py` | `probability/confirmations` | `poisson-block-arrival` | Settlement timing as a distribution rather than an average. |
| `src/risk/` | `probability/confirmations.settlement_risk` | `gamblers-ruin-doublespend` | Confirmation depth becomes a function of value at risk. |
| `src/strategies/` | `folklore/gate.assert_not_folklore` | all `folklore` entries | The gate is invoked at feature registration, not at order time — failing early and loudly. |
| `tools/registry.py` | read-only analysis tools registered `orchestratable=True` | — | Only pure analysis functions. Nothing that signs, spends or mutates, per the existing rule in `CLAUDE.md`. |
| `kg/` | ingest `docs/MATH_FOUNDATIONS.md` | — | Lets `kg_cli.py query` answer chained questions across the registry without re-reading it. |

---

## 5. Testing strategy

Per layer, because the layers fail differently.

**Pure numeric modules** — property-based tests with `hypothesis`, plus known-
answer tests from the standards named in each registry entry. Field arithmetic
is checked against Python's built-in big integers; the NTT against schoolbook
convolution; Tonelli-Shanks by squaring the result. A property test that
verifies an implementation against itself is worthless, so every one of these
has an independent oracle.

**Security-relevant modules** — a negative test for each `risk_if_misused`
string in the registry. That field is not prose: it is a test specification.
`is_on_curve` gets an off-curve point, `verify_ecdsa` gets a high-`s`
signature, `detect_crt_fault` gets a faulty signature and must recover the
factor.

**The HNP module** — end-to-end against synthetic signatures with a known
private key and deliberately biased nonces, at several bias widths, asserting
both recovery above the threshold and non-recovery below it. Without both
directions the threshold is decoration.

**The folklore gate** — a strategy module that imports a Fibonacci retracement
feature must fail its test suite. This is the test that keeps the whole
category out.

**Registry and governance** — already implemented in
`tests/test_math_registry.py`, and the command policy in
`tests/test_pre_tool_use_hook.py` and `tests/test_command_schema_policy.py`.

---

## 6. What is deliberately not built

Recorded so it is not proposed again.

- **No signing, no key generation, no transaction construction** in `mathcore`
  (law L1).
- **No isogeny cryptography.** Registry entry `isogenies` is `rejected`; SIKE
  fell to a classical attack in hours.
- **No from-scratch production KEM.** `ML-KEM` comes from a vetted
  implementation when `src/security/pq_transport.py` stops being a placeholder;
  `mathcore` supplies the parameter audit, not the primitive.
- **No general-purpose factoring or DLP solving.** `factorization.py` is
  bounded and exists for key-quality auditing.
- **No numerology-derived trading features**, and no exemption for them.
