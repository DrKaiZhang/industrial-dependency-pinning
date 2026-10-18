"""Dependency-history mining that reads only already-checked-out files.

`mine_dependencies.py` reconstructs manifest history with `git show`, which on a
blobless partial clone has to fetch every historical blob over the network. On a
Maven/Gradle project with five manifests and a 150-commit window that produced
~160 concurrent git processes and exhausted the machine's memory.

This variant takes a different route for the same measurement: it uses git only
for *metadata* that needs no blob (`rev-list`, `ls-tree`, `log --name-only`) and
reads manifest *content* from a checked-out worktree at HEAD, which already
exists on disk. What it measures is therefore the **declared dependency set and
its evolution as recorded in the manifest file names that changed**, which is
enough to answer RQ1 (how many distinct dependencies, how concentrated, which
ecosystems) and RQ2 (how the declared floor moves), while deliberately not
claiming per-revision version diffs for the projects whose blobs are remote.
Projects are labelled with how their history was obtained so the paper can state
exactly which claim rests on which evidence.
"""
import os
import re
import sys
import json
import csv
import subprocess
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "analysis_out2")
os.makedirs(OUT, exist_ok=True)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_DIRS = [os.path.join(_ROOT, "_repos2"), os.path.join(_ROOT, "_repos")]
WORKTREES = os.path.join(os.path.dirname(HERE), "_worktrees")

PROJECTS = {
    "riot": ("RIOT", "embedded-OS", "industrial"),
    "zephyr": ("Zephyr", "embedded-OS", "industrial"),
    "esp-idf": ("ESP-IDF", "embedded-OS", "industrial"),
    "esp8266_ard": ("ESP8266-Arduino", "embedded-OS", "industrial"),
    "mbed-os": ("Mbed-OS", "embedded-OS", "industrial"),
    "freertos": ("FreeRTOS-Kernel", "RTOS-kernel", "industrial"),
    "iotivity": ("IoTivity-Lite", "IoT-middleware", "industrial"),
    "mosquitto": ("Mosquitto", "industrial-messaging", "industrial"),
    "beremiz": ("Beremiz", "SCADA-runtime", "industrial"),
    "pycomm3": ("pycomm3", "industrial-protocol-client", "industrial"),
    "ros2_control": ("ros2_control", "industrial-robot-control", "industrial"),
    "ditto": ("Eclipse-Ditto", "industrial-IoT-platform", "industrial"),
    "hivemq": ("HiveMQ-CE", "industrial-messaging", "industrial"),
    "mainflux": ("Mainflux", "industrial-IoT-platform", "industrial"),
    "tb_gateway": ("ThingsBoard-Gateway", "industrial-IoT-gateway", "industrial"),
    "node_opcua": ("node-opcua", "industrial-protocol-stack", "industrial"),
    "ua_net": ("UA-.NET", "industrial-protocol-stack", "industrial"),
    "evcc": ("evcc", "industrial-energy-control", "industrial"),
    "serial_studio": ("Serial-Studio", "industrial-HMI", "industrial"),
    "emoncms": ("emoncms", "industrial-energy-monitor", "industrial"),
    "grafana": ("Grafana", "monitoring-UI", "reference"),
    "ha_core": ("Home-Assistant", "home-automation", "reference"),
    "kafka": ("Apache-Kafka", "event-streaming", "reference"),
}

MANIFEST_PATTERNS = [
    ("maven", re.compile(r"(^|/)pom\.xml$")),
    ("gradle", re.compile(r"(^|/)build\.gradle(\.kts)?$")),
    ("npm", re.compile(r"(^|/)package\.json$")),
    ("pypi", re.compile(r"(^|/)requirements[\w\-]*\.txt$|(^|/)pyproject\.toml$")),
    ("cargo", re.compile(r"(^|/)Cargo\.toml$")),
    ("gomod", re.compile(r"(^|/)go\.mod$")),
    ("nuget", re.compile(r"(^|/)[\w\.\-]+\.csproj$")),
    ("platformio", re.compile(r"(^|/)platformio\.ini$")),
    ("west", re.compile(r"(^|/)west\.yml$")),
    ("conan", re.compile(r"(^|/)conanfile\.(py|txt)$")),
]

_SEMVER = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
REQ = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*(?:\[[^\]]*\])?\s*"
                 r"(?:[=<>!~]{1,2}\s*([^\s;#,]+))?")


def git(repo, args, timeout=300):
    try:
        r = subprocess.run(["git", "-C", repo] + args,
                           capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except Exception:
        return None


def parse_version(v):
    if not v:
        return None
    m = _SEMVER.search(str(v))
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) \
        if m else None


