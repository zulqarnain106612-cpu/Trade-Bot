<!--
GENERATED FILE — DO NOT EDIT BY HAND.

Source of truth: config/math_registry.json
Regenerate with: python3 scripts/generate_math_docs.py
CI check:        python3 scripts/generate_math_docs.py --check

Editing this file directly will be reverted by the next regeneration and will
fail CI. Change the registry instead; the registry is reviewed as a diff and
validated by src/mathcore/registry.py at load time.
-->

# Mathematical foundations

Every mathematical object that has, or is claimed to have, a role in
cryptography or cryptocurrency, with an explicit verdict on whether it does.

The list exists because the question attracts two opposite errors. One is to
assume that anything with a Greek letter is doing serious work, which is how
the golden ratio ends up in a trading strategy. The other is to dismiss
constants as decoration, which is how a structural use of pi in lattice
cryptography gets mistaken for the same kind of thing as pi in Blowfish's
S-boxes. Both errors are avoided by naming, for each object, what breaks if it
is removed.

## How to read a verdict

The dividing line throughout: **an object is doing real work only if removing
it breaks a proof.** If it can be swapped for any other value or structure of
similar size with no security consequence, it is provenance at best. If it is
being used to predict prices, it is numerology.

**LOAD-BEARING** — Removing it breaks a security proof or a protocol outright. The object is not decorative and cannot be substituted for another of similar size.

**PERFORMANCE-CRITICAL** — The security argument survives without it, but the system becomes too slow to ship. Substitutable in principle, not in practice.

**ATTACK SURFACE** — The object is how the system is broken rather than how it is built. Tracked so that defences against it are explicit rather than incidental.

**PROVENANCE ONLY** — The specific value carries no mathematical weight; it is chosen to be publicly re-derivable so that a backdoor cannot hide in it. Any other publicly derivable constant of the same size would do.

**FOLKLORE** — Claimed to matter and does not. No surviving out-of-sample evidence, or no mechanism at all. Recorded here so the claim is refuted once rather than re-argued every time it resurfaces.

## Summary by verdict

| Verdict | Entries |
|---|---|
| LOAD-BEARING | 41 |
| PERFORMANCE-CRITICAL | 6 |
| ATTACK SURFACE | 9 |
| PROVENANCE ONLY | 6 |
| FOLKLORE | 7 |

## Summary by domain

| Domain | Entries |
|---|---|
| Algebra | 11 |
| Number theory | 15 |
| Fourier and harmonic analysis | 6 |
| Probability and information theory | 6 |
| Coding theory | 4 |
| Geometry, lattices and graphs | 4 |
| Constants | 6 |
| Protocol primitives | 9 |
| Numerology and folklore | 8 |

---

## Algebra

#### Finite fields GF(p) and GF(2^n)
`finite-fields` — **LOAD-BEARING** · relevance: security · status: implemented

Provides the arithmetic every symmetric and asymmetric primitive is defined over. The AES S-box is multiplicative inversion in GF(2^8) followed by an affine map; every elliptic curve in use is defined over a prime field.

- **Used by:** AES, secp256k1, Curve25519, ML-KEM, AES-GCM, Plonk, STARKs
- **Risk if misused:** A field implementation with a data-dependent branch or a non-constant-time reduction leaks the secret scalar through timing, which turns a correct algorithm into a key-recovery oracle.
- **Owned by:** `src/mathcore/fields/__init__.py`
- **Implemented by:** `src/mathcore/fields/prime_field.py`, `src/mathcore/fields/binary_field.py`
- **Consumed by:** `src/mathcore/curves/secp256k1.py`
- **References:** FIPS 197, SEC 2 v2

#### Cyclic groups and the discrete logarithm problem
`cyclic-groups-dlp` — **LOAD-BEARING** · relevance: security · status: implemented

The hardness assumption under Diffie-Hellman, DSA, ECDSA and Schnorr. Security is exactly the difficulty of recovering x from g^x in a group of large prime order.

- **Used by:** Diffie-Hellman, ECDSA, Schnorr/BIP-340, Ed25519
- **Risk if misused:** A group whose order has small factors admits Pohlig-Hellman decomposition, reducing a 256-bit problem to several small ones and recovering the key in practice.
- **Depends on:** `finite-fields`
- **Owned by:** `src/mathcore/curves/secp256k1.py`
- **Consumed by:** `src/ecc/ecdsa_scan.py`
- **References:** SEC 1 v2, FIPS 186-5

#### Elliptic curves over finite fields
`elliptic-curves` — **LOAD-BEARING** · relevance: security · status: implemented

Supplies the group in which the discrete logarithm is hard at 256-bit key sizes rather than 3072-bit ones. Every signature this project verifies or produces lives on one.

- **Used by:** Bitcoin secp256k1, Ethereum secp256k1, Solana Ed25519, Monero, TLS P-256
- **Risk if misused:** Point arithmetic that does not validate an input point lies on the curve accepts an invalid-curve point and leaks the private scalar over repeated queries.
- **Depends on:** `finite-fields`, `cyclic-groups-dlp`
- **Owned by:** `src/mathcore/curves/secp256k1.py`
- **Consumed by:** `src/ecc/ecdsa_scan.py`, `src/ecc/schnorr_taproot.py`
- **References:** SEC 2 v2, RFC 8032, BIP-340

> Status describes the owning mathcore module, which does not exist yet. Elliptic curves are already in production use through the consumers listed here; what is planned is a single owned implementation of curve parameters and verification-only point arithmetic, so those consumers stop each carrying their own.

#### Twisted Edwards curves and Ed25519
`edwards-curves` — **LOAD-BEARING** · relevance: security · status: implemented

