"""Cross-check the add-on's own files against each other.

A mismatch between config.yaml, translations and run.sh does not fail any
syntax check - it just produces an add-on that shows raw option keys in the UI,
or binds a port nothing is mapped to. So check it explicitly.
"""
import re
import sys

import yaml

cfg = yaml.safe_load(open("voicebox/config.yaml", encoding="utf-8"))
tr = yaml.safe_load(open("voicebox/translations/en.yaml", encoding="utf-8"))
run = open("voicebox/run.sh", encoding="utf-8").read()
rc = 0


def report(ok, good, bad):
    global rc
    print(f"  {'OK  ' if ok else 'FAIL'} {good if ok else bad}")
    if not ok:
        rc = 1


opts = set(cfg.get("options", {}))
schema = set(cfg.get("schema", {}))
trans = set(tr.get("configuration", {}))

report(opts == schema, f"options == schema  ({sorted(opts)})",
       f"options vs schema differ: {sorted(opts ^ schema)}")
report(opts == trans, "every option has an English translation",
       f"options vs translations differ: {sorted(opts ^ trans)}")

print()
ports = cfg.get("ports", {})
report(ports == {"8000/tcp": 17493},
       "container 8000 -> host 17493 (matches the published image)",
       f"unexpected ports mapping: {ports}")
report(cfg.get("ingress_port") == 8000,
       "ingress_port matches the container port",
       f"ingress_port {cfg.get('ingress_port')} != 8000")
report(set(cfg.get("ports_description", {})) == set(ports),
       "every published port is described",
       "ports_description does not cover every port")
report(set(tr.get("network", {})) == set(ports),
       "translations describe every port",
       "translations network keys do not match ports")

print()
m = re.search(r"--port\s+(\d+)", run)
report(bool(m) and int(m.group(1)) == 8000,
       "run.sh binds 8000, matching config.yaml",
       f"run.sh binds {m.group(1) if m else 'nothing detectable'}")

d = re.search(r"get_opt min_free_ram_mb (\d+)", run)
report(bool(d) and int(d.group(1)) == cfg["options"]["min_free_ram_mb"],
       f"min_free_ram_mb default agrees ({cfg['options']['min_free_ram_mb']})",
       "min_free_ram_mb default differs between run.sh and config.yaml")

allowed = set(re.search(r"list\((.*?)\)", cfg["schema"]["log_level"]).group(1).split("|"))
uvicorn_ok = {"critical", "error", "warning", "info", "debug", "trace"}
report(allowed <= uvicorn_ok,
       f"all log_level values are valid for uvicorn ({sorted(allowed)})",
       f"uvicorn would reject: {sorted(allowed - uvicorn_ok)}")

print()
required = {"name", "version", "slug", "description", "arch"}
missing = required - set(cfg)
report(not missing, "all required add-on keys present",
       f"missing required keys: {sorted(missing)}")
report("image" not in cfg,
       "no `image:` key, so Supervisor builds locally from the Dockerfile",
       "`image:` is set - Supervisor will pull instead of building, "
       "and no such image is published")

sys.exit(rc)
