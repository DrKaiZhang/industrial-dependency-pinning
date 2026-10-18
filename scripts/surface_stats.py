"""Statistics for the JSEP study, computed from the dependency-surface tables.

The evidence base is the declared dependency set of each project at its analysed
commit (see mine_surface.py for exactly what that does and does not include), so
the questions here are the ones that evidence can answer:

  RQ1  How large is the third-party dependency surface of industrial software,
       and how does it compare with general-purpose projects in the same
       ecosystems?
  RQ2  How tightly are industrial dependencies version-pinned compared with
       general-purpose projects?
  RQ3  How concentrated is the dependency surface on a few ecosystems and on a
       few direct dependencies?
  RQ4  Does the dependency surface of industrial software split into a
       dependency-dense class (cloud/Go protocol stacks) and a
       dependency-minimal class (RTOS/firmware), and do the two classes differ
       in pinning?
"""
import os
import re
import sys
import csv
import json
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
TABLES = os.path.join(DATA, "analysis_out2")
sys.path.insert(0, os.path.join(ROOT, "..", "00_shared"))
try:
    import stats_core as ST
except ImportError:  # pragma: no cover
    import stats_tools as ST

DENSE_MIN = 50     # a project declaring >= 50 direct dependencies
INDUSTRIAL_DOMAINS = ("embedded-OS", "RTOS-kernel", "PLC-runtime",
                     "SCADA-runtime", "industrial-messaging",
                     "industrial-protocol-stack", "industrial-IoT-platform",
                     "industrial-HMI", "IoT-middleware",
                     "industrial-robot-control",
                     "industrial-protocol-client",
                     "industrial-energy-control", "industrial-energy-monitor",
                     "industrial-IoT-gateway")


def load(n, d=TABLES):
    p = os.path.join(d, n)
    return list(csv.DictReader(open(p, encoding="utf-8"))) \
        if os.path.exists(p) else []