A curve form with a complete addition law, so there is no special case for doubling or for the identity and therefore no branch for an attacker to time. Ed25519 also fixes the nonce deterministically by construction.

- **Used by:** Ed25519, Solana, Monero, SSH host keys, this project's API request signing
- **Risk if misused:** Ed25519 has a cofactor of 8, so signature verification that does not follow one consistent convention can accept a signature another implementation rejects. For consensus-relevant checks this disagreement is a chain split, not a cosmetic difference.
- **Depends on:** `elliptic-curves`
- **Owned by:** `src/mathcore/curves/ed25519.py`
- **Consumed by:** `src/security/api_signer.py`
- **References:** RFC 8032, Chalkias et al. 2020, Taming the many EdDSAs

#### Bilinear pairings (Weil, Tate, ate)
`bilinear-pairings` — **LOAD-BEARING** · relevance: analytics · status: planned

Makes signature aggregation and constant-size polynomial commitments possible by turning a multiplicative relation in one group into an equality check in a target group.

- **Used by:** BLS12-381, Ethereum consensus BLS aggregation, KZG commitments, EIP-4844
- **Risk if misused:** A curve with a small embedding degree collapses the elliptic-curve DLP into a finite-field DLP via the MOV reduction, destroying the security margin.
- **Depends on:** `elliptic-curves`
- **Owned by:** `src/mathcore/curves/bls12_381.py`
- **References:** IETF draft-irtf-cfrg-pairing-friendly-curves, EIP-4844

#### Lattices and module algebra over polynomial rings
`lattices-module-algebra` — **LOAD-BEARING** · relevance: security · status: planned

The hardness base of the standardised post-quantum schemes: arithmetic in Z_q[x]/(x^n+1) with Learning With Errors as the assumption.

- **Used by:** ML-KEM (Kyber), ML-DSA (Dilithium), Falcon, NTRU
- **Risk if misused:** Sampling error terms from a distribution that is not the intended discrete Gaussian breaks the worst-case-to-average-case reduction, so the scheme has no proof left even though it still interoperates.
- **Owned by:** `src/mathcore/harmonic/gaussian_lattice.py`
- **Consumed by:** `src/security/pq_transport.py`
- **References:** FIPS 203, FIPS 204

> src/security/pq_transport.py is documented in-tree as a placeholder. Registering this entry as 'planned' rather than 'implemented' is the honest state; treating the placeholder as a KEM would be the single most dangerous misreading in this repository.

#### Linear algebra over F_2
`linear-algebra-f2` — **LOAD-BEARING** · status: planned

The language of linear and differential cryptanalysis and of diffusion design; MixColumns is a matrix multiplication chosen for its branch number.

- **Used by:** AES MixColumns, linear cryptanalysis, MDS matrix construction
- **Depends on:** `finite-fields`
- **Owned by:** `src/mathcore/coding/mds.py`

#### GF(2^128) multiplication in Galois/Counter Mode
`galois-field-gcm` — **LOAD-BEARING** · relevance: security · status: not_applicable

GHASH authentication is polynomial evaluation in GF(2^128) modulo x^128+x^7+x^2+x+1. Every TLS record this bot exchanges with an exchange is authenticated this way.

- **Used by:** AES-GCM, TLS 1.3, every exchange REST and WebSocket session
- **Risk if misused:** Reusing an IV under one GCM key reveals the authentication subkey and lets an attacker forge arbitrary authenticated messages; this is a total break, not a degradation.
- **Depends on:** `finite-fields`
- **References:** NIST SP 800-38D

> Owned by the TLS stack, not by this project. Registered so that nobody implements GCM here: the correct action is to use the platform's.

#### Isogeny graphs of supersingular elliptic curves
`isogenies` — **ATTACK SURFACE** · status: rejected

A post-quantum hardness candidate whose flagship instance was broken, which is exactly why it is tracked: it is the cleanest available lesson about betting on a young assumption.

- **Used by:** SIDH/SIKE (broken 2022), CSIDH, SQIsign
- **Risk if misused:** SIKE was a NIST alternate candidate and fell to a classical attack in hours on one core. Adopting an isogeny scheme for key exchange here would take on that class of risk for no benefit over the standardised lattice KEM.
- **Depends on:** `elliptic-curves`
- **References:** Castryck-Decru 2022

#### Lagrange interpolation over a finite field
`lagrange-interpolation` — **LOAD-BEARING** · relevance: security · status: planned

A degree-t polynomial is determined by t+1 points and by no fewer, which is precisely the threshold property behind secret sharing and threshold signing.

- **Used by:** Shamir secret sharing, threshold ECDSA custody, FROST, polynomial IOPs
- **Risk if misused:** Sharing with a polynomial whose coefficients are not uniformly random, or reusing one across secrets, leaks the secret to a sub-threshold set of shareholders.
- **Depends on:** `finite-fields`
- **Owned by:** `src/mathcore/fields/interpolation.py`
- **References:** Shamir 1979

#### Schwartz-Zippel lemma
`schwartz-zippel` — **LOAD-BEARING** · status: not_applicable

Two distinct low-degree polynomials agree on only a vanishing fraction of a large field, so checking one random point is a sound proof. This is the reason a succinct proof can be succinct at all.

- **Used by:** Groth16, Plonk, STARKs, every polynomial IOP
- **Depends on:** `finite-fields`
- **References:** Schwartz 1980, Zippel 1979

## Number theory

#### Integer factorisation and RSA
`prime-factorisation-rsa` — **LOAD-BEARING** · relevance: security · status: planned

RSA security is the difficulty of recovering p and q from n = pq. Still present in exchange API authentication and in certificate chains.

