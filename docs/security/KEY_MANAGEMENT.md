# Key management

Key management matters more than encryption. An attacker who obtains ciphertext
**and** the key has defeated the encryption entirely, so the interesting
questions are all about where keys live, who can reach them, and how they are
replaced.

## 1. Key classes

| Class | Example | Blast radius if leaked |
|---|---|---|
| Exchange API key | Trading credential for a venue | Funds. The highest-value asset in the project. |
| Database credential | TimescaleDB / Mongo user | Trading history, positions, model metadata |
| API token / JWT signing key | Operator access to Trade-Bot's own API | Control of the bot, including the kill switch |
| Cloud credential | Deployment identity | Everything downstream |
| GitHub token / deploy key | Repository and Actions access | Code, secrets, the release pipeline |
| Audit-chain key | Integrity of the audit log | Ability to rewrite the record of an incident |

## 2. Environment separation

```
Development key
    ↓
Test key
    ↓
Paper key
    ↓
Production key
```

**Never reuse one key across environments.** A development key that also works
in production converts a laptop compromise into a funds compromise, and it
removes any ability to tell the two apart in the logs.

The same applies to exchange accounts: paper and production use separate
accounts or subaccounts (`SECR-010`).

## 3. Where keys live

```
Developer
    ↓
local secret store

CI
    ↓
GitHub environment secret / OIDC / secret manager

Production
    ↓
dedicated secret manager
```

Prefer short-lived OIDC-issued credentials to long-lived cloud keys wherever
the provider supports the exchange. A credential that expires in fifteen
minutes has a fifteen-minute blast radius.

Never in source, a committed `.env`, a Docker image, the frontend bundle, logs,
a plaintext database column, or workflow YAML.

## 4. Exchange key posture

```
✓ trading permission only if required
✓ withdrawal disabled
✓ IP allowlisting where supported
✓ minimal account permissions
✓ separate paper/testnet keys
✓ separate production keys
✓ separate exchange accounts/subaccounts
```

The application must not possess withdrawal capability. This is verified at the
exchange console, recorded as `SECR-010`, and re-checked at every rotation.

### How that claim is held (PR-008)

The posture of every key is declared in `config/exchange_key_posture.json` and
asserted by `src/security/exchange_key_posture.py`. `LiveExecutor.__init__`
calls `assert_declared_posture_is_safe()` before it builds any state, so a
deployment with no declaration, a malformed one, or one claiming `withdraw` or
`transfer` cannot place an order at all.

Be clear about what that does and does not prove. It proves a human declared
a posture and a reviewer can see it in a diff. It does **not** prove the key
on the venue's side matches the declaration — that is the exchange's state,
unreachable from this process. The declaration is therefore paired with one
manual step, which is the actual verification:

1. With the production key, call the venue's withdrawal endpoint for a
   trivial amount to an address that is *not* on the withdrawal whitelist.
2. Confirm the venue refuses, and record the refusal in the rotation log.
3. Update `declared_on` in the declaration file to the date you did it.

`declared_on` is the date of that check, not the date the file was edited. A
stale `declared_on` next to a recent key rotation is the signal that step 1
was skipped.

## 5. Rotation

A rotation policy has six cases, not one:

| Case | Trigger | Urgency |
|---|---|---|
| Normal rotation | Scheduled interval | Planned |
| Suspected compromise | Any evidence, however weak | Immediate |
| Developer departure | Offboarding | Same day |
| Server compromise | Host integrity in question | Immediate |
| Exchange incident | Venue reports a breach | Immediate |
| GitHub compromise | Account or Actions secrets in question | Immediate |

### Rotation procedure

```
1.  Mint the new key with the same restrictions as the old one
2.  Verify the new key's restrictions at the venue/provider console
3.  Install the new key in the secret manager
4.  Roll the application onto the new key
5.  Verify: authenticated call succeeds, withdrawal call is refused
6.  Disable the old key
7.  Wait the declared observation window
8.  Delete the old key
9.  Record the rotation in the audit trail
```

Step 5 is the step that catches the common mistake: the replacement key issued
with broader permissions than the key it replaces.

Step 6 before step 8, with an observation window between, so that a missed
consumer surfaces as an authentication failure rather than as a deletion that
cannot be undone.

## 6. Rotation must be tested

> **A security procedure that has never been tested isn't a reliable
> procedure.**

`SECR-003` requires a rotation drill: run the procedure against the paper
environment, on a schedule, and record the result. A drill that cannot be run
without a production outage is a finding in itself.

### What PR-008 made mechanical

`src/security/key_lifecycle.py` turns two of the properties above from
conventions into code:

- **Separation** is a property of the derivation, not of who edits which
  `.env`. The environment, exchange and generation are all bound into the
  derived material, so copying the production seed into a developer's shell
  still cannot produce the production key. `assert_usable_in()` is called at
  the point of use, which is where a key handed across a boundary becomes
  visible.
- **Rotation** is a generation counter plus a reason, and the reason decides
  the handover. `previous_generation_still_valid()` returns False for
  `COMPROMISE` and `DEPARTURE` — the two cases where somebody other than the
  operator knows the old key — and True for `SCHEDULED` and `SERVER_REBUILD`,
  so a routine rotation is not an outage. A rotation that is always an outage
  is a rotation that stops happening, which is the failure this distinction
  exists to prevent.

The manual steps above still stand; what the module removes is the chance of
getting the *overlap* decision wrong under pressure.

## 7. Cryptographic hygiene around keys

- Unpredictable values come from the OS CSPRNG, never `random` (`SECR-007`).
- Keys are not logged, not serialised accidentally, not embedded in exception
  messages, and not returned by any API response (`SECR-001`).
- Token, signature and password comparisons are constant-time (`SECR-002`).
- No custom primitives: established libraries, established algorithms
  (`SECR-008`).

## 8. Encryption at rest

Encrypt, with keys held outside the store they protect:

```
database backups
production databases
model artifacts containing sensitive data
audit archives
configuration backups
secret-manager backups
```

For the audit archive specifically, consider authenticated or WORM-style
external storage rather than relying solely on the local hash chain
(`SECR-004`): a hash chain proves tampering, it does not prevent deletion.

### The restore path is the part that must be exercised

`src/security/at_rest.py` provides AES-256-GCM over a versioned envelope, and
`round_trip_check()` exists to be called on an ordinary day rather than on the
worst one. An encryption control that is never decrypted is a claim, not a
control: the write path runs constantly and the read path runs once, under
time pressure, when a database is already gone.

Use the associated-data parameter for whatever names the blob — backup name,
table, date. It is authenticated but not encrypted, and it is what stops one
blob being silently swapped for another encrypted under the same key.

Every decryption failure raises one exception type on purpose. Four
distinguishable failures would invite a loop that tries keys until one stops
raising, which is both an oracle and the shape of an unauthorised restore.

## 9. Compromise response

See `docs/security/INCIDENT_RESPONSE.md` §3. The ordering there is deliberate:
kill trading, then disable the key, then preserve evidence, *then* rotate.
Rotating first destroys the record of how the key was used.