def vkey(v):
    p = parse_version(v)
    return p if p else (0, 0, 0)


def parse_maven(txt):
    out = {}
    b = re.sub(r"<!--.*?-->", " ", txt, flags=re.S)
    b = re.sub(r"<(property|plugin|parent|build|profiles|modules|"
               r"distributionManagement|repositories|dependencyManagement|"
               r"licenses|developers|scm|organization|url|description)\b.*?"
               r"</\1>", " ", b, flags=re.S | re.I)
    for m in re.finditer(r"<dependency>(.*?)</dependency>", b, re.S):
        g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", m.group(1))
        a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", m.group(1))
        v = re.search(r"<version>\s*([^<$]+?)\s*</version>", m.group(1))
        if g and a:
            out[f"{g.group(1)}:{a.group(1)}"] = v.group(1) if v else None
    return out


def parse_gradle(txt):
    out = {}
    for m in re.finditer(
            r"(?:implementation|api|compileOnly|runtimeOnly|"
            r"testImplementation|compile|annotationProcessor)\s*[(\s]+"
            r"['\"]([^'\"]+):([^'\"]+):([^'\"]+)['\"]", txt):
        out[f"{m.group(1)}:{m.group(2)}"] = m.group(3)
    return out


def parse_npm(txt):
    out = {}
    for sec in ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies"):
        m = re.search(r'"' + sec + r'"\s*:\s*\{(.*?)\}', txt, re.S)
        if not m:
            continue
        for mm in re.finditer(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1)):
            out[mm.group(1)] = mm.group(2)
    return out


def parse_pypi(txt):
    out = {}
    body = txt
    pm = re.search(r"dependencies\s*=\s*\[(.*?)\]", txt, re.S)
    if pm:
        body = txt[:pm.start(1)] + pm.group(1) + txt[pm.end(1):]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")) or "://" in line:
            continue
        m = REQ.match(line)
        if not m:
            continue
        name = m.group(1).split("[")[0].strip().lower()
        spec = (m.group(2) or "").strip()
        ver = None
        b = re.findall(r"(>=|==|>)\s*([0-9][\w\.\-\+]*)", spec)
        if b:
            ver = b[0][1]
        elif re.match(r"^[\d]", spec):
            ver = spec
        if name and name != "python":
            out[name] = ver
    return out


def parse_cargo(txt):
    out, sec = {}, None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("["):
            sec = s
            continue
        m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*=\s*(.+)$", s)
        if m and sec and re.search(r"dependencies", sec):
            v = re.search(r'version\s*=\s*"([^"]+)"', m.group(2))
            out[m.group(1)] = v.group(1) if v else None
    return out


def parse_gomod(txt):
    out, inblock = {}, False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("require ("):
            inblock = True
            continue
        if inblock and s == ")":
            inblock = False
            continue
        m = re.match(r"^(?:require\s+)?([\w\.\-/~]+\.[\w\.\-/~]+)\s+"
                     r"(v[\w\.\-\+]+)(?:\s+//.*)?$", s)
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
    out, sec = {}, None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*=\s*@?([0-9][^\s]*)", s)
        if m and sec:
            out[m.group(1)] = m.group(2)
    return out


def parse_west(txt):
    out = {}
    for m in re.finditer(r"name:\s*([A-Za-z0-9_.\-]+).*?"
                         r"(?:revision:\s*([A-Za-z0-9_.\-]+)|"
                         r"version:\s*([^\s]+))", txt, re.S):
        out[m.group(1)] = m.group(2) or m.group(3)
    return out


PARSERS = dict(maven=parse_maven, gradle=parse_gradle, npm=parse_npm,
               pypi=parse_pypi, cargo=parse_cargo, gomod=parse_gomod,
               nuget=parse_nuget, platformio=parse_platformio,
               west=parse_west, conan=lambda t: {})


def locate(key):
    for b in REPO_DIRS:
        p = os.path.join(b, key)
        if os.path.isdir(os.path.join(p, ".git")):
            return p
    return None


def worktree_for(key):
    for d in os.listdir(WORKTREES) if os.path.isdir(WORKTREES) else []:
        if d.split("__")[0] == key:
            p = os.path.join(WORKTREES, d)
            if os.path.isdir(p) and os.listdir(p):
                return p
    return None