- **Used by:** RSA-2048 TLS certificates, JWT RS256, legacy exchange API signing
- **Risk if misused:** Keys generated from a low-entropy pool share prime factors across the population, and a batch GCD over public keys recovers the private keys of every affected party at once.
- **Owned by:** `src/mathcore/numbertheory/factorization.py`
- **References:** RFC 8017, Heninger et al. 2012, Mining Your Ps and Qs

#### Safe primes and prime-order subgroups
`safe-primes-dh` — **LOAD-BEARING** · relevance: security · status: planned

Choosing p = 2q+1 with q prime removes the small subgroups that would otherwise let an attacker confine a shared secret to a tiny set of values.

- **Used by:** finite-field Diffie-Hellman, DSA, TLS ffdhe groups
- **Risk if misused:** Unvalidated DH parameters allow a small-subgroup confinement attack in which the shared secret takes one of a handful of values.
- **Depends on:** `cyclic-groups-dlp`
- **Consumed by:** `src/mathcore/numbertheory/primality.py`
- **References:** RFC 7919

#### Pseudo-Mersenne and Solinas field primes
`pseudo-mersenne-primes` — **PERFORMANCE-CRITICAL** · relevance: security · status: implemented

Field primes of the shape 2^k - c admit reduction by shift-and-add instead of division, which is why these curves are fast enough for per-transaction verification.

- **Used by:** secp256k1 (2^256 - 2^32 - 977), Curve25519 (2^255 - 19), NIST P-256
- **Risk if misused:** A hand-rolled fast reduction that is correct on random inputs but wrong on a narrow carry case produces a signature that verifies for the attacker's chosen message.
- **Depends on:** `finite-fields`
- **Owned by:** `src/mathcore/fields/prime_field.py`
- **References:** SEC 2 v2, RFC 7748

#### Baillie-PSW primality testing (Miller-Rabin plus strong Lucas)
`primality-testing-bpsw` — **LOAD-BEARING** · relevance: security · status: implemented

The actual gate every generated RSA or DH prime passes. Combines a base-2 strong probable-prime test with a strong Lucas test, whose sequences are the Fibonacci family.

- **Used by:** OpenSSL key generation, GnuPG, every RSA keygen in practice
- **Risk if misused:** Too few Miller-Rabin rounds, or rounds with attacker-chosen bases, accepts a composite as prime and yields a modulus that factors trivially.
- **Depends on:** `lucas-sequences`
- **Owned by:** `src/mathcore/numbertheory/primality.py`
- **References:** Baillie-Wagstaff 1980, FIPS 186-5 Appendix B

#### Euler's theorem and the totient function phi(n)
`euler-theorem-totient` — **LOAD-BEARING** · status: not_applicable

Supplies RSA correctness: m^(ed) = m mod n when ed = 1 mod lambda(n). Without it RSA decryption does not invert encryption.

- **Used by:** RSA, Paillier
- **Depends on:** `prime-factorisation-rsa`

> Euler's totient is written phi(n) and has nothing to do with the golden ratio, also written phi. This symbol collision is a recurring source of the belief that the golden ratio underpins RSA. It does not.

#### Chinese Remainder Theorem
`chinese-remainder-theorem` — **PERFORMANCE-CRITICAL** · relevance: security · status: implemented

Splits an RSA private operation into two half-size operations modulo p and q, roughly a fourfold speedup.

- **Used by:** RSA-CRT, residue number system arithmetic
- **Risk if misused:** A single bit-flip during one CRT half, whether from a fault injection or bad RAM, lets an observer factor the modulus from one faulty signature by a GCD. Verify every CRT signature before releasing it.
- **Depends on:** `prime-factorisation-rsa`
- **Owned by:** `src/mathcore/numbertheory/crt.py`
- **References:** Boneh-DeMillo-Lipton 1997

#### Quadratic residues, Legendre and Jacobi symbols
`quadratic-residues` — **LOAD-BEARING** · relevance: security · status: implemented

Decides whether a square root exists in a prime field, which is what makes a compressed public key decompressible and underpins the Rabin and Goldwasser-Micali constructions.

- **Used by:** compressed EC point decoding, Rabin cryptosystem, Goldwasser-Micali
- **Risk if misused:** Accepting a decompressed point without checking the residue condition admits an off-curve point, which is the entry point for invalid-curve key extraction.
- **Depends on:** `finite-fields`
- **Owned by:** `src/mathcore/numbertheory/residues.py`

#### Tonelli-Shanks square roots
`tonelli-shanks` — **LOAD-BEARING** · relevance: analytics · status: implemented

Computes the y coordinate from x when decompressing a public key. Executed on every compressed key this project parses.

- **Used by:** secp256k1 point decompression, BLS hash-to-curve
- **Depends on:** `quadratic-residues`
- **Owned by:** `src/mathcore/numbertheory/residues.py`

#### Class groups of imaginary quadratic fields
`class-groups-vdf` — **LOAD-BEARING** · status: not_applicable

Provides a group of unknown order without a trusted setup, which is what a verifiable delay function needs to be both slow to compute and fast to verify.

- **Used by:** Wesolowski VDF, Pietrzak VDF, Chia consensus
- **References:** Wesolowski 2019, Boneh et al. 2018

#### Hasse's theorem and Schoof's point-counting algorithm
`hasse-schoof` — **LOAD-BEARING** · status: not_applicable

Bounds and then computes the order of a curve group, which is the number that must be prime for the curve to be usable at all.

- **Used by:** curve parameter generation, SEC/NIST curve validation
- **Depends on:** `elliptic-curves`

#### Pollard rho, baby-step giant-step, Pohlig-Hellman, index calculus
`generic-dlp-algorithms` — **ATTACK SURFACE** · relevance: security · status: planned

