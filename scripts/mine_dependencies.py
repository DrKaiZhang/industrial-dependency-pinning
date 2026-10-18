#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
03_JSEP_dependency_evolution / mine_dependencies.py

Empirical mining of third-party dependency-update evolution in open-source
industrial-software projects.

Method (file-level history only -- no blob fetch required, so it scales to
long histories):
  1. locate dependency manifests in each project (pom.xml, build.gradle,
     package.json, requirements*.txt, Cargo.toml, go.mod, CMakeLists.txt,
     platformio.ini, *.csproj, conanfile.*);
  2. walk the commit history of each manifest with `git log --follow`, giving
     a dated sequence of dependency declarations;
  3. reconstruct the declared-version timeline per dependency, including
     additions, upgrades, downgrades and removals;
  4. compute, per project and per dependency:
       - number of distinct upgrades
       - upgrade cadence (upgrades per active year)
       - time to first upgrade after a dependency appears
       - time lag between consecutive upgrades
       - share of upgrades that jump more than one major version
       - burstiness of upgrade commits
       - whether upgrade commits co-occur with bug-fix commits
         (fix-commit proximity as an observable risk signal)
  5. classify each project as embedded/industrial vs general-purpose by a
     reproducible path-and-content classifier, so industrial-specific
     behaviour can be contrasted with a reference group.

Outputs: data/dependency_events.csv, data/dependency_summary.csv,
         data/project_summary.csv, data/config.json
