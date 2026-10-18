#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
03_JSEP_dependency_evolution / analyse.py

Statistical analysis of the mined dependency-update history. Every number quoted
in the manuscript is produced here.

Research questions
  RQ1  How frequently do industrial-software projects update their declared
       third-party dependencies, and how does that cadence compare with
       general-purpose projects in the same ecosystems?
  RQ2  How long is the lag between consecutive updates of the same dependency,
       and how does it vary across ecosystems and across industrial domains?
  RQ3  Are dependency-upgrade commits associated with bug-fix activity in the
       same repository, beyond what commit volume alone would predict?
  RQ4  How concentrated is update activity on a few dependencies, and do
       explicit major-version jumps occur as upgrades or as downgrades/reverts?
"""
import os
import re
import sys
import csv
import json
import math
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "00_shared"))
import stats_tools as ST  # noqa: E402

# Domain label -> industrial / general-purpose reference
INDUSTRIAL = ("embedded-OS", "RTOS-kernel", "PLC-runtime", "SCADA-runtime",
              "industrial-messaging", "industrial-protocol-stack",
              "industrial-IoT-platform", "industrial-HMI", "IoT-middleware",
              "industrial-robot-control", "industrial-protocol-client",
              "industrial-energy-control", "industrial-energy-monitor",
              "industrial-IoT-gateway")


def load(name):
    p = os.path.join(DATA, name)
    return list(csv.DictReader(open(p, encoding="utf-8"))) if os.path.exists(p) else []


def main():
    ev = load("dependency_events.csv")
    if not ev:
        print("NO EVENTS -- run mine_dependencies.py first")
        return
    for e in ev:
        e["major_jump"] = int(e["major_jump"])
        e["n_deps_in_manifest"] = int(e["n_deps_in_manifest"])
    dep = load("dependency_summary.csv")
    proj = load("project_summary.csv")
    out = {}
    out["population"] = dict(
        n_events=len(ev), n_projects=len({e["project"] for e in ev}),
        n_dependencies=len({(e["project"], e["dependency"]) for e in ev}),
        n_ecosystems=len({e["ecosystem"] for e in ev}),
        n_industrial_projects=len({e["project"] for e in ev
                                   if e["domain"] in INDUSTRIAL}),
        event_types=dict(Counter(e["event"] for e in ev)),
    )
    print(f"[population] {out['population']}")

    # ------------------------------------------------------------ RQ1 cadence
    r1 = []
    for p in proj:
        grp = ("industrial" if p["domain"] in INDUSTRIAL else "reference")
        r1.append(dict(group=grp, project=p["project"], domain=p["domain"],
                       n_events=int(p["n_events"]),
                       n_distinct_deps=int(p["n_distinct_deps"]),
                       n_upgrades=int(p["n_upgrades"]),
                       n_downgrades=int(p["n_downgrades"]),
                       upgrades_per_year=float(p["upgrades_per_year"]),
                       median_deps_in_manifest=float(
                           p["median_deps_in_manifest"]),
                       observation_years=float(p["observation_years"])))
    ind = [x["upgrades_per_year"] for x in r1 if x["group"] == "industrial"]
    ref = [x["upgrades_per_year"] for x in r1 if x["group"] == "reference"]
    out["rq1"] = dict(
        per_project=r1,
        industrial=dict(n=len(ind), median_upgrades_per_year=ST.quantile(
            sorted(ind), .5) if ind else None,
            mean_upgrades_per_year=round(
                sum(ind) / len(ind), 3) if ind else None),
        reference=dict(n=len(ref), median_upgrades_per_year=ST.quantile(
            sorted(ref), .5) if ref else None,
            mean_upgrades_per_year=round(
                sum(ref) / len(ref), 3) if ref else None))
    if len(ind) >= 3 and len(ref) >= 3:
        t = ST.mannwhitney_u(ind, ref)
        t["cliffs_delta"] = ST.cliffs_delta(ind, ref)
        t["median_diff_ci"] = ST.bootstrap_diff_median_ci(ind, ref)
        out["rq1"]["test_industrial_vs_reference"] = t
        print(f"[rq1] industrial {out['rq1']['industrial']} vs "
              f"reference {out['rq1']['reference']} p={t.get('p')}")
    ST.write_csv(r1, os.path.join(DATA, "analysis_rq1_cadence.csv"))

    # per ecosystem
    eco = defaultdict(list)
    for x in r1:
        for e in {e["ecosystem"] for e in ev if e["project"] == x["project"]}:
            eco[e].append(x["upgrades_per_year"])
    eco_rows = [dict(ecosystem=k, n_projects=len(v),
                     median_upgrades_per_year=ST.quantile(sorted(v), .5),
                     mean=round(sum(v) / len(v), 3))
                for k, v in sorted(eco.items(), key=lambda x: -len(x[1]))]
    ST.write_csv(eco_rows, os.path.join(DATA, "analysis_rq1_ecosystem.csv"))
    out["rq1"]["per_ecosystem"] = eco_rows

    # ------------------------------------------------------------ RQ2 lag
    lags = [(float(d["median_gap_days"]), d["project"], d["ecosystem"],
             d["domain"], int(d["n_upgrades"]))
            for d in dep if d["median_gap_days"] not in ("", None)
            and int(d["n_upgrades"]) >= 3]
    if lags:
        v = [x[0] for x in lags]
        out["rq2"] = dict(
            n_dependencies_with_3plus_upgrades=len(v),
            median_gap_days=ST.quantile(sorted(v), .5),
            q1=ST.quantile(sorted(v), .25), q3=ST.quantile(sorted(v), .75),
            p90=ST.quantile(sorted(v), .9),
            ci=ST.bootstrap_median_ci(v),
            longest=[dict(project=x[1], ecosystem=x[2], median_gap_days=x[0])
                     for x in sorted(lags, reverse=True)[:10]])
        byeco = defaultdict(list)
        for g, pr, e, dm, n in lags:
            byeco[e].append(g)
        out["rq2"]["per_ecosystem"] = [
            dict(ecosystem=k, n=len(x), median_gap_days=ST.quantile(sorted(x), .5))
            for k, x in sorted(byeco.items(), key=lambda y: -len(y[1]))]
        indl = [x[0] for x in lags if x[3] in INDUSTRIAL]
        refl = [x[0] for x in lags if x[3] not in INDUSTRIAL]
        if len(indl) >= 5 and len(refl) >= 5:
            t = ST.mannwhitney_u(indl, refl)
            t["cliffs_delta"] = ST.cliffs_delta(indl, refl)
            out["rq2"]["test_industrial_vs_reference_lag"] = t
            print(f"[rq2] lag industrial n={len(indl)} med="
                  f"{ST.quantile(sorted(indl), .5)} vs reference n={len(refl)}"
                  f" med={ST.quantile(sorted(refl), .5)} p={t.get('p')}")
        print(f"[rq2] median gap {out['rq2']['median_gap_days']} days "
              f"over n={len(v)} dependencies")
    ST.write_csv([dict(project=x[1], ecosystem=x[2], domain=x[3],
                       n_upgrades=x[4], median_gap_days=x[0])
                  for x in sorted(lags, key=lambda y: -y[0])],
                 os.path.join(DATA, "analysis_rq2_lag.csv"))

    # -------------------------------------------------- RQ3 fix-proximity
    pr = []
    for p in proj:
        if not p["overlap_share"]:
            continue
        grp = ("industrial" if p["domain"] in INDUSTRIAL else "reference")
        pr.append(dict(group=grp, project=p["project"],
                       n_upgrades=int(p["n_upgrades"]),
                       upgrade_commits_also_fix=int(
                           p["upgrade_commits_also_fix"]),
                       overlap_share=float(p["overlap_share"])))
    out["rq3"] = dict(per_project=pr)
    for g in ("industrial", "reference"):
        v = [x["overlap_share"] for x in pr if x["group"] == g]
        if v:
            out["rq3"][g] = dict(n=len(v),
                                 median_overlap_share=ST.quantile(sorted(v), .5),
                                 mean=round(sum(v) / len(v), 4),
                                 ci=ST.bootstrap_median_ci(v))
    vi = [x["overlap_share"] for x in pr if x["group"] == "industrial"]
    vr = [x["overlap_share"] for x in pr if x["group"] == "reference"]
    if len(vi) >= 3 and len(vr) >= 3:
        t = ST.mannwhitney_u(vi, vr)
        t["cliffs_delta"] = ST.cliffs_delta(vi, vr)
        out["rq3"]["test"] = t
        print(f"[rq3] overlap industrial med={ST.quantile(sorted(vi), .5)} "
              f"reference med={ST.quantile(sorted(vr), .5)} p={t.get('p')}")
    ST.write_csv(pr, os.path.join(DATA, "analysis_rq3_fix_proximity.csv"))

    # ------------------------------------------- RQ4 concentration + jumps
    ups = [e for e in ev if e["event"] == "upgrade"]
    byproj = Counter(e["project"] for e in ups)
    bydep = Counter((e["project"], e["dependency"]) for e in ups)
    tot_up = len(ups)
    out["rq4"] = dict(
        n_upgrades=tot_up,
        major_jumps=sum(e["major_jump"] for e in ups),
        major_jump_share=round(sum(e["major_jump"] for e in ups) / tot_up, 4)
        if tot_up else 0,
        top10_dependency_share=round(
            sum(c for _, c in bydep.most_common(10)) / tot_up, 4) if tot_up else 0,
        n_distinct_dependencies_upgraded=len(bydep),
        concentration=dict(
            top1=f"{bydep.most_common(1)[0][0][1]}:{bydep.most_common(1)[0][1]}"
            if bydep else "",
            top10=";".join(f"{k[1]}:{v}" for k, v in bydep.most_common(10))),
        project_concentration=round(
            sum(c for _, c in byproj.most_common(3)) / tot_up, 4) if tot_up else 0,
    )
    # downgrades: a re-pinned older version, a real risk signal
    downs = [e for e in ev if e["event"] == "downgrade"]
    out["rq4"]["n_downgrades"] = len(downs)
    out["rq4"]["downgrade_projects"] = len({e["project"] for e in downs})
    out["rq4"]["downgrade_examples"] = [
        dict(project=e["project"], dependency=e["dependency"],
             old=e["old_version"], new=e["new_version"], date=e["date"])
        for e in downs[:15]]
    # revert-like: downgrade immediately after an upgrade on the same dependency
    bydep_seq = defaultdict(list)
    for e in ev:
        if e["event"] in ("upgrade", "downgrade"):
            bydep_seq[(e["project"], e["dependency"])].append(e)
    reverts = 0
    for k, v in bydep_seq.items():
        v.sort(key=lambda x: x["date"])
        for a, b in zip(v, v[1:]):
            if a["event"] == "upgrade" and b["event"] == "downgrade":
                reverts += 1
    out["rq4"]["upgrade_then_downgrade_pairs"] = reverts
    print(f"[rq4] upgrades={tot_up} major_jumps="
          f"{out['rq4']['major_jumps']} top10dep_share="
          f"{out['rq4']['top10_dependency_share']} reverts={reverts}")
    ST.write_csv([dict(dependency=k[1], project=k[0], n_upgrades=v)
                  for k, v in bydep.most_common()],
                 os.path.join(DATA, "analysis_rq4_dependency_concentration.csv"))

    # manifest growth over time per project (dependency accumulation)
    growth = defaultdict(list)
    for e in ev:
        growth[e["project"]].append((e["date"], e["n_deps_in_manifest"]))
    grows = []
    for prj, v in growth.items():
        v.sort()
        first = v[0][1]
        last = v[-1][1]
        yrs = max(1.0, (len(set(x[0][:4] for x in v))))
        grows.append(dict(project=prj, first_deps=first, last_deps=last,
                          delta=last - first, per_year=round((last - first) / yrs, 3)))
    grows.sort(key=lambda x: -x["delta"])
    out["rq4"]["manifest_growth"] = grows[:20]
    ST.write_csv(grows, os.path.join(DATA, "analysis_rq4_manifest_growth.csv"))

    ST.write_json(out, os.path.join(DATA, "analysis_results.json"))
    print("ANALYSIS COMPLETE")


if __name__ == "__main__":
    main()
