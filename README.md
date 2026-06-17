# cucm-service-manager

Bulk-disable **any** Cisco Unified Communications Manager (CUCM) service —
deactivate or stop it — across many clusters and every node, from one command.
You provide only each cluster's publisher; the tool discovers the rest and
applies the change safely.

The target service is just a config value. WebDialer is the default example, but
the same code disables `Cisco Tftp`, `Cisco CTIManager`, `Cisco CallManager`, or
any other service by name.

Built to replace a slow, error-prone manual task: logging into each node of each
cluster and toggling a service by hand in the Serviceability GUI. In a large
estate that's dozens of nodes across many clusters, repeated by hand, with no
record of what changed.

---

## The problem

Turning a single service off across a multi-cluster CUCM deployment means
visiting **Cisco Unified Serviceability** on every node, in every cluster, and
toggling the same service — then hoping you didn't miss a node or touch the
wrong one. There's no built-in bulk action and no audit trail.

This tool turns that into: define your publishers once, dry-run to see exactly
what will change, then apply — with a CSV record of every node.

## What it does

- **Any service, everywhere.** Deactivates or stops a single configurable
  service across all selected clusters and nodes.
- **Auto-discovers nodes.** You enter only each cluster's publisher; subscribers
  are pulled from the publisher's database via AXL (a read-only `SELECT`).
- **Reports the truth.** Distinguishes genuinely *Deactivated* from
  *activated-but-stopped* — a distinction the run-state field alone can't make
  (see [Design notes](#design-notes)).
- **Leaves an audit trail.** Every run writes a per-node CSV (before / action /
  after / result).

## Two ways to disable a service

CUCM exposes two different mechanisms, and this tool supports both via the
`action` setting:

| `action`     | CUCM mechanism                  | Applies to                                | Persistent? |
|--------------|---------------------------------|-------------------------------------------|-------------|
| `deactivate` | Service Activation (`UnDeploy`) | **Feature** services (WebDialer, TFTP, …) | Yes         |
| `stop`       | Control Center (`Stop`)         | **Any** service (feature or network)      | No          |

CUCM does not allow *deactivating* Network services; if you point `deactivate`
at one, that node simply errors and is logged, no change made. Stopping core
network services is possible but dangerous — know what you're targeting.

## Design notes

A few decisions that make this safe to run against production:

**Safe by default.** The default run is a **dry-run** — it reads status and
reports what *would* change, touching nothing. Changes require `--apply`, which
prompts for confirmation (bypass with `--yes` for automation).

**Single-service blast radius.** Every deactivate/stop request carries exactly
one item — the configured service name. The tool never enumerates or modifies
any other service. Deactivating a feature service stops only that service; it
does not restart CallManager or bounce the node.

**Activation vs. run-state.** Cisco's `soapGetServiceStatus` returns *run-state*
(`Started` / `Stopped` / …), not *activation*. A deactivated service and an
activated-but-stopped service both read as `Stopped`. This tool reads
`ReasonCodeString` (`Service Not Activated`, `ReasonCode -1068`) to report true
**Activated / Deactivated**, matching the GUI — and to skip nodes already done.

**Idempotent.** Already-deactivated nodes are detected and skipped, so re-runs
are safe and make no redundant changes.

**No secrets in source.** All hostnames and credentials live in an external,
git-ignored `config.yaml`. Passwords can be pulled from environment variables
with `env:VAR_NAME` so nothing sensitive is written to disk.

## How it works

```
  config.yaml --> for each cluster:
                    publisher --[ AXL executeSQLQuery (read-only) ]--> node list
                    for each node:
                      soapGetServiceStatus --> Activated? Deactivated?
                      if eligible and --apply:
                        deactivate (UnDeploy) or stop  (single service only)
                      re-check --> CSV log
```

It speaks to two CUCM SOAP interfaces with plain HTTPS and hand-built envelopes
(no WSDL toolchain required): the **AXL** API for node discovery and the
**Serviceability Control Center Services** API for status and the change.

## Requirements

- Python 3.9+
- A CUCM Application User with **Standard CCM Super Users** (Serviceability) and
  **Standard AXL API Access**
- The **Cisco AXL Web Service** activated on each publisher
- Network reach to TCP 8443 on the publishers and nodes

## Install

```bash
git clone https://github.com/<your-username>/cucm-service-manager.git
cd cucm-service-manager
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then edit config.yaml
```

## Configure

Edit `config.yaml` (see `config.example.yaml` for the full template):

```yaml
defaults:
  username: "svc-account"
  password: "env:CUCM_PASSWORD"     # reads $CUCM_PASSWORD at runtime
action: deactivate                  # or: stop
service_name: "Cisco WebDialer Web Service"   # any service name
only_clusters: [cluster-01]         # [] = all clusters
clusters:
  cluster-01:
    publisher: "cucm01-pub.example.com"
  cluster-02:
    publisher: "cucm02-pub.example.com"
```

## Usage

```bash
# 1) Dry-run: discover nodes and report what would change (no changes made)
python cucm_service.py

# 2) Inspect full status fields for the target service, per node
python cucm_service.py --detail

# 3) Limit a run to one cluster (recommended for rollout)
python cucm_service.py --cluster cluster-01

# 4) Apply the change (asks for confirmation; add --yes to skip)
python cucm_service.py --apply
```

### Example (dry-run)

```
=== Cisco WebDialer Web Service | deactivate | DRY-RUN | 2026-06-16 16:06 ===

[cluster-01] nodes: cucm01-pub.example.com, cucm01-sub1.example.com
[cluster-01/cucm01-pub.example.com]  Activated (run-state Started)
[cluster-01/cucm01-sub1.example.com] Deactivated (run-state Stopped)
[cluster-01/cucm01-sub1.example.com] already deactivated -- skipping

Log written: deactivate_20260616_160603.csv
```

## Recommended rollout

1. Set `only_clusters` to a single cluster (or use `--cluster`).
2. Run the dry-run; confirm the discovered node list and the per-node status.
3. Run `--apply`; confirm at the prompt.
4. Spot-check in the GUI (Tools -> Service Activation / Control Center), then move on.

## Roadmap

- [ ] Additional actions: activate (`Deploy`), restart, start
- [ ] Target multiple services in one run
- [ ] Parallel execution across clusters with a concurrency cap
- [ ] HTML/JSON run report in addition to CSV
- [ ] Optional TLS certificate verification against a CA bundle

## Disclaimer

This tool changes service state on production telephony systems. Review the
dry-run output and run it in a change window. Provided as-is under the MIT
License — no warranty. Not affiliated with or endorsed by Cisco.

## License

[MIT](LICENSE) © 2026 Freddy Antony