Set the actual security level of a group: square-root generic attacks are why a 256-bit group gives 128-bit security, and Pohlig-Hellman is why the order must be prime.

- **Used by:** security-level analysis of every deployed curve
- **Risk if misused:** Quoting a key's bit length as its security level overstates safety whenever the group order has small factors or a cofactor is mishandled.
- **Depends on:** `cyclic-groups-dlp`
- **Owned by:** `src/mathcore/numbertheory/dlp_bounds.py`

#### General Number Field Sieve
`number-field-sieve` — **ATTACK SURFACE** · relevance: security · status: planned

Subexponential L_n[1/3] factorisation, which is the entire reason RSA needs 2048 or 3072 bits where an elliptic curve needs 256.

- **Used by:** RSA key-size selection, finite-field DH key-size selection
- **Risk if misused:** Comparing an RSA modulus size with an elliptic-curve key size as if the same attack applied to both leads to keeping RSA-1024 alongside a 256-bit curve and believing they match.
- **Depends on:** `prime-factorisation-rsa`
- **Owned by:** `src/mathcore/numbertheory/dlp_bounds.py`
- **References:** NIST SP 800-57 Part 1

#### Continued fractions and Wiener's attack
`continued-fractions` — **ATTACK SURFACE** · relevance: security · status: planned

The convergents of e/n recover a small RSA private exponent directly. Also the source of the claim that the golden ratio is the most irrational number, its expansion being all ones.

- **Used by:** Wiener's attack, Boneh-Durfee extension, low-discrepancy sequence theory
- **Risk if misused:** Choosing a small private exponent d to speed up decryption exposes the key outright when d is below roughly n^0.25.
- **Owned by:** `src/mathcore/numbertheory/continued_fractions.py`
- **References:** Wiener 1990, Boneh-Durfee 1999

#### Zero-knowledge-friendly field primes
`zk-friendly-primes` — **PERFORMANCE-CRITICAL** · status: not_applicable

Primes chosen so that a large power of two divides p-1, giving the roots of unity an NTT needs, with reduction that costs a few instructions.

- **Used by:** Goldilocks 2^64-2^32+1, BabyBear, Mersenne31, Plonky3, Circle STARKs
- **Depends on:** `finite-fields`

> The primes are selected so that a large power of two divides p-1, which is an NTT requirement; the dependency runs from the transform to the prime, not the other way, so ntt depends_on this entry and not the reverse.

#### Lucas sequences
`lucas-sequences` — **LOAD-BEARING** · relevance: security · status: implemented

The second half of the Baillie-PSW primality test. This is the one place where a Fibonacci-family object does real cryptographic work.

- **Used by:** Baillie-PSW, Lucas-Lehmer testing
- **Risk if misused:** Implementing the ordinary rather than the strong Lucas test weakens the composite-rejection guarantee that the combined test relies on.
- **Owned by:** `src/mathcore/numbertheory/primality.py`

> Frequently mistaken for evidence that the golden ratio matters in cryptography. The growth rate of these sequences is the golden ratio, but the test depends on their recurrence structure modulo n, not on that limit.

## Fourier and harmonic analysis

#### Number-theoretic transform
`ntt` — **PERFORMANCE-CRITICAL** · relevance: security · status: planned

The FFT carried out over a finite field. It turns polynomial multiplication from quadratic to n log n, which is what makes lattice cryptography and succinct proofs fast enough to deploy.

- **Used by:** ML-KEM, ML-DSA, Plonk, STARK provers, Falcon
- **Risk if misused:** A butterfly loop with a secret-dependent memory access pattern leaks the secret polynomial through cache timing, which is a published attack class against lattice KEMs, not a theoretical one.
- **Depends on:** `finite-fields`, `zk-friendly-primes`
- **Owned by:** `src/mathcore/fields/ntt.py`
- **Consumed by:** `src/security/pq_transport.py`
- **References:** FIPS 203 Algorithm 9

#### Walsh-Hadamard transform (Fourier analysis on the Boolean cube)
`walsh-hadamard` — **LOAD-BEARING** · status: planned

Defines S-box nonlinearity, correlation immunity and bent functions. It is the measurement that says whether a substitution box resists linear cryptanalysis.

- **Used by:** AES S-box analysis, stream cipher filter design, bent function construction
- **Depends on:** `linear-algebra-f2`
- **Owned by:** `src/mathcore/harmonic/walsh_hadamard.py`

#### Poisson summation and the lattice smoothing parameter
`poisson-summation-smoothing` — **LOAD-BEARING** · relevance: security · status: planned

Fourier series on a lattice: it is how the discrete Gaussian is shown to behave like a continuous one above the smoothing parameter, which is the step every lattice security proof depends on.

- **Used by:** ML-KEM security proof, ML-DSA, Falcon sampler, Regev's LWE reduction
- **Risk if misused:** Choosing a Gaussian width below the smoothing parameter voids the reduction, so the scheme retains its interface and loses its proof without any visible symptom.
- **Depends on:** `lattices-module-algebra`
- **Owned by:** `src/mathcore/harmonic/gaussian_lattice.py`
- **References:** Micciancio-Regev 2007, Regev 2005

> This is where pi does structural rather than decorative work: the discrete Gaussian is defined as exp(-pi|x|^2/s^2) precisely so it is its own Fourier transform.

#### Quantum Fourier transform and Shor's algorithm
`quantum-fourier-shor` — **ATTACK SURFACE** · relevance: security · status: planned

Period finding by quantum Fourier transform breaks factoring and discrete logarithms alike. The Fourier transform is simultaneously what builds modern cryptography and what would destroy the currently deployed generation of it.