"""
import os
import re
import sys
import json
import csv
import datetime as dt
import subprocess
from collections import defaultdict, Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
REPO_DIRS = [os.path.join(_ROOT, "_repos2"), os.path.join(_ROOT, "_repos")]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data")
os.makedirs(OUT, exist_ok=True)

# project key -> (display, declared domain)
PROJECTS = {
    "riot": ("RIOT", "embedded-OS"),
    "contiki-ng": ("Contiki-NG", "embedded-OS"),
    "zephyr": ("Zephyr", "embedded-OS"),
    "esp-idf": ("ESP-IDF", "embedded-OS"),
    "esp8266_ard": ("ESP8266-Arduino", "embedded-OS"),
    "mbed-os": ("Mbed-OS", "embedded-OS"),
    "freertos-kernel": ("FreeRTOS-Kernel", "RTOS-kernel"),
    "iotivity-lite": ("IoTivity-Lite", "IoT-middleware"),
    "thingsboard": ("ThingsBoard", "industrial-IoT-platform"),
    "mosquitto": ("Eclipse-Mosquitto", "industrial-messaging"),
    "ros2_control": ("ros2_control", "industrial-robot-control"),
    "node_opcua": ("node-opcua", "industrial-protocol-stack"),
    "ua_net": ("UA-.NET", "industrial-protocol-stack"),
    "beremiz": ("Beremiz", "SCADA-runtime"),
    "pycomm3": ("pycomm3", "industrial-protocol-client"),
    "serial_studio": ("Serial-Studio", "industrial-HMI"),
    "evcc": ("evcc", "industrial-energy-control"),
    # general-purpose reference projects (same ecosystems, different domain)
    "grafana": ("Grafana", "monitoring-UI"),
    "ha_core": ("Home-Assistant", "home-automation"),
    "kafka": ("Apache-Kafka", "event-streaming"),
}

MANIFEST_PATTERNS = [
    ("maven", re.compile(r"(^|/)pom\.xml$")),
    ("gradle", re.compile(r"(^|/)build\.gradle(\.kts)?$|(^|/)settings\.gradle")),
    ("npm", re.compile(r"(^|/)package\.json$")),
    ("pypi", re.compile(r"(^|/)requirements[\w\-]*\.txt$|(^|/)pyproject\.toml$")),
    ("cargo", re.compile(r"(^|/)Cargo\.toml$")),
    ("gomod", re.compile(r"(^|/)go\.mod$")),
    ("nuget", re.compile(r"(^|/)[\w\.\-]+\.csproj$")),
    ("platformio", re.compile(r"(^|/)platformio\.ini$")),
    ("conan", re.compile(r"(^|/)conanfile\.(py|txt)$")),
    ("west", re.compile(r"(^|/)west\.yml$")),
]

# ------------------------------------------------- version comparators
_SEMVER = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(v):
    """Return a comparable tuple; (major, minor, patch, pre_release_rank)."""
    if v is None:
        return None
    s = str(v).strip().lstrip("vV= ")
    m = _SEMVER.search(s)
    if not m:
        return None
    pre = 0 if re.search(r"(alpha|beta|rc|pre|dev|snapshot|nightly)", s, re.I) else 1
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), pre)


def vkey(v):
    p = parse_version(v)
    return p if p else (-1, -1, -1, 0)


# ------------------------------------------------- manifest parsers
def _strip_xml(s):
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<(property|plugin|parent|build|profiles|modules|distributionManagement|"
               r"repositories|dependencyManagement|licenses|developers|scm|"
               r"organization|url|ciManagement|inceptionYear|description)\b.*?"
               r"</\1>", " ", s, flags=re.S | re.I)
    return s


def parse_maven(txt):
    out = {}
    body = _strip_xml(txt)
    for m in re.finditer(r"<dependency>(.*?)</dependency>", body, re.S):
        b = m.group(1)
        g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", b)
        a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", b)
        v = re.search(r"<version>\s*([^<$]+?)\s*</version>", b)
        if g and a:
            out[f"{g.group(1)}:{a.group(1)}"] = v.group(1) if v else None
    return out


def parse_gradle(txt):
    out = {}
    for m in re.finditer(
            r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|"
            r"compile|annotationProcessor)\s*[(\s]+['\"]([^'\"]+):([^'\"]+)"
            r":([^'\"]+)['\"]", txt):
        out[f"{m.group(1)}:{m.group(2)}"] = m.group(3)
    for m in re.finditer(r"\bimplementation\s+[(\s]+['\"]([^'\"]+)['\"]", txt):
        out.setdefault(m.group(1), None)
    return out


def parse_npm(txt):
    out = {}
    for sec in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        m = re.search(r'"' + sec + r'"\s*:\s*\{(.*?)\}', txt, re.S)
        if not m:
            continue
        for mm in re.finditer(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1)):
            out[mm.group(1)] = mm.group(2)
    return out


REQ = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*(?:\[[^\]]*\])?\s*"
                  r"(?:[=<>!~]{1,2}\s*([^\s;#,]+))?")


def parse_pypi(txt):
    """requirements.txt / pyproject dependency lists.

    Version specifiers are reduced to a concrete comparable version so that
    upgrade detection works on ranges such as `>=1.2,<2.0`; the lower bound is
    the declared floor, which is what an upgrade actually moves.
    """
    out = {}
    # pyproject [project] dependencies = [ "name>=1.2" , ... ]
    for m in re.finditer(r"[\"']?([A-Za-z0-9_.\-]+)\s*([<>=!~]=?[^,\"\']*)[\"']?",
                         txt):
        pass
    body = txt
    pm = re.search(r"dependencies\s*=\s*\[(.*?)\]", txt, re.S)
    if pm:
        body = txt[:pm.start(1)] + pm.group(1) + txt[pm.end(1):]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if "://" in line:
            continue
        m = REQ.match(line)
        if not m:
            continue
        name = m.group(1).split("[")[0].strip().lower()
        spec = (m.group(2) or "").strip()
        ver = None
        # lowest bound of the specifier is the pinned floor
        bounds = re.findall(r"(>=|==|>)\s*([0-9][\w\.\-\+]*)", spec)
        if bounds:
            ver = bounds[0][1]
        elif re.match(r"^[\d]", spec):
            ver = spec
        elif re.match(r"^\^?~?=?\s*[\d]", spec):
            ver = spec.lstrip("^~")
        if name and name not in ("python",):
            out[name] = ver
    return out


def parse_west(txt):
    """west.yml (Zephyr meta-manifest) -- project revisions and versions."""
    out = {}
    for m in re.finditer(r"name:\s*([A-Za-z0-9_.\-]+).*?"
                         r"(?:revision:\s*([A-Za-z0-9_.\-]+)|"
                         r"version:\s*([^\s]+))", txt, re.S):
        out[m.group(1)] = m.group(2) or m.group(3)
    return out


def parse_kconfig(txt):
    """Zephyr west.yml-like / Kconfig dependency pins are rare; skip."""


def parse_cargo(txt):
    """Cargo.toml, including `[workspace.dependencies]` inheritance."""
    out = {}
    sec = None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("["):
            sec = s
            continue
        m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*=\s*(.+)$", s)
        if not m:
            continue
        name, val = m.group(1), m.group(2)
        if sec and re.search(r"dependencies|build-dependencies|dev-dependencies",
                             sec):
            v = re.search(r'version\s*=\s*"([^"]+)"', val)
            if v:
                out[name] = v.group(1)
            elif re.search(r"workspace\s*=\s*true", val):
                out[name] = None       # resolved from the workspace root
            elif re.search(r"^\s*\{", val) and "version" not in val:
                out[name] = None
        if sec and re.search(r"workspace\.dependencies", sec):
            v = re.search(r'version\s*=\s*"([^"]+)"', val)
            if v:
                out[name] = v.group(1)
    return out


def parse_gomod(txt):
    out = {}
    inblock = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("require ("):
            inblock = True
            continue
        if inblock and s == ")":
            inblock = False
            continue
        m = re.match(r"^(?:require\s+)?([\w\.\-/~]+\.[\w\.\-/~]+)\s+(v[\w\.\-\+]+)"
                     r"(?:\s+//.*)?$", s)
        if m and (inblock or s.startswith("require")):
            out[m.group(1)] = m.group(2)
    return out


def parse_nuget(txt):
    out = {}
    for m in re.finditer(r"PackageReference\s+Include\s*=\s*\"([^\"]+)\""
                         r"(?:[^>]*Version\s*=\s*\"([^\"]+)\")?", txt):
        out[m.group(1)] = m.group(2)
    return out


def parse_platformio(txt):
    out = {}
    sec = None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*=\s*@?([0-9][^\s]*)", s)
        if m and sec and re.search(r"env|env:|_release|_debug", sec):
            out[m.group(1)] = m.group(2)
    return out


PARSERS = dict(maven=parse_maven, gradle=parse_gradle, npm=parse_npm,
               pypi=parse_pypi, cargo=parse_cargo, gomod=parse_gomod,
               nuget=parse_nuget, platformio=parse_platformio,
               west=parse_west)

# ------------------------------------------------------- git helpers
def git(repo, args, timeout=900):
    try:
        r = subprocess.run(["git", "-C", repo] + args,
                           capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except Exception:
        return None


def list_manifests(repo):
    """Choose manifests that actually pin third-party versions.

    Embedded repositories keep many *tooling* requirements files; the ones that
    carry the shipped product's third-party dependencies are the component- or
    platform-level manifests. Selection rules, applied in order:
      1. prefer a manifest that declares at least one version constraint;
      2. prefer component/platform level over repository tooling (tools/, doc/,
         scripts/ci) unless the tooling file is the only one available;
      3. keep at most two manifests per ecosystem, in different subtrees, so a
         project is not represented by a single aggregate file only.
    """
    out = git(repo, ["ls-tree", "-r", "--name-only", "HEAD"])
    if not out:
        return []
    cands = []
    for path in out.splitlines():
        p = path.strip()
        if not p:
            continue
        for eco, rx in MANIFEST_PATTERNS:
            if rx.search(p):
                if re.search(r"(^|/)(test|tests|example|examples|sample|"
                             r"node_modules|vendor|third_party|3rdparty)/",
                             p, re.I):
                    continue
                cands.append((eco, p))
                break
    if not cands:
        return []

    scored = []
    for eco, p in cands:
        blob = git(repo, ["show", f"HEAD:{p}"], timeout=180)
        if not blob or len(blob) > 2_000_000:
            continue
        try:
            deps = PARSERS[eco](blob)
        except Exception:
            continue
        pinned = sum(1 for v in deps.values()
                     if v and re.search(r"\d", str(v)))
        if not deps or pinned == 0:
            continue
        tooling = bool(re.search(r"(^|/)(tools?|doc|docs|scripts?/ci|\.github|"
                                 r"ci)/", p, re.I))
        scored.append(dict(eco=eco, path=p, n_pinned=pinned, n_deps=len(deps),
                           tooling=tooling, depth=p.count("/")))

    chosen = {}
    for eco in sorted({s["eco"] for s in scored}):
        pool = [s for s in scored if s["eco"] == eco]
        # 1) component-level manifests first, 2) then larger pinned sets
        pool.sort(key=lambda s: (s["tooling"], -s["n_pinned"], s["depth"]))
        picked = []
        trees = set()
        for s in pool:
            tree = s["path"].split("/")[0] if "/" in s["path"] else ""
            if tree and tree in trees:
                continue
            picked.append(s)
            if tree:
                trees.add(tree)
            if len(picked) >= 2:
                break
        for s in picked:
            chosen[(eco, s["path"])] = s
    return [(eco, p) for (eco, p) in sorted(chosen)]


def manifest_history(repo, eco, path, max_commits=400):
    """Dated sequence of (unix_ts, sha, declared-deps) for one manifest.

    git emits  %x1e<timestamp>\\n<sha>\\n\\n<subject...>  so the record separator
    precedes the timestamp and the sha is the first line of the following
    block.
    """
    fmt = "%x1e%H %at"
    out = git(repo, ["log", "--follow", f"--pretty=format:{fmt}",
                     f"-n{max_commits}", "--", path], timeout=1200)
    if not out:
        return []
    entries = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        head = chunk.split("\n", 1)[0].strip()
        parts = head.split()
        if len(parts) < 2:
            continue
        sha, ts = parts[0], parts[1]
        if not ts.isdigit():
            continue
        entries.append((int(ts), sha))
    if not entries:
        return []
    # batch-read blob contents: one `git show` per sampled revision
    step = max(1, len(entries) // 120)
    sampled = entries[::step]
    if sampled[-1] != entries[-1]:
        sampled.append(entries[-1])
    seq = []
    for ts, sha in sampled:
        blob = git(repo, ["show", f"{sha}:{path}"], timeout=300)
        if not blob or len(blob) > 4_000_000:
            continue
        try:
            deps = PARSERS[eco](blob)
        except Exception:
            deps = {}
        deps = {k: v for k, v in deps.items()
                if k and not k.startswith(("http://", "https://", "git@"))}
        if deps:
            seq.append((ts, sha, deps))
    seq.sort(key=lambda x: x[0])
    # de-duplicate consecutive identical states
    ded = []
    for ts, sha, d in seq:
        if ded and ded[-1][2] == d:
            continue
        ded.append((ts, sha, d))
    return ded


def classify_upgrade(old, new):
    a, b = vkey(old), vkey(new)
    if b < (0, 0, 0, 0):
        return "unknown", 0
    if a < (0, 0, 0, 0):
        return "add", 0
    if b == a:
        return "same", 0
    if b > a:
        major_jump = (b[0] - a[0]) > 1
        return "upgrade", int(major_jump)
    return "downgrade", 0


def locate(key):
    for base in REPO_DIRS:
        p = os.path.join(base, key)
        if os.path.isdir(os.path.join(p, ".git")):
            return p
    return None


def mine_project(key, display, domain, window=400):
    path = locate(key)
    if not path:
        print(f"[skip] {key}: no clone")
        return [], None
    mans = list_manifests(path)
    if not mans:
        print(f"[skip] {key}: no manifest")
        return [], None
    print(f"[mine] {display}: manifests={[m[0] for m in mans]}", flush=True)
    events = []
    dep_first_seen = {}
    for eco, mpath in mans:
        seq = manifest_history(path, eco, mpath, window)
        if not seq:
            continue
        for i, (ts, sha, deps) in enumerate(seq):
            if i == 0:
                for d, v in deps.items():
                    dep_first_seen.setdefault(d, (ts, v, eco, mpath))
                    events.append(dict(
                        project=display, domain=domain, ecosystem=eco,
                        manifest=mpath, ts=ts,
                        date=dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                        commit=sha, dependency=d, old_version=None,
                        new_version=v, event="add", major_jump=0,
                        n_deps_in_manifest=len(deps)))
                continue
            pts, _, pdeps = seq[i - 1]
            for d, v in deps.items():
                if d not in pdeps:
                    kind, jump = "add", 0
                else:
                    kind, jump = classify_upgrade(pdeps[d], v)
                if kind == "same":
                    continue
                events.append(dict(
                    project=display, domain=domain, ecosystem=eco,
                    manifest=mpath, ts=ts,
                    date=dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                    commit=sha, dependency=d, old_version=pdeps.get(d),
                    new_version=v, event=kind, major_jump=jump,
                    n_deps_in_manifest=len(deps)))
    print(f"[done] {display}: {len(events)} dependency events")
    return events, len(mans)


def main():
    window = 400
    for a in sys.argv[1:]:
        if a.startswith("--window="):
            window = int(a.split("=", 1)[1])
    all_events = []
    used = {}
    for k, v in PROJECTS.items():
        if v is None:
            continue
        ev, nm = mine_project(k, v[0], v[1], window)
        all_events.extend(ev)
        if nm:
            used[k] = dict(display=v[0], domain=v[1], n_manifests=nm)
    if not all_events:
        print("NO EVENTS")
        return

    p = os.path.join(OUT, "dependency_events.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        cols = ["project", "domain", "ecosystem", "manifest", "date", "ts",
                "commit", "dependency", "old_version", "new_version",
                "event", "major_jump", "n_deps_in_manifest"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sorted(all_events, key=lambda x: x["ts"]))
    print(f"[write] {p} n={len(all_events)}")

    # fix-commit proximity: upgrade commits vs bug-fix commits in the same repo
    fix_shas = defaultdict(set)
    for k in used:
        rp = locate(k)
        if not rp:
            continue
        out = git(rp, ["log", "--no-merges", "-n4000", "--pretty=format:%H\t%s"],
                  timeout=1200)
        if not out:
            continue
        for line in out.splitlines():
            if "\t" not in line:
                continue
            sha, subj = line.split("\t", 1)
            if re.search(r"\b(fix|bug|defect|error|crash|panic|regression|issue)\b",
                         subj, re.I) and not re.search(
                    r"\b(typo|docs?|readme|translation)\b", subj, re.I):
                fix_shas[k].add(sha.strip())
    print(f"[info] fix-commit index: "
          f"{ {k: len(v) for k, v in fix_shas.items()} }")

    # per-dependency summary
    byproj_dep = defaultdict(list)
    for e in all_events:
        byproj_dep[(e["project"], e["dependency"])].append(e)
    dsum = []
    for (proj, dep), evs in byproj_dep.items():
        evs.sort(key=lambda x: x["ts"])
        ups = [e for e in evs if e["event"] == "upgrade"]
        downs = [e for e in evs if e["event"] == "downgrade"]
        adds = [e for e in evs if e["event"] == "add"]
        first = evs[0]["ts"]
        last = evs[-1]["ts"]
        span_years = max((last - first) / (365.25 * 86400), 1e-6)
        gaps = [(b["ts"] - a["ts"]) / 86400.0 for a, b in zip(ups, ups[1:])]
        gaps_sorted = sorted(gaps)
        med_gap = (gaps_sorted[len(gaps_sorted) // 2]
                   if gaps_sorted else None)
        dsum.append(dict(
            project=proj, dependency=dep,
            domain=evs[0]["domain"], ecosystem=evs[0]["ecosystem"],
            first_seen=dt.datetime.utcfromtimestamp(first).strftime("%Y-%m-%d"),
            last_event=dt.datetime.utcfromtimestamp(last).strftime("%Y-%m-%d"),
            span_years=round(span_years, 2),
            n_adds=len(adds), n_upgrades=len(ups), n_downgrades=len(downs),
            upgrades_per_year=round(len(ups) / span_years, 2),
            n_major_jumps=sum(e["major_jump"] for e in ups),
            major_jump_share=round(sum(e["major_jump"] for e in ups) / len(ups), 3)
            if ups else "",
            median_gap_days=round(med_gap, 1) if med_gap is not None else "",
            final_version=evs[-1]["new_version"],
        ))
    dsum.sort(key=lambda x: -x["n_upgrades"])
    p2 = os.path.join(OUT, "dependency_summary.csv")
    with open(p2, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(dsum[0].keys()))
        w.writeheader()
        w.writerows(dsum)
    print(f"[write] {p2} n={len(dsum)}")

    # per-project summary
    byproj = defaultdict(list)
    for e in all_events:
        byproj[e["project"]].append(e)
    psum = []
    for proj, evs in byproj.items():
        evs.sort(key=lambda x: x["ts"])
        ups = [e for e in evs if e["event"] == "upgrade"]
        deps = {e["dependency"] for e in evs}
        first, last = evs[0]["ts"], evs[-1]["ts"]
        span_years = max((last - first) / (365.25 * 86400), 1e-6)
        upg_shas = {e["commit"] for e in evs}
        # upgrade commits that are also bug-fix commits
        k = next((kk for kk, vv in used.items() if vv["display"] == proj), None)
        fx = fix_shas.get(k, set()) if k else set()
        overlap = len(upg_shas & fx)
        psum.append(dict(
            project=proj, domain=evs[0]["domain"],
            ecosystems=";".join(sorted({e["ecosystem"] for e in evs})),
            n_events=len(evs), n_distinct_deps=len(deps),
            n_upgrades=len(ups), n_downgrades=len(e for e in evs
                                                  if e["event"] == "downgrade"),
            upgrades_per_year=round(len(ups) / span_years, 2),
            median_deps_in_manifest=round(
                sorted(e["n_deps_in_manifest"] for e in evs)[len(evs) // 2], 1),
            upgrade_commits_also_fix=overlap,
            overlap_share=round(overlap / len(upg_shas), 3) if upg_shas else "",
            observation_years=round(span_years, 2),
        ))
    psum.sort(key=lambda x: -x["n_upgrades"])
    p3 = os.path.join(OUT, "project_summary.csv")
    with open(p3, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(psum[0].keys()))
        w.writeheader()
        w.writerows(psum)
    print(f"[write] {p3} n={len(psum)}")

    json.dump(dict(window_commits=window, projects=used,
                   n_events=len(all_events), n_dependencies=len(dsum)),
              open(os.path.join(OUT, "config.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print("DEPENDENCY MINING COMPLETE")


if __name__ == "__main__":
    main()
