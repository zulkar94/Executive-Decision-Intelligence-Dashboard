# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and semantic versioning.

## [1.0.0] - 2026-09-06

### Added
- Decision management: decisions, weighted criteria with benefit/cost direction, options,
  and a bulk idempotent score matrix with per-cell uncertainty.
- Ranking by weighted sum and TOPSIS, with per-criterion contribution breakdowns.
- One-at-a-time weight sensitivity reporting swing and rank-reversal thresholds.
- Monte Carlo simulation with optional Dirichlet weight jitter, returning win
  probabilities, rank distributions, percentile bands, and a reproducible seed.
- AHP weight derivation with a Saaty consistency ratio, refusing to apply CR > 0.10.
- KPI registry, idempotent time-series ingestion, Holt forecasting with prediction bands
  and holdout backtesting, and robust median/MAD anomaly detection.
- Executive summary combining indicator health with decisions whose winner is fragile.
- Argon2id credentials, rotating refresh-token families with replay detection, RBAC,
  account lockout, and an append-only audit trail.
- Dependency-free dashboard with canvas charts served under a strict CSP.
- Docker multi-stage build, compose stack, GitHub Actions CI with lint, types, coverage
  gate, bandit, pip-audit, gitleaks, Trivy, and CodeQL.