- **Used by:** the entire post-quantum migration rationale
- **Risk if misused:** Harvest-now-decrypt-later: traffic and on-chain signatures captured today are decryptable once a cryptographically relevant quantum computer exists. Long-lived secrets need migrating before that date, not after.
- **Depends on:** `ntt`
- **References:** Shor 1997, NIST IR 8547

#### Spectral analysis of side channels
`fft-side-channel` — **ATTACK SURFACE** · relevance: security · status: planned

Power, electromagnetic and timing traces are correlated in the frequency domain; the FFT is what makes differential power analysis tractable over long traces.

- **Used by:** DPA and CPA against hardware wallets, timing analysis of signing code
- **Risk if misused:** Asserting that a comparison is constant-time without measuring it. The only evidence that matters is a distribution of timings that does not separate by secret value.
- **Owned by:** `src/mathcore/harmonic/fft_spectral.py`
- **Consumed by:** `src/security/constant_time.py`
- **References:** Kocher et al. 1999

#### FFT-based big-integer multiplication
`fft-bignum-multiplication` — **PERFORMANCE-CRITICAL** · status: not_applicable

Schoenhage-Strassen and its successors make very large modular arithmetic practical, which matters for large RSA moduli and for class-group VDFs.

- **Used by:** GMP, OpenSSL large-modulus paths
- **Depends on:** `ntt`

## Probability and information theory

#### Birthday bound
`birthday-bound` — **LOAD-BEARING** · relevance: security · status: planned

Sets collision resistance at half the digest length and governs how often a nonce or IV may be reused before collision becomes likely.

- **Used by:** SHA-256 128-bit collision resistance, GCM IV limits, Merkle tree security
- **Risk if misused:** Assuming a 256-bit hash gives 256-bit collision resistance leads to accepting truncated digests that are within reach of a birthday search.
- **Owned by:** `src/mathcore/probability/birthday.py`
- **References:** NIST SP 800-107

#### Shannon entropy and min-entropy
`shannon-entropy` — **LOAD-BEARING** · relevance: security · status: planned

The measure that decides whether a seed, a nonce or a key is actually unpredictable. Min-entropy, not Shannon entropy, is the correct measure for key material because the adversary guesses the most likely value first.

- **Used by:** BIP-39 seed generation, RNG validation, NIST SP 800-90B health tests
- **Risk if misused:** Quoting average Shannon entropy for a skewed source overstates unpredictability; a source with 128 bits of Shannon entropy and 30 bits of min-entropy is brute-forceable.
- **Owned by:** `src/mathcore/probability/entropy.py`
- **References:** NIST SP 800-90B

#### Poisson block arrival
`poisson-block-arrival` — **LOAD-BEARING** · relevance: strategy · status: planned

Proof-of-work block discovery is a memoryless process, so inter-arrival times are exponential. Every confirmation-time estimate and every difficulty-adjustment control loop rests on this.

- **Used by:** Bitcoin, every Nakamoto-style chain
- **Risk if misused:** Treating a ten-minute average as a ten-minute guarantee. The exponential tail means an hour-long gap is unremarkable, and a deposit-timing model that assumes otherwise will mis-size positions during confirmation waits.
- **Owned by:** `src/mathcore/probability/confirmations.py`
- **Consumed by:** `src/features/derivatives.py`
- **References:** Nakamoto 2008 section 11

#### Gambler's ruin and double-spend probability
`gamblers-ruin-doublespend` — **LOAD-BEARING** · relevance: strategy · status: planned

The random-walk argument that gives the probability an attacker with hash-rate fraction q catches up from z blocks behind. This is the actual mathematics behind 'wait six confirmations'.

- **Used by:** Bitcoin whitepaper section 11, exchange deposit-crediting policy
- **Risk if misused:** Using a fixed confirmation count regardless of transfer size and current hash-rate distribution. The required depth is a function of value at risk and attacker share, not a constant.
- **Owned by:** `src/mathcore/probability/confirmations.py`
- **Consumed by:** `src/risk/`
- **References:** Nakamoto 2008 section 11, Rosenfeld 2014

#### Chernoff and Hoeffding bounds
`chernoff-committee-bounds` — **LOAD-BEARING** · relevance: analytics · status: planned

Bound the chance that a randomly sampled committee is adversarially controlled, which is what makes sampled-committee consensus safe at all.

- **Used by:** Algorand, Ethereum attestation committees, data availability sampling
- **Owned by:** `src/mathcore/probability/committee.py`
- **References:** Chen-Micali 2019

#### Markov models of mining strategy
`markov-selfish-mining` — **LOAD-BEARING** · relevance: strategy · status: planned

Determines the hash-rate share above which withholding blocks becomes more profitable than honest mining, and therefore where a chain's incentive assumption stops holding.

- **Used by:** selfish-mining analysis, chain reorganisation risk assessment
- **Owned by:** `src/mathcore/probability/reorg.py`
- **References:** Eyal-Sirer 2014

## Coding theory

#### Reed-Solomon codes
`reed-solomon` — **LOAD-BEARING** · relevance: analytics · status: planned

Erasure coding that lets a small random sample certify that a large block was published, which is the mechanism behind data availability sampling.

- **Used by:** EIP-4844 blobs, Danksharding, Celestia, FRI low-degree testing
- **Depends on:** `finite-fields`, `lagrange-interpolation`
- **Owned by:** `src/mathcore/coding/reed_solomon.py`
- **References:** EIP-4844

#### Binary Goppa codes
`goppa-codes` — **LOAD-BEARING** · status: not_applicable

The hardness base of Classic McEliece, the oldest unbroken post-quantum assumption and the conservative hedge against a lattice break.

- **Used by:** Classic McEliece
- **References:** Classic McEliece NIST submission

#### MDS matrices and branch number
`mds-matrices` — **LOAD-BEARING** · status: planned

