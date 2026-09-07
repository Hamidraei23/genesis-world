"""
Result plotting for the test1 / test2 / test3 comparison.

Reads:
  - TensorBoard event files in logs/<test>/          (learning curves, retention)
  - eval JSONs written by eval_tests_franka.py:
        logs/<test>/eval/eval_<rand|clean>_seed<seed>.json
                                                     (success rate, failure mix,
                                                      regrasps, z_improvement)

Figures (PNG, written to --out-dir, default logs/plots):
  curves      pairwise learning curves: test2-vs-test3, test2-vs-test1, test3-vs-test1
  retention   reward normalized to each run's own pre-switch plateau + retention bars
  success     success rate per test (bar)
  failures    outcome mix per test; regrasp-limit fails are COUNTED AS TIMEOUT
  regrasp     regrasps per success and mean z_improvement per regrasp

Usage:
    python3 examples/rigid/plot_results_franka.py                 # all figures
    python3 examples/rigid/plot_results_franka.py --curves
    python3 examples/rigid/plot_results_franka.py --success --failures
    python3 examples/rigid/plot_results_franka.py --eval-suffix clean --seed 1
    python3 examples/rigid/plot_results_franka.py --smooth 15 --switch 450
"""

import argparse
import glob
import itertools
import json
import os

import matplotlib
matplotlib.use("Agg")  # headless – required inside Docker (no $DISPLAY)
import matplotlib.pyplot as plt
import numpy as np

