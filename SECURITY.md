# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/zulqarnain106612-cpu/Trade-Bot/security/advisories/new)
form. Please do not open a public issue for anything exploitable.

Include the affected version or commit, reproduction steps, and the impact you
believe it has. Expect an initial response within seven days.

## Scope

This repository is trading software: it holds exchange API keys and can place
orders. Treat the following as security-relevant, not merely bugs:

- Anything that leaks credentials, API keys, or private keys — including into
  logs, metrics, error messages, or URLs.
- Anything that lets an untrusted input reach an order-placement path, or that
  bypasses the risk gates and kill switches.
- Anything that silently degrades a safety signal instead of failing loudly.

## Supported versions

Only `main` receives security fixes. There are no maintained release branches.

## Automated scanning

CodeQL (`security-and-quality`) runs on every pull request and weekly on a
schedule; Dependabot opens grouped dependency updates weekly.