Guarantees a minimum number of active S-boxes per round, converting a coding-theory bound directly into a differential-cryptanalysis bound.

- **Used by:** AES MixColumns, Poseidon, Rescue
- **Depends on:** `linear-algebra-f2`
- **Owned by:** `src/mathcore/coding/mds.py`

#### FRI low-degree proximity testing
`fri-proximity-testing` — **LOAD-BEARING** · status: not_applicable

Proves a committed vector is close to a low-degree polynomial without a trusted setup, which is what makes STARKs transparent.

- **Used by:** STARKs, Plonky2, Circle STARKs
- **Depends on:** `reed-solomon`, `ntt`

## Geometry, lattices and graphs

#### LLL and BKZ lattice reduction
`lattice-reduction-lll` — **ATTACK SURFACE** · relevance: security · status: planned

The workhorse of practical lattice attacks. Sets the concrete security of every lattice scheme and, combined with the hidden number problem, recovers ECDSA keys from biased nonces.

- **Used by:** ML-KEM parameter selection, ECDSA nonce-bias key recovery, Coppersmith attacks on RSA
- **Risk if misused:** Underestimating BKZ progress when choosing lattice parameters produces a scheme that is secure on paper against yesterday's reduction and not against today's.
- **Owned by:** `src/mathcore/lattice/lll.py`
- **References:** Lenstra-Lenstra-Lovasz 1982, Albrecht et al. lattice estimator

#### Hidden number problem and biased-nonce key recovery
`hidden-number-problem` — **ATTACK SURFACE** · relevance: security · status: planned

A handful of ECDSA signatures whose nonces share even a few known bits yield the private key by lattice reduction. This is the single most directly relevant attack in this registry for a system that signs.

- **Used by:** PS3 key recovery, repeated real-world Bitcoin wallet thefts, Minerva and TPM-Fail
- **Risk if misused:** Any signer whose nonce generation is not RFC 6979 deterministic, or whose RNG is biased, leaks its key over enough signatures. Detection is necessary but the fix is deterministic nonces, not monitoring.
- **Depends on:** `lattice-reduction-lll`, `elliptic-curves`
- **Owned by:** `src/mathcore/lattice/hnp.py`
- **Consumed by:** `src/ecc/ecdsa_scan.py`
- **References:** Boneh-Venkatesan 1996, RFC 6979, Breitner-Heninger 2019

#### Expander and Ramanujan graphs
`expander-graphs` — **LOAD-BEARING** · status: not_applicable

Rapid mixing is what makes a random walk in a supersingular isogeny graph, or a gossip network's propagation, reach a uniform distribution quickly.

- **Used by:** isogeny-based hash functions, peer-to-peer gossip analysis
- **Depends on:** `isogenies`

#### Merkle trees and DAGs
`merkle-trees` — **LOAD-BEARING** · relevance: analytics · status: planned

Logarithmic-size membership proofs against a constant-size commitment. Every SPV proof, state root and content-addressed store is one.

- **Used by:** Bitcoin block headers, Ethereum state trie, IPFS, certificate transparency
- **Risk if misused:** A tree that does not domain-separate leaves from internal nodes admits second-preimage attacks; Bitcoin's duplicate-transaction quirk is the well-known instance of getting this subtly wrong.
- **Depends on:** `birthday-bound`
- **Owned by:** `src/mathcore/commitments/merkle.py`
- **References:** RFC 6962

## Constants

#### Nothing-up-my-sleeve constants
`nutms-constants` — **PROVENANCE ONLY** · relevance: security · status: planned

Constants derived from a published formula so that no backdoor can hide in them. The value is irrelevant; the auditability is the whole point. MD5 uses sines, SHA-1 uses square roots, SHA-256 uses cube roots of small primes, Blowfish uses the digits of pi, BLAKE uses pi, RC5 uses e and the golden ratio, ARIA uses 1/pi.

- **Used by:** MD5, SHA-1, SHA-2, Blowfish, BLAKE2, RC5, RC6, TEA, ARIA
- **Risk if misused:** An unexplained constant is a place a backdoor can live. Dual_EC_DRBG's unexplained points were exactly that, and secp256k1's generator point has no published derivation either, which is a real if largely theoretical criticism of Bitcoin's curve.
- **Owned by:** `src/mathcore/constants/nutms.py`
- **References:** FIPS 180-4, RFC 7539, Bernstein et al., How to manipulate curve standards

#### Pi where it is structural rather than decorative
`pi-structural-uses` — **LOAD-BEARING** · relevance: security · status: planned

In the discrete Gaussian exp(-pi|x|^2/s^2) the pi is not cosmetic: it makes the Gaussian self-dual under the Fourier transform, which is the property Poisson summation needs for the smoothing-parameter argument.

- **Used by:** ML-KEM security analysis, ML-DSA, Falcon
- **Risk if misused:** Dropping the pi from the Gaussian definition when porting a formula changes the width by a constant factor and silently moves the scheme below its smoothing parameter.
- **Depends on:** `poisson-summation-smoothing`
- **Consumed by:** `src/mathcore/harmonic/gaussian_lattice.py`

#### e and the prime number theorem
`e-prime-density` — **LOAD-BEARING** · status: not_applicable

The density of primes near n is about 1/ln(n), which sets how many candidates a keygen must test. For 2048-bit RSA that is roughly one in seven hundred odd candidates.

- **Used by:** RSA key generation cost models, DH parameter generation
- **Depends on:** `primality-testing-bpsw`

#### Golden ratio as a nothing-up-my-sleeve constant
`golden-ratio-nutms` — **PROVENANCE ONLY** · status: not_applicable