def num(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def main():
    surf = load("dependency_surface.csv")
    deps = load("declared_dependencies.csv")
    if not surf:
        print("no dependency_surface.csv -- run mine_surface.py first")
        return
    for r in surf:
        r["n_dependencies"] = num(r["n_dependencies"], int) or 0
        r["n_pinned"] = num(r["n_pinned"], int) or 0
        r["pinned_share"] = num(r["pinned_share"])
        r["n_manifests"] = num(r["n_manifests"], int) or 0
        r["n_commits_in_clone"] = num(r["n_commits_in_clone"], int) or 0

    out = {"evidence": dict(
        n_projects=len(surf),
        n_dependency_declarations=len(deps),
        industrial_projects=sum(1 for r in surf if r["group"] == "industrial"),
        reference_projects=sum(1 for r in surf if r["group"] == "reference"),
        industrial_domains=len({r["domain"] for r in surf
                                if r["group"] == "industrial"}),
        method=("manifest content read from a local worktree at the analysed "
                "commit; git used only for metadata, so no historical blob is "
                "fetched -- version-difference claims are therefore out of scope "
                "and only the declared surface and its pinning are measured"))}

    # ------------------------------------------------------------- RQ1 size
    ind = [r["n_dependencies"] for r in surf if r["group"] == "industrial"]
    ref = [r["n_dependencies"] for r in surf if r["group"] == "reference"]
    r1 = []
    for grp, sel in (("industrial", ind), ("reference", ref)):
        if sel:
            r1.append(dict(group=grp, n_projects=len(sel), **ST.describe(sel, grp)))
    for r in sorted(surf, key=lambda x: -x["n_dependencies"]):
        r1.append(dict(group="project", n_projects=1, name=r["project"],
                       domain=r["domain"], n=r["n_dependencies"],
                       median=r["n_dependencies"], mean=r["n_dependencies"],
                       ecosystems=r["ecosystems"]))
    out["rq1_surface_size"] = r1
    out["rq1_summary"] = dict(
        industrial=ST.describe(ind, "industrial"),
        reference=ST.describe(ref, "reference"))
    if len(ind) >= 3 and len(ref) >= 3:
        t = ST.mannwhitney_u(ind, ref)
        t["cliffs_delta"] = ST.cliffs_delta(ind, ref)
        t["median_diff_ci"] = ST.bootstrap_diff_median_ci(ind, ref)
        out["rq1_summary"]["test"] = t
        print(f"[rq1] surface size industrial {ST.quantile(sorted(ind), .5)} "
              f"vs reference {ST.quantile(sorted(ref), .5)} p={t.get('p')}")

    # ----------------------------------------------------------- RQ2 pinning
    pin = [(r["pinned_share"], r["project"], r["group"], r["n_dependencies"],
            r["n_pinned"]) for r in surf]
    r2 = []
    for grp in ("industrial", "reference"):
        v = [x[0] for x in pin if x[2] == grp and x[0] is not None]
        if v:
            r2.append(dict(group=grp, n_projects=len(v), **ST.describe(v, grp)))
    out["rq2_pinning"] = r2
    pi = [x[0] for x in pin if x[2] == "industrial" and x[0] is not None]
    pr = [x[0] for x in pin if x[2] == "reference" and x[0] is not None]
    if len(pi) >= 3 and len(pr) >= 3:
        t = ST.mannwhitney_u(pi, pr)
        t["cliffs_delta"] = ST.cliffs_delta(pi, pr)
        t["median_diff_ci"] = ST.bootstrap_diff_median_ci(pi, pr)
        out["rq2_test"] = t
        print(f"[rq2] pinned share industrial {ST.quantile(sorted(pi), .5):.3f} "
              f"vs reference {ST.quantile(sorted(pr), .5):.3f} p={t.get('p')}")
    out["rq2_per_project"] = [
        dict(project=p, group=g, pinned_share=s, n_deps=nd, n_pinned=np_)
        for s, p, g, nd, np_ in sorted(pin, key=lambda x: -(x[0] or 0))]

    # ---------------------------------------------------- RQ3 concentration
    eco = Counter()
    for d in deps:
        eco[d["ecosystem"]] += 1
    dep_ct = Counter(d["dependency"] for d in deps)
    tot = len(deps)
    out["rq3_concentration"] = dict(
        n_ecosystems=len(eco),
        ecosystem_counts=dict(eco.most_common()),
        n_distinct_dependencies=len(dep_ct),
        top10_dependency_share=round(
            sum(c for _, c in dep_ct.most_common(10)) / tot, 4) if tot else 0,
        top_dependencies=";".join(f"{k}:{v}" for k, v in dep_ct.most_common(15)),
        most_common_ecosystems_share=round(
            sum(c for _, c in eco.most_common(3)) / tot, 4) if tot else 0)
    print(f"[rq3] {len(eco)} ecosystems, top-3 share "
          f"{out['rq3_concentration']['most_common_ecosystems_share']:.3f}, "
          f"top-10 deps share {out['rq3_concentration']['top10_dependency_share']:.3f}")

    # ------------------------------------------- RQ4 dense vs minimal class
    r4 = []
    for r in surf:
        if r["group"] != "industrial":
            continue
        cls = ("dependency-dense" if r["n_dependencies"] >= DENSE_MIN
               else "dependency-minimal")
        r4.append(dict(project=r["project"], domain=r["domain"], klass=cls,
                       n_dependencies=r["n_dependencies"],
                       n_pinned=r["n_pinned"],
                       pinned_share=r["pinned_share"],
                       ecosystems=r["ecosystems"],
                       n_commits=r["n_commits_in_clone"]))
    r4.sort(key=lambda x: -x["n_dependencies"])
    out["rq4_classes"] = r4
    out["rq4_counts"] = dict(Counter(x["klass"] for x in r4))
    dense = [x["pinned_share"] for x in r4
             if x["klass"] == "dependency-dense" and x["pinned_share"] is not None]
    minimal = [x["pinned_share"] for x in r4
               if x["klass"] == "dependency-minimal" and x["pinned_share"] is not None]
    out["rq4_pinning_by_class"] = dict(
        dense=ST.describe(dense, "dense"),
        minimal=ST.describe(minimal, "minimal"))
    if len(dense) >= 3 and len(minimal) >= 3:
        t = ST.mannwhitney_u(dense, minimal)
        t["cliffs_delta"] = ST.cliffs_delta(dense, minimal)
        out["rq4_test"] = t
        print(f"[rq4] pinning dense {ST.quantile(sorted(dense), .5):.3f} "
              f"vs minimal {ST.quantile(sorted(minimal), .5):.3f} p={t.get('p')}")
    print(f"[rq4] classes {out['rq4_counts']}")

    # which ecosystems the dense class uses
    dense_eco = Counter()
    for x in deps:
        if x["group"] == "industrial":
            n = next((y["n_dependencies"] for y in surf
                      if y["project"] == x["project"]), 0)
            if n >= DENSE_MIN:
                dense_eco[x["ecosystem"]] += 1
    out["rq4_dense_ecosystems"] = dict(dense_eco.most_common())

    p = os.path.join(TABLES, "analysis_results.json")
    json.dump(out, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"[write] {p}")

    # tables
    for name, rows in (("analysis_rq1_surface.csv", r1),
                       ("analysis_rq2_pinning.csv", r2),
                       ("analysis_rq4_classes.csv", r4)):
        if rows:
            keys = []
            for x in rows:
                for k in x:
                    if k not in keys:
                        keys.append(k)
            with open(os.path.join(TABLES, name), "w", newline="",
                      encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys, restval="",
                                   extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            print(f"[write] {name} n={len(rows)}")
    print("STATS COMPLETE")


if __name__ == "__main__":
    main()
