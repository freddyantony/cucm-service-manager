#!/usr/bin/env python3
"""
cucm_service.py -- bulk-disable any Cisco CUCM service across clusters.

Deactivates or stops a Cisco Unified Communications Manager service on every node
of one or more clusters. The target service is configurable -- it is NOT tied to
WebDialer; that is only the default example in config.yaml.

  action: deactivate  -> Service Activation (UnDeploy). Works on FEATURE services
                         (WebDialer, TFTP, CTIManager, CallManager, ...). CUCM does
                         not allow deactivating Network services.
  action: stop        -> Control Center (Stop). Works on ANY service, feature or
                         network (stopping core network services is dangerous).

You provide only each cluster's PUBLISHER; subscriber nodes are discovered
automatically from the publisher's database (AXL, read-only SELECT).

Design goals: safe by default, auditable, idempotent.

Operations used, and ONLY these:
  - AXL  executeSQLQuery            -> READ-ONLY SELECT of node names (discovery)
  - Serviceability soapGetServiceStatus       -> read-only status
  - Serviceability soapDoServiceDeployment    -> deactivate (UnDeploy)
  - Serviceability soapDoControlServices      -> stop (only if action: stop)
The deactivate/stop service list ALWAYS contains exactly one item: the configured
service name. No other service is ever read-modified.

Configuration lives in an external YAML file (default: config.yaml) so no
credentials or hostnames are baked into this script. See config.example.yaml.

Auth: an Application User with BOTH Standard CCM Super Users (Serviceability)
AND Standard AXL API Access.

Usage:
  python cucm_service.py                       # dry run (default config.yaml)
  python cucm_service.py --detail              # dump full status per node
  python cucm_service.py --cluster cluster-01  # limit to one cluster
  python cucm_service.py --apply               # make changes (asks to confirm)
  python cucm_service.py --apply --yes         # make changes, no prompt
"""

import os
import sys
import csv
import argparse
from datetime import datetime
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: PyYAML. Run `pip install -r requirements.txt`.")

# ---- runtime settings (populated from the YAML config at startup) ---------
DEFAULT_USERNAME = ""
DEFAULT_PASSWORD = ""
CLUSTERS = {}
ONLY_CLUSTERS = []
ACTION = "deactivate"
SERVICE_NAME = "Cisco WebDialer Web Service"
AXL_VERSION = "12.5"

PORT = 8443
TIMEOUT = 30
NS = "http://schemas.cisco.com/ast/soap"

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)