COLORS = {"test1": "tab:green", "test2": "tab:orange", "test3": "tab:purple"}
LABELS = {
    "test1": "test1 (10 obs: force, no history)",
    "test2": "test2 (16 obs: force + history)",
    "test3": "test3 (14 obs: history, no force)",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def read_scalar_series(log_dir: str, tag: str):
    """Merge a scalar tag across every event file in a run directory."""
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

    series = {}
    for path in sorted(glob.glob(os.path.join(log_dir, "events.out.tfevents.*"))):
        acc = EventAccumulator(path, size_guidance={"scalars": 0})
        acc.Reload()
        if tag not in acc.Tags().get("scalars", []):
            continue
        for ev in acc.Scalars(tag):
            series[int(ev.step)] = float(ev.value)  # later files win on overlap
    steps = np.array(sorted(series))
    values = np.array([series[s] for s in steps])
    return steps, values


def load_curves(tests, tag):
    out = {}
    for t in tests:
        steps, values = read_scalar_series(f"logs/{t}", tag)
        if steps.size == 0:
            print(f"[WARNING] no '{tag}' data in logs/{t}; skipping it in curve plots")
            continue
        out[t] = (steps, values)
    return out


def load_eval(tests, suffix, seed):
    out = {}
    for t in tests:
        path = f"logs/{t}/eval/eval_{suffix}_seed{seed}.json"
        if not os.path.exists(path):
            print(f"[WARNING] {path} not found; run eval_tests_franka.py first "
                  f"(skipping {t} in eval-based plots)")
            continue
        with open(path) as f:
            out[t] = json.load(f)
    return out


def smooth(values, window):
    if window <= 1 or values.size < window:
        return values
    kernel = np.ones(window) / window
    return np.convolve(values, kernel, mode="valid")


def smooth_steps(steps, window):
    if window <= 1 or steps.size < window:
        return steps
    return steps[window - 1:]


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def fig_curves(curves, args):
    """Pairwise learning-curve comparison: one row per pair."""
    pairs = [p for p in [("test2", "test3"), ("test2", "test1"), ("test3", "test1")]
             if p[0] in curves and p[1] in curves]
    if not pairs:
        print("[WARNING] not enough curve data for pairwise plots")
        return

    fig, axes = plt.subplots(len(pairs), 1, figsize=(11, 3.6 * len(pairs)), sharex=True)
    if len(pairs) == 1:
        axes = [axes]

    for ax, (a, b) in zip(axes, pairs):
        for t in (a, b):
            steps, values = curves[t]
            ax.plot(steps, values, color=COLORS[t], alpha=0.18, lw=0.8)
            ax.plot(smooth_steps(steps, args.smooth), smooth(values, args.smooth),
                    color=COLORS[t], lw=1.8, label=LABELS.get(t, t))
        ax.axvline(args.switch, color="k", ls="--", lw=1, alpha=0.6)
        ax.text(args.switch, ax.get_ylim()[1], " randomize on", va="top", fontsize=8, alpha=0.7)
        ax.set_ylabel(args.tag.split("/")[-1])
        ax.set_title(f"{a} vs {b}")
        ax.legend(fontsize=8, loc="lower right")
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel("iteration")

    fig.tight_layout()
    out = os.path.join(args.out_dir, "learning_curves_pairwise.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"Wrote {out}")


def fig_retention(curves, args):
    """Left: reward normalized to each run's own pre-switch plateau.
    Right: plateau / trough / final bars with retention percentages."""
    stats = {}
    for t, (steps, values) in curves.items():
        pl = (steps >= args.switch - 50) & (steps < args.switch)
        tr = (steps >= args.switch) & (steps < args.switch + 25)
        last = steps.max()
        fi = steps >= last - 50
        if not (pl.any() and tr.any() and fi.any()):
            print(f"[WARNING] {t}: not enough data around the switch; skipping in retention")
            continue
        stats[t] = dict(
            plateau=float(values[pl].mean()),
            trough=float(values[tr].mean()),
            final=float(values[fi].mean()),
        )
    if not stats:
        return

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(13, 4.6), width_ratios=[1.6, 1])

    # Normalized curves
    for t, (steps, values) in curves.items():
        if t not in stats:
            continue
        norm = values / stats[t]["plateau"]
        ax0.plot(smooth_steps(steps, args.smooth), smooth(norm, args.smooth),
                 color=COLORS[t], lw=1.8, label=LABELS.get(t, t))
    ax0.axvline(args.switch, color="k", ls="--", lw=1, alpha=0.6)
    ax0.axhline(1.0, color="k", lw=0.8, alpha=0.4)
    ax0.set_xlabel("iteration")
    ax0.set_ylabel("reward / own pre-switch plateau")
    ax0.set_title("Retention across the randomization switch")
    ax0.legend(fontsize=8, loc="lower left")
    ax0.grid(alpha=0.3)

    # Bars
    names = list(stats)
    x = np.arange(len(names))
    w = 0.27
    for i, key in enumerate(("plateau", "trough", "final")):
        vals = [stats[t][key] for t in names]
        ax1.bar(x + (i - 1) * w, vals, w, label=key,
                color=[COLORS[t] for t in names], alpha=(0.45, 0.75, 1.0)[i])
    for xi, t in zip(x, names):
        ret = stats[t]["final"] / stats[t]["plateau"]
        ax1.text(xi + w, stats[t]["final"], f" {ret:.0%}", ha="left", va="bottom", fontsize=9)
    ax1.set_xticks(x, names)
    ax1.set_ylabel("Train/mean_reward")
    ax1.set_title("plateau  /  trough  /  final   (label = retention)")
    ax1.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    out = os.path.join(args.out_dir, "retention_switch.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"Wrote {out}")


def fig_success(evals, args):
    names = list(evals)
    if not names:
        return
    succ = [evals[t]["stats"]["success_rate"] for t in names]
    n_ep = [evals[t]["stats"]["episodes"] for t in names]

    fig, ax = plt.subplots(figsize=(6.5, 4.4))
    x = np.arange(len(names))
    ax.bar(x, succ, color=[COLORS[t] for t in names])
    for xi, v, n in zip(x, succ, n_ep):
        # 1-sigma binomial error bar for honesty about sample size
        err = np.sqrt(v * (1 - v) / n) if n else 0.0
        ax.errorbar(xi, v, yerr=err, color="k", capsize=4, lw=1)
        ax.text(xi, v + err, f" {v:.1%}", ha="center", va="bottom", fontsize=10)
    ax.set_xticks(x, [LABELS.get(t, t) for t in names], fontsize=8)
    ax.set_ylim(0, min(1.0, max(succ) * 1.35 + 0.05))
    ax.set_ylabel("success rate")
    ax.set_title(f"Success rate ({args.eval_suffix} eval, {n_ep[0]} episodes, ±1σ)")
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    out = os.path.join(args.out_dir, f"success_rate_{args.eval_suffix}.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"Wrote {out}")


def fig_failures(evals, args):
    """Outcome mix. Per request: regrasp-limit fails are folded into timeout."""
    names = list(evals)
    if not names:
        return
    succ = np.array([evals[t]["stats"]["success_rate"] for t in names])
    geom = np.array([evals[t]["stats"]["geometric_fail_rate"] for t in names])
    tout = np.array([evals[t]["stats"]["timeout_rate"]
                     + evals[t]["stats"]["regrasp_limit_fail_rate"] for t in names])

    fig, ax = plt.subplots(figsize=(7, 4.6))
    x = np.arange(len(names))
    bottom = np.zeros(len(names))
    for vals, label, color in ((succ, "success", "tab:green"),
                               (geom, "geometric fail", "tab:red"),
                               (tout, "timeout (incl. regrasp limit)", "tab:gray")):
        ax.bar(x, vals, bottom=bottom, label=label, color=color)
        for xi, v, b in zip(x, vals, bottom):
            if v > 0.04:
                ax.text(xi, b + v / 2, f"{v:.0%}", ha="center", va="center",
                        fontsize=9, color="white")
        bottom += vals
    ax.set_xticks(x, [LABELS.get(t, t) for t in names], fontsize=8)
    ax.set_ylim(0, 1.0)
    ax.set_ylabel("fraction of episodes")
    ax.set_title(f"Terminal outcomes ({args.eval_suffix} eval)")
    ax.legend(fontsize=8, loc="center left", bbox_to_anchor=(1.01, 0.5))
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    out = os.path.join(args.out_dir, f"failure_causes_{args.eval_suffix}.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"Wrote {out}")


def fig_regrasp(evals, args):
    names = list(evals)
    if not names:
        return
    reg = [evals[t]["stats"].get("mean_regrasps_on_success") for t in names]
    zimp = [evals[t]["stats"].get("mean_z_improvement_per_regrasp") for t in names]
    zimp_mm = [None if v is None else v * 1000.0 for v in zimp]

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(11, 4.4))
    x = np.arange(len(names))

    def bars(ax, vals, ylabel, title, fmt):
        vv = [0.0 if v is None else v for v in vals]
        ax.bar(x, vv, color=[COLORS[t] for t in names])
        for xi, v in zip(x, vals):
            ax.text(xi, 0 if v is None else v,
                    "n/a" if v is None else fmt.format(v),
                    ha="center", va="bottom", fontsize=10)
        ax.set_xticks(x, names)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.3)

    bars(ax0, reg, "regrasps", "Regrasps per successful episode", "{:.2f}")
    bars(ax1, zimp_mm, "mm", "Mean z_improvement per regrasp", "{:.2f}")

    fig.tight_layout()
    out = os.path.join(args.out_dir, f"regrasp_metrics_{args.eval_suffix}.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"Wrote {out}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Plot training/eval comparisons for test1/test2/test3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--tests", nargs="+", default=["test1", "test2", "test3"])
    p.add_argument("--curves", action="store_true", help="pairwise learning curves")
    p.add_argument("--retention", action="store_true", help="retention across the switch")
    p.add_argument("--success", action="store_true", help="success-rate bars")
    p.add_argument("--failures", action="store_true",
                   help="outcome mix (regrasp-limit counted as timeout)")
    p.add_argument("--regrasp", action="store_true",
                   help="regrasps per success + z_improvement per regrasp")
    p.add_argument("--tag", type=str, default="Train/mean_reward",
                   help="TensorBoard scalar for the curve plots")
    p.add_argument("--smooth", type=int, default=11, help="moving-average window (iterations)")
    p.add_argument("--switch", type=int, default=450, help="--randomize-at iteration")
    p.add_argument("--eval-suffix", choices=("rand", "clean"), default="rand",
                   help="which eval JSONs to use")
    p.add_argument("--seed", type=int, default=1, help="eval seed in the JSON filename")
    p.add_argument("--out-dir", type=str, default="logs/plots")
    args = p.parse_args()

    # No selection flags -> everything
    selected = [args.curves, args.retention, args.success, args.failures, args.regrasp]
    if not any(selected):
        args.curves = args.retention = args.success = args.failures = args.regrasp = True

    os.makedirs(args.out_dir, exist_ok=True)

    if args.curves or args.retention:
        curves = load_curves(args.tests, args.tag)
        if args.curves:
            fig_curves(curves, args)
        if args.retention:
            fig_retention(curves, args)

    if args.success or args.failures or args.regrasp:
        evals = load_eval(args.tests, args.eval_suffix, args.seed)
        if args.success:
            fig_success(evals, args)
        if args.failures:
            fig_failures(evals, args)
        if args.regrasp:
            fig_regrasp(evals, args)


if __name__ == "__main__":
    main()
