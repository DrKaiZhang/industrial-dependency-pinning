"""Figures for the JSEP study, projected from data/analysis_out2/."""
import os
import csv
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TABLES = os.path.join(ROOT, "data", "analysis_out2")
FIGDIR = os.path.join(ROOT, "figures")
os.makedirs(FIGDIR, exist_ok=True)

plt.rcParams.update({
    "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "figure.dpi": 160, "savefig.dpi": 160, "savefig.bbox": "tight",
    "axes.grid": True, "grid.alpha": 0.25, "grid.linewidth": 0.5,
    "axes.spines.top": False, "axes.spines.right": False,
})
C = dict(ind="#1f4e79", ref="#c0504d", neutral="#7f7f7f", amber="#d9a03c",
         green="#4f8a4f")


def load(n, d=TABLES):
    p = os.path.join(d, n)
    return list(csv.DictReader(open(p, encoding="utf-8"))) \
        if os.path.exists(p) else []


def f(v, d=float):
    try:
        return d(v)
    except (TypeError, ValueError):
        return None


# ---------------- Fig 1: dependency surface per project, industrial vs reference
def fig_surface():
    rows = load("dependency_surface.csv")
    if not rows:
        return
    rows = sorted(rows, key=lambda r: f(r["n_dependencies"], int))
    fig, ax = plt.subplots(figsize=(6.6, 0.30 * len(rows) + 1.4))
    y = np.arange(len(rows))
    tot = [f(r["n_dependencies"], int) for r in rows]
    pin = [f(r["n_pinned"], int) for r in rows]
    cols = [C["ind"] if r["group"] == "industrial" else C["ref"]
            for r in rows]
    ax.barh(y, tot, color=cols, height=0.66, label="declared")
    ax.barh(y, pin, color="white", height=0.34, edgecolor="k", linewidth=0.6,
            label="version-pinned")
    for i, r in enumerate(rows):
        ax.text(f(r["n_dependencies"], int) + 4, i,
                f"{r['n_dependencies']}  ({r['ecosystems']})", va="center",
                fontsize=6.8)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{r['project'][:22]}" for r in rows], fontsize=7.4)
    ax.set_xlabel("direct third-party dependencies declared")
    ax.set_xlim(0, max(tot) * 1.42)
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    ax.set_title("Fig. 1  Declared dependency surface of 19 repositories\n"
                 "blue = industrial, red = general-purpose reference",
                 loc="left")
    fig.savefig(os.path.join(FIGDIR, "fig1_dependency_surface.png"))
    plt.close(fig)
    print("[fig] fig1")


# ------------------------------- Fig 2: pinned share, industrial vs reference
def fig_pinning():
    """Per-project pinned share, one bar per project, grouped and sorted.

    A boxplot of three reference values against sixteen industrial ones hides
    the distribution that matters here (the industrial projects split into
    "pins everything" and "pins almost nothing", e.g. ESP-IDF at 0%), so every
    project is drawn individually.
    """
    rows = load("dependency_surface.csv")
    if not rows:
        return
    rows = [r for r in rows if f(r["pinned_share"]) is not None]
    rows.sort(key=lambda r: (r["group"] != "industrial",
                             -(f(r["pinned_share"]) or 0)))
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    y = np.arange(len(rows))
    vals = [f(r["pinned_share"]) * 100 for r in rows]
    cols = [C["ind"] if r["group"] == "industrial" else C["ref"]
            for r in rows]
    ax.barh(y, vals, color=cols, height=0.66)
    ax.axvline(50, color="k", ls="--", lw=0.9)
    ax.text(51, len(rows) - 0.4, " 50%", fontsize=7.4, va="top")
    for i, (r, v) in enumerate(zip(rows, vals)):
        ax.text(v + 1.5, i,
                f"{v:.0f}%  of {r['n_dependencies']}",
                va="center", fontsize=6.9)
    ax.set_yticks(y)
    ax.set_yticklabels([r["project"][:24] for r in rows], fontsize=7.4)
    ax.set_xlim(0, 122)
    ax.set_xlabel("share of declared dependencies carrying a pinned version (%)")
    h = [plt.Line2D([], [], color=C["ind"], lw=6,
                    label="industrial (n=16)"),
         plt.Line2D([], [], color=C["ref"], lw=6,
                    label="general-purpose reference (n=3)")]
    ax.legend(handles=h, fontsize=8, frameon=False, ncol=2,
              loc="upper center", bbox_to_anchor=(0.5, -0.16))
    ax.set_title("Fig. 2  Version pinning by project\n"
                 "median 99.4% industrial vs 10.9% reference "
                 "(Cliff's delta 0.625, p = 0.092, n_ref = 3)",
                 loc="left")
    fig.savefig(os.path.join(FIGDIR, "fig2_version_pinning.png"))
    plt.close(fig)
    print("[fig] fig2")