# ---- config ---------------------------------------------------------------
def _resolve(value):
    """Allow 'env:VAR_NAME' values so secrets can come from the environment."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value or ""


def load_config(path):
    global DEFAULT_USERNAME, DEFAULT_PASSWORD, CLUSTERS, ONLY_CLUSTERS
    global ACTION, SERVICE_NAME, AXL_VERSION
    if not os.path.exists(path):
        sys.exit(f"Config file not found: {path}\n"
                 f"Copy config.example.yaml to {path} and fill it in.")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    defaults = cfg.get("defaults", {}) or {}
    DEFAULT_USERNAME = defaults.get("username", "")
    DEFAULT_PASSWORD = defaults.get("password", "")
    CLUSTERS = cfg.get("clusters", {}) or {}
    ONLY_CLUSTERS = cfg.get("only_clusters", []) or []
    ACTION = cfg.get("action", "deactivate")
    SERVICE_NAME = cfg.get("service_name", "Cisco WebDialer Web Service")
    AXL_VERSION = str(cfg.get("axl_version", "12.5"))
    if ACTION not in ("deactivate", "stop"):
        sys.exit(f"Invalid action '{ACTION}' (use 'deactivate' or 'stop').")


# ---- xml helpers ----------------------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _check_fault(root, label):
    for el in root.iter():
        if _local(el.tag) == "Fault":
            fault = "".join((c.text or "") for c in el.iter() if _local(c.tag) == "faultstring")
            raise RuntimeError(f"{label} Fault: {fault or 'unknown'}")


# ---- serviceability (control center) --------------------------------------
def _cc_post(node, user, pwd, inner):
    r = requests.post(
        f"https://{node}:{PORT}/controlcenterservice2/services/ControlCenterServices",
        data=(f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
              f'xmlns:ns="{NS}"><soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>').encode("utf-8"),
        auth=HTTPBasicAuth(user, pwd),
        verify=False,                       # point at a CUCM tomcat .pem to enable cert checking
        timeout=TIMEOUT,
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
    return r.text


def _parse_records(xml_text):
    root = ET.fromstring(xml_text)
    _check_fault(root, "SOAP")
    out = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        rec = {_local(c.tag): (c.text or "").strip() for c in list(item)}
        if rec.get("ServiceName"):
            out.append(rec)
    return out


def find_record(node, user, pwd, name):
    inner = "<ns:soapGetServiceStatus><ns:ServiceStatus></ns:ServiceStatus></ns:soapGetServiceStatus>"
    for rec in _parse_records(_cc_post(node, user, pwd, inner)):
        if rec.get("ServiceName", "").strip().lower() == name.strip().lower():
            return rec
    return None


def deactivate(node, user, pwd, name):
    inner = (
        "<ns:soapDoServiceDeployment><ns:DeploymentServiceRequest>"
        f"<ns:NodeName>{escape(node)}</ns:NodeName>"
        "<ns:DeployType>UnDeploy</ns:DeployType>"
        f"<ns:ServiceList><ns:item>{escape(name)}</ns:item></ns:ServiceList>"
        "</ns:DeploymentServiceRequest></ns:soapDoServiceDeployment>"
    )
    return _parse_records(_cc_post(node, user, pwd, inner))


def stop(node, user, pwd, name):
    inner = (
        "<ns:soapDoControlServices><ns:ControlServiceRequest>"
        f"<ns:NodeName>{escape(node)}</ns:NodeName>"
        "<ns:ControlType>Stop</ns:ControlType>"
        f"<ns:ServiceList><ns:item>{escape(name)}</ns:item></ns:ServiceList>"
        "</ns:ControlServiceRequest></ns:soapDoControlServices>"
    )
    return _parse_records(_cc_post(node, user, pwd, inner))


def activation_of(rec):
    """Derive Activated/Deactivated. A deactivated service reports ReasonCodeString
    'Service Not Activated' (ReasonCode -1068); ServiceStatus alone is only run-state."""
    if rec is None:
        return "Unknown"
    rcs = (rec.get("ReasonCodeString") or "").strip().lower()
    rc = (rec.get("ReasonCode") or "").strip()
    if "not activated" in rcs or rc == "-1068":
        return "Deactivated"
    return "Activated"


# ---- AXL (read-only node discovery) ---------------------------------------
def axl_list_nodes(publisher, user, pwd):
    ns = f"http://www.cisco.com/AXL/API/{AXL_VERSION}"
    sql = "SELECT name FROM ProcessNode WHERE name != 'EnterpriseWideData'"
    body = (
        f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:ns="{ns}"><soapenv:Header/><soapenv:Body>'
        f'<ns:executeSQLQuery><sql>{escape(sql)}</sql></ns:executeSQLQuery>'
        f'</soapenv:Body></soapenv:Envelope>'
    )
    r = requests.post(
        f"https://{publisher}:{PORT}/axl/",
        data=body.encode("utf-8"),
        auth=HTTPBasicAuth(user, pwd),
        verify=False,
        timeout=TIMEOUT,
        headers={"Content-Type": "text/xml; charset=utf-8",
                 "SOAPAction": f"CUCM:DB ver={AXL_VERSION} executeSQLQuery"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"AXL HTTP {r.status_code}: {r.text[:200]!r}")
    root = ET.fromstring(r.text)
    _check_fault(root, "AXL")
    names = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        for c in list(row):
            if _local(c.tag) == "name" and (c.text or "").strip():
                names.append(c.text.strip())
    return list(dict.fromkeys(names))


def creds_for(cfg):
    return (_resolve(cfg.get("username") or DEFAULT_USERNAME),
            _resolve(cfg.get("password") or DEFAULT_PASSWORD))


def resolve_nodes(cfg, user, pwd):
    if cfg.get("nodes"):
        return list(dict.fromkeys(cfg["nodes"]))
    pub = cfg.get("publisher")
    if not pub:
        raise RuntimeError("cluster has neither 'publisher' nor 'nodes' configured")
    names = axl_list_nodes(pub, user, pwd)
    if not names:
        raise RuntimeError("AXL returned no nodes")
    return names


def active_clusters():
    for cluster, cfg in CLUSTERS.items():
        if ONLY_CLUSTERS and cluster not in ONLY_CLUSTERS:
            continue
        yield cluster, cfg


# ---- modes ----------------------------------------------------------------
def detail():
    fields = ["ServiceName", "ServiceStatus", "ReasonCode", "ReasonCodeString", "StartTime", "UpTime"]
    for cluster, cfg in active_clusters():
        user, pwd = creds_for(cfg)
        try:
            nodes = resolve_nodes(cfg, user, pwd)
        except Exception as e:
            print(f"\n[{cluster}] node discovery failed: {e}")
            continue
        print(f"\n[{cluster}] discovered nodes: {', '.join(nodes)}")
        for node in nodes:
            print(f"=== {cluster} / {node} ===")
            try:
                rec = find_record(node, user, pwd, SERVICE_NAME)
                if not rec:
                    print(f"  '{SERVICE_NAME}' not present on this node"); continue
                for k in fields:
                    print(f"  {k:18}: {rec.get(k)}")
                print(f"  {'=> activation':18}: {activation_of(rec)}")
            except Exception as e:
                print(f"  ERROR: {e}")


def run(apply_changes):
    mode = "APPLY" if apply_changes else "DRY-RUN"
    print(f"=== {SERVICE_NAME} | {ACTION} | {mode} | {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")

    rows = []
    for cluster, cfg in active_clusters():
        user, pwd = creds_for(cfg)
        if not user or not pwd:
            print(f"[{cluster}] missing username/password -- skipping cluster")
            rows.append({"cluster": cluster, "node": "(cluster)", "before": "",
                         "action": "", "after": "", "result": "no credentials"})
            continue
        try:
            nodes = resolve_nodes(cfg, user, pwd)
        except Exception as e:
            print(f"[{cluster}] node discovery failed: {e} -- skipping cluster")
            rows.append({"cluster": cluster, "node": "(discovery)", "before": "",
                         "action": "", "after": "", "result": f"discovery failed: {e}"})
            continue

        print(f"[{cluster}] nodes: {', '.join(nodes)}")
        for node in nodes:
            row = {"cluster": cluster, "node": node, "before": "",
                   "action": "", "after": "", "result": ""}
            try:
                rec = find_record(node, user, pwd, SERVICE_NAME)
                if rec is None:
                    row["before"] = "NOT FOUND"
                    row["result"] = "service not present on node"
                    print(f"[{cluster}/{node}] {SERVICE_NAME} not present -- skipping")
                    rows.append(row); continue

                before = activation_of(rec)
                row["before"] = before
                print(f"[{cluster}/{node}] {before} (run-state {rec.get('ServiceStatus')})")

                if ACTION == "deactivate" and before == "Deactivated":
                    row["action"] = "none"; row["result"] = "already deactivated"
                    print(f"[{cluster}/{node}] already deactivated -- skipping")
                    rows.append(row); continue

                if not apply_changes:
                    row["action"] = "dry-run"; row["result"] = f"would {ACTION}"
                    rows.append(row); continue

                if ACTION == "deactivate":
                    deactivate(node, user, pwd, SERVICE_NAME)
                else:
                    stop(node, user, pwd, SERVICE_NAME)
                row["action"] = ACTION

                rec2 = find_record(node, user, pwd, SERVICE_NAME)
                row["after"] = activation_of(rec2) if rec2 else "?"
                row["result"] = "OK"
                print(f"[{cluster}/{node}] -> {row['after']}")
            except Exception as e:
                row["result"] = f"error: {e}"
                print(f"[{cluster}/{node}] ERROR: {e}")
            rows.append(row)

    out = f"{ACTION}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cluster", "node", "before", "action", "after", "result"])
        w.writeheader(); w.writerows(rows)
    print(f"\nLog written: {out}")


def confirm_apply():
    targets = [c for c, _ in active_clusters()]
    print(f"About to {ACTION.upper()} '{SERVICE_NAME}' across clusters: {', '.join(targets) or '(none)'}")
    if not sys.stdin.isatty():
        sys.exit("Refusing to apply non-interactively without --yes.")
    reply = input("Type 'yes' to proceed: ").strip().lower()
    if reply != "yes":
        sys.exit("Aborted.")


def main():
    p = argparse.ArgumentParser(description="Bulk-deactivate a CUCM feature service across clusters.")
    p.add_argument("--config", default="config.yaml", help="path to YAML config (default: config.yaml)")
    p.add_argument("--cluster", help="limit this run to a single cluster key (overrides only_clusters)")
    p.add_argument("--apply", action="store_true", help="make changes (default is dry-run)")
    p.add_argument("--detail", action="store_true", help="dump full status fields per node and exit")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt on --apply")
    args = p.parse_args()

    load_config(args.config)
    if args.cluster:
        global ONLY_CLUSTERS
        ONLY_CLUSTERS = [args.cluster]

    if args.detail:
        detail()
        return
    if args.apply and not args.yes:
        confirm_apply()
    run(args.apply)


if __name__ == "__main__":
    main()