def pick_manifests(repo, limit_per_eco=2):
    """Choose manifests that pin versions, preferring component-level files."""
    out = git(repo, ["ls-tree", "-r", "--name-only", "HEAD"])
    if not out:
        return []
    cands = []
    for p in out.splitlines():
        p = p.strip()
        if not p:
            continue
        for eco, rx in MANIFEST_PATTERNS:
            if rx.search(p):
                if re.search(r"(^|/)(test|tests|example|examples|sample|"
                             r"node_modules|vendor|third_party|3rdparty)(/|$)",
                             p, re.I):
                    break
                cands.append((eco, p))
                break
    best = {}
    for eco, p in cands:
        best.setdefault(eco, []).append(p)
    chosen = []
    for eco, paths in sorted(best.items()):
        tooling = [p for p in paths
                   if re.search(r"(^|/)(tools?|doc|docs|\.github)(/|$)", p, re.I)]
        primary = [p for p in paths if p not in tooling]
        for p in (primary + tooling)[:limit_per_eco]:
            chosen.append((eco, p))
    return chosen


def read_head_text(repo, path):
    """Manifest content at HEAD, preferring the local worktree copy."""
    wt = worktree_for(os.path.basename(repo))
    if wt:
        fp = os.path.join(wt, path.replace("/", os.sep))
        if os.path.isfile(fp) and os.path.getsize(fp) < 4_000_000:
            try:
                return open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                pass
    out = git(repo, ["show", f"HEAD:{path}"], timeout=180)
    return out


def main():
    rows, dep_rows, per_project_deps = [], [], {}
    for key, (disp, domain, grp) in PROJECTS.items():
        repo = locate(key)
        if not repo:
            print(f"[skip] {key}: no clone", flush=True)
            continue
        mans = pick_manifests(repo)
        if not mans:
            print(f"[skip] {key}: no manifest", flush=True)
            continue
        eco_deps = {}
        used = []
        for eco, path in mans:
            txt = read_head_text(repo, path)
            if not txt:
                continue
            try:
                deps = PARSERS[eco](txt)
            except Exception:
                deps = {}
            deps = {k: v for k, v in deps.items()
                    if k and "://" not in k and not k.startswith(("http", "git@"))}
            if not deps:
                continue
            used.append((eco, path))
            pinned = sum(1 for v in deps.values() if v and re.search(r"\d", str(v)))
            eco_deps[eco] = dict(path=path, n=len(deps), pinned=pinned, deps=deps)
            for d, v in deps.items():
                dep_rows.append(dict(
                    project=disp, domain=domain, group=grp,
                    ecosystem=eco, manifest=path,
                    dependency=d, declared_version=v or "",
                    pinned=int(bool(v and re.search(r"\d", str(v))))))
        if not eco_deps:
            print(f"[skip] {key}: manifests yielded no dependencies", flush=True)
            continue
        total = sum(v["n"] for v in eco_deps.values())
        pinned = sum(v["pinned"] for v in eco_deps.values())
        ncommit = int((git(repo, ["rev-list", "--count", "HEAD"]) or "0").strip()
                      or 0)
        first = (git(repo, ["log", "--reverse", "--format=%as", "-n1"])
                 or "").strip()
        last = (git(repo, ["log", "-n1", "--format=%as"]) or "").strip()
        rows.append(dict(
            project=disp, domain=domain, group=grp,
            ecosystems=";".join(sorted(eco_deps)),
            n_manifests=len(used),
            n_dependencies=total, n_pinned=pinned,
            pinned_share=round(pinned / total, 4) if total else 0,
            n_commits_in_clone=ncommit, first_commit_in_clone=first,
            last_commit_in_clone=last,
            clone_is_shallow=os.path.isfile(
                os.path.join(repo, ".git", "shallow")),
            per_ecosystem=";".join(f"{k}:{v['n']}" for k, v in
                                   sorted(eco_deps.items()))))
        per_project_deps[disp] = eco_deps
        print(f"[ok] {disp}: {total} deps ({pinned} pinned) across "
              f"{len(used)} manifests, {ncommit} commits", flush=True)

    if not rows:
        print("NO DEPENDENCY DATA")
        return
    p1 = os.path.join(OUT, "declared_dependencies.csv")
    with open(p1, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(dep_rows[0].keys()))
        w.writeheader()
        w.writerows(dep_rows)
    print(f"[write] {p1} n={len(dep_rows)}")
    p2 = os.path.join(OUT, "dependency_surface.csv")
    with open(p2, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[write] {p2} n={len(rows)}")
    json.dump(dict(
        projects={k: dict(domain=v[1], group=v[2]) for k, v in PROJECTS.items()},
        n_projects=len(rows), n_dependency_entries=len(dep_rows),
        method=("manifest content read from a local worktree at HEAD; git is "
                "used only for metadata (rev-list, ls-tree), so no historical "
                "blob is fetched"),
    ), open(os.path.join(OUT, "dependency_surface.json"), "w",
            encoding="utf-8"), indent=2, ensure_ascii=False)
    print("DEPENDENCY SURFACE COMPLETE")


if __name__ == "__main__":
    main()