The value 0x9E3779B9 is floor(2^32/phi) and appears as the round constant delta in TEA, XTEA and XXTEA, as Q32 in RC5 and RC6, and as the multiplier in Knuth's Fibonacci hashing and splitmix64. It is a provenance choice and contributes no security property.

- **Used by:** TEA, XTEA, RC5, RC6, Knuth multiplicative hashing, splitmix64

> This entry exists to state the accurate version of a claim that is usually made inaccurately in both directions. The golden ratio does appear in real ciphers; it appears there interchangeably with pi or any other publicly derivable irrational, and it carries no cryptographic weight.

#### Fibonacci hashing and low-discrepancy sequences
`fibonacci-hashing` — **PROVENANCE ONLY** · relevance: infrastructure · status: not_applicable

Multiplying by 2^k/phi spreads hash keys evenly because phi's continued fraction is all ones, making it the worst-approximable irrational. Useful for hash tables and quasi-random sampling; not a cryptographic property.

- **Used by:** hash table index derivation, R2 low-discrepancy sequences, quasi-Monte Carlo sampling

> Legitimate but non-cryptographic. Never use a low-discrepancy sequence where unpredictability is required: it is designed to be evenly spread, which is the opposite of random.

#### Fibonacci and Galois LFSR configurations
`lfsr-fibonacci-galois` — **PROVENANCE ONLY** · status: not_applicable

Two equivalent wirings of a linear feedback shift register. The names honour Fibonacci and Galois; only the Galois connection is mathematical.

- **Used by:** A5/1, E0, legacy stream ciphers

> Registered because the name is a common source of the belief that Fibonacci numbers appear in stream ciphers. The configuration is a wiring diagram, not a Fibonacci sequence.

## Protocol primitives

#### BIP-32 hierarchical deterministic key derivation
`bip32-hd-derivation` — **LOAD-BEARING** · relevance: security · status: planned

Derives an unbounded key tree from one seed using HMAC-SHA512 and scalar addition on the curve. It is how a single backed-up seed controls every address a bot uses.

- **Used by:** every HD wallet, exchange deposit address generation
- **Risk if misused:** An extended public key plus any one non-hardened child private key reveals the parent private key and therefore the whole branch. Use hardened derivation at account level, and never let an xpub and a child key meet.
- **Depends on:** `elliptic-curves`
- **Owned by:** `src/mathcore/derivation/bip32.py`
- **Consumed by:** `src/security/credential_vault.py`
- **References:** BIP-32, BIP-44, SLIP-0010

#### RFC 6979 deterministic nonce generation
`rfc6979-deterministic-nonces` — **LOAD-BEARING** · relevance: security · status: planned

Derives the ECDSA nonce deterministically from the key and message, removing the RNG from the signing path entirely and with it the entire biased-nonce attack class.

- **Used by:** Bitcoin Core signing, Ed25519 by construction, most modern signing libraries
- **Risk if misused:** This is the fix for the hidden number problem. Any signing path that reintroduces a random nonce reintroduces the entire attack class, however good the RNG is believed to be.
- **Depends on:** `hidden-number-problem`
- **Owned by:** `src/mathcore/derivation/nonces.py`
- **Consumed by:** `src/security/api_signer.py`
- **References:** RFC 6979, RFC 8032

#### Threshold signatures and secret sharing
`threshold-signatures` — **LOAD-BEARING** · relevance: security · status: planned

Splits signing authority so that no single machine holds a spendable key, which is the only structural defence against a compromised trading host.

- **Used by:** FROST, MuSig2, institutional MPC custody
- **Risk if misused:** Naive multi-round threshold Schnorr is vulnerable to concurrent-session attacks (the Wagner ROS attack). Use a scheme with proven concurrent security, not an ad-hoc aggregation.
- **Depends on:** `lagrange-interpolation`, `elliptic-curves`
- **Owned by:** `src/mathcore/derivation/threshold.py`
- **Consumed by:** `src/ecc/schnorr_taproot.py`
- **References:** RFC 9591 FROST, MuSig2

#### Merkle-Damgard and sponge constructions
`hash-constructions` — **LOAD-BEARING** · relevance: security · status: not_applicable

The two structural families behind deployed hashes. The distinction is operational: Merkle-Damgard admits length extension, sponge does not.

- **Used by:** SHA-256, SHA-3/Keccak, Ethereum keccak256
- **Risk if misused:** Authenticating with a bare SHA-256 of secret concatenated with message permits length-extension forgery. Use HMAC or a sponge-based MAC instead.
- **References:** FIPS 202, RFC 2104

#### Arithmetisation-friendly hashes (Poseidon, Rescue)
`zk-friendly-hashes` — **PERFORMANCE-CRITICAL** · status: not_applicable

Hashes built from low-degree power maps over a prime field so that proving a hash inside a circuit costs a handful of constraints instead of tens of thousands.

- **Used by:** Poseidon, Rescue-Prime, zk rollup state trees
- **Depends on:** `finite-fields`, `mds-matrices`

#### KZG polynomial commitments
`kzg-commitments` — **LOAD-BEARING** · status: not_applicable

Constant-size commitment to a polynomial with constant-size opening proofs, at the cost of a trusted setup. The commitment scheme behind blob data availability.

- **Used by:** EIP-4844, Plonk, Ethereum Danksharding
- **Depends on:** `bilinear-pairings`, `lagrange-interpolation`

#### Verifiable random functions
`verifiable-random-functions` — **LOAD-BEARING** · relevance: analytics · status: not_applicable

Produces a pseudorandom output with a proof that it was computed correctly, which is how a chain selects leaders without a bias-able beacon.

- **Used by:** Algorand sortition, Cardano Ouroboros Praos, Chainlink VRF
- **References:** RFC 9381

