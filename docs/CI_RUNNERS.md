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

- **The `backend` job needs Docker.** It declares a `services:` block for
  TimescaleDB, which the runner starts as a Docker container. The daemon is
  installed and active on this host, but the `fujitsu` user must be in the
  `docker` group for the runner to use it:

  ```bash
  sudo usermod -aG docker fujitsu && sudo ./svc.sh stop && sudo ./svc.sh start
  ```

  Note that docker-group membership is effectively root on the host. Without
  it, the `backend` job fails to start its service container while the other
  three jobs run fine. Podman is also installed but Actions `services:` speaks
  to the Docker socket specifically.

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
