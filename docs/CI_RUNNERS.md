# CI runners — cloud first, self-hosted as fallback

CI is the authoritative validator for this repo (see `CLAUDE.md`: never run
pytest / ruff / mypy locally). That rule only holds if CI can actually run, so
there are two runner tiers and a one-command switch between them.

## The policy

**Always use GitHub-hosted runners.** They are the default and need no
maintenance. The self-hosted runner exists for exactly one situation: the cloud
tier is unusable and work would otherwise be blocked with no way to validate
anything.

Legitimate reasons to switch:

- Actions minutes exhausted, or a billing block on the account.
- A runner-allocation outage. **Symptom:** every job fails in ~2 seconds with a
  `null` runner id — including jobs your change cannot possibly affect (a
  Frontend failure on a Python-only diff). Confirm before blaming your code:

  ```bash
  gh run view <run-id> --json jobs \
    --jq '.jobs[] | "\(.name) | \(.conclusion) | \(.startedAt) -> \(.completedAt) | runner=\(.runnerId)"'
  ```

  Two-second durations and `runner=null` across the board mean the infra never
  allocated a machine. There are no logs to read because nothing ran.

Not a reason to switch: a job that is merely slow, queued, or failing for a
reason your diff explains.

## Switching

```bash
gh variable set CI_RUNNER --body trade-bot-selfhosted   # → self-hosted
gh variable delete CI_RUNNER                            # → back to cloud (default)
gh variable list                                        # which tier is active
```

`.github/workflows/ci.yml` reads `runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}`
in every job. Unset means cloud, so the safe state is also the default state.

**Switch back as soon as the cloud tier recovers.** Leaving CI pinned to one
machine in someone's house is a single point of failure, and it runs workflow
code from every branch on that machine.

## The self-hosted runner

| | |
|---|---|
| Host | `fujitsu-s752` (local Linux workstation) |
| Install path | `~/actions-runner` |
| Labels | `self-hosted`, `Linux`, `X64`, `trade-bot-selfhosted` |
| Service | systemd, via the runner's own `svc.sh` |
| Scope | repository-level (`zulqarnain106612-cpu/Trade-Bot`) |

### Service management

```bash
cd ~/actions-runner
sudo ./svc.sh status
sudo ./svc.sh start
sudo ./svc.sh stop
journalctl -u "$(systemctl list-units --type=service --no-legend 'actions.runner.*' | awk '{print $1}')" -f
```

### Re-registering

Registration tokens expire after an hour, so generate one at the moment you
need it:

```bash
cd ~/actions-runner
TOKEN=$(gh api -X POST repos/zulqarnain106612-cpu/Trade-Bot/actions/runners/registration-token --jq .token)
./config.sh --url https://github.com/zulqarnain106612-cpu/Trade-Bot \
            --token "$TOKEN" \
            --name fujitsu-s752 \
            --labels trade-bot-selfhosted \
            --unattended --replace
```

Removing it: `./config.sh remove --token $(gh api -X POST .../remove-token --jq .token)`.

## Known limits of the self-hosted tier

These are differences from GitHub-hosted runners that will bite you. They are
listed because a job failing for one of these reasons is *not* a code defect.

- **`actions/setup-python` cannot download Python on this host.** The CPU is an
  Ivy Bridge i3-3110M, which tops out at the `x86-64-v2` microarchitecture
  level; GitHub's prebuilt CPython 3.11 needs `x86-64-v3`. The symptom is a
  `Set up Python` step failing with:

  ```
  ./python: CPU ISA level is lower than required
  ```

  This is a property of the machine, not a misconfiguration — no amount of
  workflow tweaking makes the hosted build run. It is worked around by
  pre-populating the runner's tool cache with a baseline-compatible build (the
  one `uv` manages), so `setup-python` finds it locally and never downloads:

  ```bash
  V=3.11.15
  D=~/actions-runner/_work/_tool/Python/$V/x64
  mkdir -p "$(dirname "$D")"
  cp -aL ~/.local/share/uv/python/cpython-$V-linux-x86_64-gnu/ "$D"
  ln -sf python3.11 "$D/bin/python"
  touch ~/actions-runner/_work/_tool/Python/$V/x64.complete   # the marker setup-python looks for
  ```

  Copy, don't symlink: CI installs packages into that tree, and a symlink would
  push them back into uv's managed toolchain. If a failed download left a
  partial version directory behind (`3.11.16` with a missing
  `libpython3.11.so.1.0`), delete it — `setup-python` prefers the highest
  version present and will pick the broken one.

- **The `backend` job needs a Docker API for its TimescaleDB `services:`
  container**, supplied here by **rootless podman**, not Docker:

  ```bash
  systemctl --user enable --now podman.socket
  sudo loginctl enable-linger fujitsu   # socket must exist with no login session
  ```

  `~/actions-runner/.env` sets `DOCKER_HOST` to the podman user socket, and the
  runner reads that file into every job. Restart the service after editing it.

  This is deliberately *not* `usermod -aG docker fujitsu`. Docker-group
  membership is effectively root on the host, and this runner executes workflow
  code from any pushed branch; rootless podman runs those containers as uid
  1000 instead. Symptom if the socket is missing or `DOCKER_HOST` is unset:
  `permission denied while trying to connect to the docker API at
  unix:///var/run/docker.sock`, failing `Initialize containers` while the other
  jobs pass.

- **The workspace is not clean between runs.** Hosted runners get a fresh VM
  every time; this one reuses `~/actions-runner/_work`. A test that passes only
  because of a leftover file will pass here and fail in the cloud. If a result
  differs between tiers, trust the cloud.

- **Caches and toolchain are shared.** `actions/setup-python` and
  `actions/setup-node` install into the runner's tool cache and persist. Disk
  fills over time; `~/actions-runner/_work/_tool` is the place to prune.

- **One job at a time.** A single runner has one executor, so the four CI jobs
  that run in parallel on hosted runners serialise here. Expect roughly the sum
  of their durations, not the maximum.

- **It runs whatever the workflow says.** Any branch pushed to this repo
  executes on this machine with this user's permissions. That is the real
  reason self-hosted is a fallback and not the default.
