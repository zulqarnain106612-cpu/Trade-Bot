# Mathematical core: roadmap

Build order for the modules specified in `docs/MATH_ARCHITECTURE.md`. Phases
are ordered by dependency first and by expected value second, so that each one
ships something usable rather than only unlocking the next.

Every phase has an **exit gate**. A phase is not finished when the code exists;
it is finished when the gate passes. A phase that cannot pass its gate is
reported as blocked, not quietly carried forward.

---

## Phase 0 — Governance (complete)

Delivered, in the branch this document arrives on.

| Item | State |
|---|---|
| `config/math_registry.json` + JSON Schema, 69 entries | done |
| `src/mathcore/registry.py` loader with strict validation | done |
| `docs/MATH_FOUNDATIONS.md` generated from the registry | done |
| `scripts/generate_math_docs.py` with `--check` for CI | done |
| `COMMAND_EXEC_SCHEMA` 1.1.0: `timeout_s`, `cwd`, `env`, `max_bytes`, `redact`, `classification` | done |
| `.claude/hooks/pre_tool_use.py` + `config/command_policy.json` | done |
| Tests: registry, schema policy, hook | done |

**Exit gate:** all four test modules green; the registry cannot claim a module
that does not exist; the generated document is byte-identical to a fresh
render. **Passed.**

---

## Phase 1 — Pure numeric foundation

Everything else depends on this, and it is the cheapest to get right because
it is entirely deterministic and has independent oracles.

**Modules:** `fields/prime_field.py`, `fields/binary_field.py`,
`numbertheory/primality.py`, `numbertheory/residues.py`,
`numbertheory/crt.py`.

**Registry entries closed:** `finite-fields`, `pseudo-mersenne-primes`,
`primality-testing-bpsw`, `lucas-sequences`, `quadratic-residues`,
`tonelli-shanks`, `chinese-remainder-theorem`.

**Exit gate**
- Known-answer tests against FIPS 197 (AES S-box inversion), FIPS 186-5
  (primality), and SEC 2 (curve field primes).
- Property tests: `inv` inverts, `sqrt` squares back, BPSW agrees with a
  reference sieve on every integer below 10^6 with no disagreement.
- `detect_crt_fault` recovers a factor from a synthetic faulty signature.
- Every public function documents whether it is constant-time.

**Risk:** a subtly wrong fast reduction is correct on random input and wrong on
a carry edge case. Mitigation: differential testing against Python big
integers over 10^6 random inputs plus every boundary value near the modulus.

---

## Phase 2 — Curves and the on-chain analytics wiring

First phase with visible product value: it hardens code that already runs.

**Modules:** `curves/secp256k1.py`, `curves/ed25519.py`.
**Wiring:** `src/ecc/secp256k1_cluster.py` and `src/ecc/ecdsa_scan.py` route
point parsing through `is_on_curve` and `decompress`.

**Registry entries closed:** `elliptic-curves`, `edwards-curves`,
`cyclic-groups-dlp`.

**Exit gate**
- BIP-340 and RFC 8032 test vectors pass, including the negative vectors.
- Every point entering `src/ecc/` is on-curve-checked; a test asserts an
  off-curve point is rejected at the boundary rather than deeper in.
- `verify_ecdsa` rejects high-`s` signatures by default.
- Ed25519 cofactor convention is explicit and tested in both settings.

---

## Phase 3 — Nonce bias: the attack that actually takes funds

Highest expected value in the roadmap. A trading system signs constantly, and
biased-nonce recovery is the failure mode with a documented history of
draining real wallets.

**Modules:** `lattice/lll.py`, `lattice/hnp.py`, `derivation/nonces.py`.
**Wiring:** `src/ecc/ecdsa_scan.py` gains bias detection alongside its existing
`r`-reuse detection; `src/security/api_signer.py` gains `audit_signer`.

**Registry entries closed:** `lattice-reduction-lll`,
`hidden-number-problem`, `rfc6979-deterministic-nonces`.

**Exit gate**
- End-to-end recovery of a known private key from synthetic signatures with
  4-bit, 2-bit and 1-bit nonce bias, and demonstrated **non**-recovery from
  unbiased RFC 6979 signatures. Both directions, or the threshold is unproven.
- `audit_signer` returns a clean report for the current Ed25519 signer.
- A documented statement of what the detector cannot see, so its output is not
  read as an all-clear it never claimed.

**Constraint:** `recover_private_key` operates only on caller-supplied samples
and has no access to this project's key material (law L1). A test asserts the
module imports nothing from `src/security/credential_vault.py`.

---

## Phase 4 — Settlement risk and confirmation depth

Converts a fixed confirmation rule into a function of value at risk. This is
where the probability work reaches the trading logic.

**Modules:** `probability/confirmations.py`, `probability/birthday.py`,
`probability/entropy.py`.
**Wiring:** `src/risk/` consumes `settlement_risk`; `src/features/derivatives.py`
consumes the confirmation-time quantiles.

**Registry entries closed:** `poisson-block-arrival`,
`gamblers-ruin-doublespend`, `birthday-bound`, `shannon-entropy`.