# --------------------------- Fig 3: the two industrial populations
def fig_bimodal():
    rows = load("analysis_rq4_classes.csv")
    if not rows:
        return
    for r in rows:
        for k in ("n_dependencies", "n_pinned"):
            r[k] = f(r[k], int) or 0
        r["pinned_share"] = f(r["pinned_share"])
    rows.sort(key=lambda r: r["n_dependencies"])
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    dense = [r for r in rows if r["klass"] == "dependency-dense"]
    minimal = [r for r in rows if r["klass"] == "dependency-minimal"]
    for r in rows:
        c = C["amber"] if r["klass"] == "dependency-dense" else C["ind"]
        ax.scatter(r["n_dependencies"], (r["pinned_share"] or 0) * 100,
                   s=46, color=c, edgecolor="white", lw=0.6, zorder=3)
    # collision-free labels (pre-jitter tied-y groups, then adjustText)
    from adjustText import adjust_text
    from collections import defaultdict as _dd
    _seen = _dd(int)
    _texts = []
    for r in rows:
        x, y = r["n_dependencies"], (r["pinned_share"] or 0) * 100
        k = _seen[round(y)] 
        _seen[round(y)] += 1
        _texts.append(ax.text(x, y + (k % 3) * 3 - 3, r["project"],
                              fontsize=6.6, color="#303030"))
    ax.set_ylim(-6, 122)
    adjust_text(_texts, ax=ax, expand=(1.35, 1.7),
                arrowprops=dict(arrowstyle="-", color="#909090", lw=0.5))
    ax.set_xscale("log")
    ax.axvline(50, color="k", ls="--", lw=0.9)
    ax.text(52, 8, " 50 = dense/minimal cut", fontsize=7.4, rotation=90,
            va="bottom")
    ax.set_xlabel("direct dependencies declared (log scale)")
    ax.set_ylabel("version-pinned share (%)")
    h = [plt.Line2D([], [], marker="o", ls="", color=C["amber"],
                    label=f"dependency-dense (n={len(dense)})"),
         plt.Line2D([], [], marker="o", ls="", color=C["ind"],
                    label=f"dependency-minimal (n={len(minimal)})")]
    ax.legend(handles=h, fontsize=8, frameon=False, loc="upper center",
              bbox_to_anchor=(0.5, -0.22), ncol=2)
    fig.subplots_adjust(bottom=0.30)
    ax.set_title("Fig. 3  Industrial software is two populations\n"
                 "surface size differs by two orders of magnitude, "
                 "pinning discipline does not", loc="left")
    fig.savefig(os.path.join(FIGDIR, "fig3_two_populations.png"))
    plt.close(fig)
    print("[fig] fig3")


# ------------------------------------------- Fig 4: ecosystem concentration
def fig_eco():
    res_p = os.path.join(TABLES, "analysis_results.json")
    if not os.path.exists(res_p):
        return
    res = json.load(open(res_p, encoding="utf-8"))
    conc = res.get("rq3_concentration", {})
    eco = conc.get("ecosystem_counts", {})
    if not eco:
        return
    items = sorted(eco.items(), key=lambda x: -x[1])
    tot = sum(eco.values())
    fig, axes = plt.subplots(1, 2, figsize=(7.8, 3.0))
    ax = axes[0]
    names = [k for k, _ in items]
    vals = [v for _, v in items]
    ax.bar(names, vals, color=C["ind"], width=0.62)
    for i, v in enumerate(vals):
        ax.text(i, v + max(vals) * 0.02, f"{v}", ha="center", fontsize=7.5)
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7.5)
    ax.set_ylabel("dependency declarations")
    ax.set_title("(a) declarations per ecosystem", loc="left", fontsize=9)
    ax = axes[1]
    shares = [v / tot for v in vals]
    cum = np.cumsum(shares)
    ax.bar(range(len(names)), shares, color=C["ind"], width=0.62,
           label="share")
    ax.plot(range(len(names)), cum, marker="o", ms=3.5, color=C["amber"],
            label="cumulative")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7.5)
    ax.set_ylabel("share of the 1,042 declarations")
    ax.legend(fontsize=8, frameon=False)
    ax.set_title(f"(b) concentration: top-3 ecosystems = "
                 f"{conc.get('most_common_ecosystems_share', 0)*100:.1f}%",
                 loc="left", fontsize=9)
    fig.suptitle("Fig. 4  Industrial dependency concentration is "
                 "ecosystem-level", x=0.01, y=1.04, ha="left", fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGDIR, "fig4_ecosystem_concentration.png"))
    plt.close(fig)
    print("[fig] fig4")


if __name__ == "__main__":
    fig_surface()
    fig_pinning()
    fig_bimodal()
    fig_eco()
    print("FIGURES COMPLETE ->", FIGDIR)
