# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). This project uses semantic versioning.

## [1.0.0] - 2026-06-16
### Added
- Bulk-disable (deactivate or stop) any configurable CUCM service across multiple clusters.
- Automatic subscriber discovery from each cluster publisher via AXL (read-only SELECT).
- True Activated/Deactivated detection via `ReasonCodeString`, independent of run-state.
- Safe-by-default workflow: dry-run default, `--apply` with confirmation, `--detail` inspection.
- External YAML config with `env:` secret resolution; no credentials in source.
- Per-run CSV audit log.