#### Grover's algorithm and symmetric key margins
`grover-hash-margins` — **ATTACK SURFACE** · relevance: security · status: planned

A quadratic speedup on unstructured search, which halves the effective strength of symmetric keys and hash preimage resistance and is why AES-256 rather than AES-128 is the post-quantum recommendation.

- **Used by:** post-quantum symmetric parameter selection
- **Risk if misused:** Migrating asymmetric primitives to post-quantum schemes while leaving 128-bit symmetric keys in place leaves the weakest link untouched.
- **References:** Grover 1996, NIST IR 8547

#### Protocol magic numbers
`protocol-magic-numbers` — **PROVENANCE ONLY** · relevance: infrastructure · status: not_applicable

Arbitrary tags for framing and replay separation: Bitcoin's network magic, chain IDs, version bits. They look like numerology and are pure engineering.

- **Used by:** Bitcoin P2P framing, EIP-155 chain IDs

> Registered to draw the line clearly: an arbitrary constant that separates namespaces is engineering. An arbitrary constant claimed to predict price is not.

## Numerology and folklore

#### Bitcoin's 21 million supply cap
`supply-schedule-geometric-series` — **PROVENANCE ONLY** · status: not_applicable

The cap is 210000 blocks times 50 BTC times the sum of a halving geometric series. It is arithmetic with a chosen parameter, not a mystical constant.

- **Used by:** Bitcoin issuance schedule

> Included in the numerology domain because it is routinely presented as one, and the refutation is a one-line calculation.

#### Fibonacci retracement levels
`fibonacci-retracement` — **FOLKLORE** · relevance: strategy · status: rejected

Claims that price reverses at ratios derived from the golden ratio. No mechanism, and no out-of-sample edge that survives correction for the number of levels and lookbacks tested.

- **Used by:** retail technical analysis, most charting platforms by default
- **Risk if misused:** Its levels are popular enough to occasionally show real order-book clustering, which is self-fulfilling reflexivity rather than a property of the ratio. Treating that as predictive is how a strategy overfits a coincidence and sizes into it.

> If any strategy proposes these levels as a feature, src/mathcore/folklore/gate.py requires it to clear the same walk-forward and multiple-testing bar as any other feature. There is no exemption for being 'natural'.

#### Elliott wave theory
`elliott-wave` — **FOLKLORE** · relevance: strategy · status: rejected

Claims price moves in nested five-and-three wave patterns with golden-ratio proportions. Unfalsifiable in practice: wave counts are relabelled after the fact.

- **Used by:** retail technical analysis
- **Risk if misused:** A model that can be relabelled to fit any outcome produces no testable signal, so backtests of it measure the analyst's hindsight rather than the market.

#### Gann angles and squares
`gann-methods` — **FOLKLORE** · relevance: strategy · status: rejected

Claims price and time relate through fixed geometric angles. Requires an arbitrary choice of scale, so the angles are not scale-invariant and mean nothing across instruments.

- **Used by:** retail technical analysis
- **Risk if misused:** The scale dependence means the same data yields different signals under a different axis choice, which is the definition of a non-signal.

#### Stock-to-flow price model
`stock-to-flow` — **FOLKLORE** · relevance: strategy · status: rejected

Claimed a deterministic relationship between scarcity and price. Its econometrics were invalid: the cointegration argument did not hold, and it failed badly out of sample after 2021.

- **Used by:** crypto market commentary
- **Risk if misused:** The most dangerous entry in this domain because it was dressed in regression output. Statistical presentation is not statistical validity; check stationarity and out-of-sample behaviour before trusting any such fit.
- **References:** Contested; see published critiques of the cointegration claim

#### Rainbow charts and power-law price models
`rainbow-and-power-law-charts` — **FOLKLORE** · relevance: strategy · status: rejected

Log-scale bands fitted after the fact, with no mechanism and no rule for when the fit would be considered broken.

- **Used by:** crypto market commentary
- **Risk if misused:** A curve refitted whenever price leaves it has no predictive content; it only ever looks correct in hindsight.

#### Lunar and astrological trading signals
`astro-lunar-trading` — **FOLKLORE** · relevance: strategy · status: rejected

No mechanism. Any apparent effect is a multiple-comparisons artefact across the many cycles that can be tested.

- **Used by:** retail trading content
- **Risk if misused:** Testing enough cycles guarantees that one will look significant. Without a correction for the number of hypotheses tested, the result is noise with a p-value attached.

#### Meme supply numbers and vanity addresses
`meme-tokenomics-numbers` — **FOLKLORE** · relevance: analytics · status: not_applicable

Supply figures and addresses chosen for cultural resonance. Zero technical content, and occasionally a fraud signal rather than a neutral one.

- **Used by:** token launches
- **Risk if misused:** Worth tracking as a categorical risk feature rather than a price feature: a supply number chosen for a joke says something about a project's governance, not about its value.


---

## Governance

This document is generated from `config/math_registry.json`, which is
validated on load by `src/mathcore/registry.py` and in CI by
`tests/test_math_registry.py`. The validation is not decorative:

- An entry marked `implemented` must name an owning module that **exists on
  disk**. The registry cannot claim code that was never written.
- An entry marked `folklore` may **never** be marked `implemented`. Numerology
  cannot become a live signal by editing one field.
- Every `depends_on` must resolve, and the dependency graph must be acyclic.
  Both of these were violated by the first draft of this registry and caught by
  the loader, which is the reason the checks exist.
- Every security-relevant entry must state its concrete failure mode.

To add or change an entry, edit the registry and regenerate this file. See
`docs/MATH_ARCHITECTURE.md` for where each object lands in the tree, and
`docs/MATH_ROADMAP.md` for the order in which they are built.

Registry version: 1.0.0 — 69 entries.