**Exit gate**
- `double_spend_probability` reproduces the table in Nakamoto section 11 to
  four decimal places.
- `required_depth` is monotone in both value at risk and attacker share, tested
  as a property rather than at sample points.
- `audit_rng` runs against the RNG behind any seed generation this project
  performs and reports min-entropy, not average entropy.
- A backtest comparing fixed-six-confirmations against risk-derived depth,
  reporting the capital-efficiency difference. If it shows no difference, the
  phase ships the finding and the simpler rule stays.

---

## Phase 5 — The folklore gate

Small, and the only phase that prevents a loss rather than detecting one.

**Modules:** `folklore/gate.py`, `folklore/detectors.py`.
**Wiring:** `src/strategies/` calls `assert_not_folklore` at feature
registration.

**Registry entries closed:** all `folklore` entries become enforced rather
than merely documented.

**Exit gate**
- A strategy module importing a Fibonacci retracement feature fails its tests
  with a message naming the registry entry and what evidence would be required.
- `ValidationEvidence` cannot be satisfied without an out-of-sample window, a
  hypothesis count and a multiple-testing correction. A test constructs each
  incomplete case and asserts refusal.
- `scan_strategy_module` finds every pattern in `FOLKLORE_SIGNATURES` in a
  fixture file, with no false positive on the existing strategy modules.

**Note on scope:** the gate does not ban anything outright. It requires the
same evidence any other feature needs. That distinction matters if a
golden-ratio level ever does show a real order-book clustering effect: the
path to using it exists, and it runs through evidence.

---

## Phase 6 — Post-quantum prerequisites

The phase that makes `src/security/pq_transport.py` honest.

**Modules:** `fields/ntt.py`, `harmonic/gaussian_lattice.py`.
**Wiring:** `src/security/pq_transport.py` either adopts a vetted ML-KEM
implementation or its docstring is amended to say plainly that it is not one.

**Registry entries closed:** `ntt`, `lattices-module-algebra`,
`poisson-summation-smoothing`, `pi-structural-uses`.

**Exit gate**
- NTT round-trips and matches schoolbook negacyclic convolution on random
  inputs in the ML-KEM ring.
- `validate_kem_parameters` reproduces the FIPS 203 parameter sets and flags a
  deliberately under-width Gaussian.
- `src/security/pq_transport.py` no longer reads as a working KEM when it is
  not one. **This is a required outcome of the phase, not a stretch goal:** a
  placeholder that looks like transport security is the most dangerous single
  artefact in the repository today.

---

## Phase 7 — Constants, commitments and remaining analytics

Lower urgency, completed for coverage.

**Modules:** `constants/nutms.py`, `commitments/merkle.py`,
`coding/reed_solomon.py`, `coding/mds.py`, `harmonic/walsh_hadamard.py`,
`harmonic/fft_spectral.py`, `numbertheory/factorization.py`,
`numbertheory/continued_fractions.py`, `numbertheory/dlp_bounds.py`,
`probability/committee.py`, `probability/reorg.py`,
`fields/interpolation.py`, `derivation/bip32.py`,
`derivation/threshold.py`, `curves/bls12_381.py`.

**Exit gate**
- `verify_published_constants` re-derives and matches every constant set it
  claims: SHA-256 cube roots, SHA-1 square roots, MD5 sines, Blowfish pi
  digits, and the TEA/RC5 golden-ratio delta `0x9E3779B9`.
- `merkle_root` is domain-separated by default, with a test demonstrating the
  second-preimage collision the undefended version admits.
- `timing_leak_test` is wired into the `src/security/constant_time.py` suite
  and passes on the current implementation.
- Every remaining registry entry is `implemented`, `not_applicable` or
  `rejected`. No entry is left `planned`.

---

## Sequencing rationale

Phase 3 precedes phase 4 because a key-recovery exposure outranks capital
efficiency. Phase 5 is deliberately early and cheap: it costs little and
prevents a class of loss that no later phase addresses. Phase 6 is placed after
the gate rather than before because the post-quantum threat is real but not
imminent, while the placeholder problem it fixes is present today and is
handled by that phase's required outcome.

Phase 1 and 2 could be merged if `mathcore` is only ever used for analytics.
They are kept separate because phase 2 changes code that already runs in
production, and that change deserves its own review boundary.

---

## Cross-cutting requirements

Applied to every phase, checked at every gate.

- **No phase lands with a `planned` registry entry claiming `implemented`.**
  The loader refuses it; CI runs the loader.
- **Every new module names its registry ids in its docstring** (law L4).
- **Every `risk_if_misused` string gets a negative test.** The field is a test
  specification, not commentary.
- **Regenerate `docs/MATH_FOUNDATIONS.md`** whenever the registry changes;
  `scripts/generate_math_docs.py --check` fails CI otherwise.
- **Command execution stays inside the schema.** Every shell command in a
  build, test or CI script is declared through `COMMAND_EXEC_SCHEMA` and run
  via `common/shell_exec.run()`, capped and redacted.
