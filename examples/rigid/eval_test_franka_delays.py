"""
Gripper-pulse timing sweep for the test1 / test2 / test3 policies.

Everything stays randomized exactly as in training (desired_z, friction, finger
gains, control error, zero-hold, ...). Exactly ONE pulse-timing parameter is
pinned per sweep point -- the one being swept. The other keeps its native
per-episode randomization, so each point measures the swept parameter against a
realistic spread of the other rather than against one arbitrary value.

Pinning collapses that parameter's randomization range onto the value:

    PULSE_DELAY_RANDOM_MIN = PULSE_DELAY_RANDOM_MAX = d      (delay sweep)
    PULSE_LENGTH_RANDOM_MIN = PULSE_LENGTH_RANDOM_MAX = l    (length sweep)

while the other parameter's MIN/MAX are restored to the env class's own values,
captured before anything is touched. _sample_gripper_pulse_delays /
_sample_gripper_pulse_lengths run on every reset, including the mid-rollout
per-env resets, so both the pin and the randomization hold for every episode.
Both parameters are in target-period steps (20 ms each at target_dt=0.02).

Two sweeps per test:
    delay sweep    pulse delay 1..3 pinned, pulse length randomized (3..6)
    length sweep   pulse length 3..6 pinned, pulse delay randomized (1..3)

Before each point the RNG is reseeded to --seed, so every point faces the same
episode draws -- including the same draws of the un-pinned pulse parameter --
and the only thing that differs is the swept value. Pass --no-reseed for
independent draws instead.

Usage:
    # all three tests, both sweeps, 60 episodes per point, then plots
    python3 examples/rigid/eval_test_franka_delays.py --all --episodes 60

    # one test only
    python3 examples/rigid/eval_test_franka_delays.py -e test2 --episodes 60

    # re-plot from JSON without rerunning the simulation
    python3 examples/rigid/eval_test_franka_delays.py --plot-only

Outputs:
    logs/<test>/eval/pulse_sweep_seed<seed>.json   per-test raw results
    logs/pulse_sweep_comparison.json               all tests merged
    logs/plots/pulse_delay_sweep.png               success rate vs pulse delay
    logs/plots/pulse_length_sweep.png              success rate vs pulse length
"""

import argparse
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless – required inside Docker (no $DISPLAY)
import matplotlib.pyplot as plt
import numpy as np
import torch

from rsl_rl.runners import OnPolicyRunner

import genesis as gs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_tests_franka import (  # noqa: E402  (path must be set first)
    ALL_TESTS,
    evaluate,
    load_env_class,
    resolve_env_spec,
    select_checkpoint,
)

DEFAULT_DELAYS = (1, 2, 3)
DEFAULT_LENGTHS = (3, 4, 5, 6)

COLORS = {"test1": "tab:green", "test2": "tab:orange", "test3": "tab:purple"}
LABELS = {
    "test1": "test1 (10 obs: force, no history)",
    "test2": "test2 (16 obs: force + history)",
    "test3": "test3 (14 obs: history, no force)",
}


# ---------------------------------------------------------------------------
# Pulse pinning
# ---------------------------------------------------------------------------

def capture_ranges(env_cls) -> dict:
    """The env's own pulse randomization ranges, read before anything is pinned."""
    return {
        "delay": (int(env_cls.PULSE_DELAY_RANDOM_MIN), int(env_cls.PULSE_DELAY_RANDOM_MAX)),
        "length": (int(env_cls.PULSE_LENGTH_RANDOM_MIN), int(env_cls.PULSE_LENGTH_RANDOM_MAX)),
    }


def set_pulse(target, ranges: dict, delay=None, length=None) -> None:
    """Pin the given parameter(s); leave the rest on their native random range.

    `None` means "randomize as the env normally would". `target` is the env
    class (before construction) or the instance (after); the samplers read
    these off `self`, so either works. This script always runs randomize=True,
    so only the RANDOM_MIN/MAX pair drives the samplers -- PULSE_DELAY_STEPS /
    PULSE_LENGTH are set alongside it purely to keep the two consistent.
    """
    d_lo, d_hi = (int(delay), int(delay)) if delay is not None else ranges["delay"]
    l_lo, l_hi = (int(length), int(length)) if length is not None else ranges["length"]
    target.PULSE_DELAY_RANDOM_MIN, target.PULSE_DELAY_RANDOM_MAX = d_lo, d_hi
    target.PULSE_LENGTH_RANDOM_MIN, target.PULSE_LENGTH_RANDOM_MAX = l_lo, l_hi
    if delay is not None:
        target.PULSE_DELAY_STEPS = int(delay)
    if length is not None:
        target.PULSE_LENGTH = int(length)


def observed_pulse(env) -> dict:
    """What the env actually used, read back from its per-env state tensors.

    Called after a rollout, so these reflect the most recent resets. A pinned
    parameter must show exactly one value here; a randomized one should show a
    spread inside its range. Either failing is how a broken pin surfaces.
    """
    return {
        "delays": sorted(int(v) for v in env._gripper_pulse_delays.unique().tolist()),
        "lengths": sorted(int(v) for v in env._gripper_pulse_lengths.unique().tolist()),
    }


def check_pulse(used: dict, ranges: dict, delay=None, length=None) -> list:
    """Complaints about a sweep point whose pulse state is not what was asked for."""
    problems = []
    for key, pinned, rng in (("delays", delay, ranges["delay"]),
                             ("lengths", length, ranges["length"])):
        seen = used[key]
        if pinned is not None:
            if seen != [int(pinned)]:
                problems.append(f"{key} pinned to {pinned} but env used {seen}")
        else:
            lo, hi = rng
            if not seen or min(seen) < lo or max(seen) > hi:
                problems.append(f"{key} should be randomized in [{lo}, {hi}] but env used {seen}")
            elif len(seen) == 1 and hi > lo:
                # every env drawing the same value out of hi-lo+1 is ~impossible
                problems.append(f"{key} should be randomized in [{lo}, {hi}] but every "
                                f"env used {seen[0]}; randomization looks disabled")
    return problems


def reseed(seed: int) -> None:
    """Same episode draws at every sweep point, so only the pulse timing moves."""
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)


# ---------------------------------------------------------------------------
# Single-test driver (one process, one env, many sweep points)
# ---------------------------------------------------------------------------

def run_one(args) -> dict:
    log_dir = f"logs/{args.exp_name}"
    if not os.path.isdir(log_dir):
        raise FileNotFoundError(f"No such run directory: {log_dir}")

    env_spec = args.env or resolve_env_spec(log_dir, args.exp_name)
    env_cls = load_env_class(env_spec)
    print(f"\n### {args.exp_name}: env={env_spec} -> {env_cls.__name__}")

    if args.ckpt is not None:
        ckpt_iter, ckpt_path = args.ckpt, os.path.join(log_dir, f"model_{args.ckpt}.pt")
        ckpt_score = float("nan")
        if not os.path.exists(ckpt_path):
            raise FileNotFoundError(ckpt_path)
    else:
        ckpt_iter, ckpt_path, ckpt_score, _ = select_checkpoint(
            log_dir, args.after, args.window, args.tag
        )
    print(f"Checkpoint: {ckpt_path}")

    with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
        train_cfg = pickle.load(f)

    gs.init(backend=gs.gpu, precision="32", logging_level="warning",
            seed=args.seed, performance_mode=True)

    # Read the env's native randomization ranges before touching anything;
    # whichever parameter is not being swept gets restored to these.
    ranges = capture_ranges(env_cls)
    print(f"Native pulse ranges: delay {ranges['delay']}  length {ranges['length']}")

    kwargs = dict(
        num_envs=args.num_envs,
        vis=False,
        dt=args.dt,
        target_dt=args.target_dt,
        mix=args.mix,
        normalize=args.normalization,
        randomize=True,          # the point of this sweep: everything else random
    )
    import inspect
    accepted = inspect.signature(env_cls.__init__).parameters
    env = env_cls(**{k: v for k, v in kwargs.items() if k in accepted})

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=gs.device)

    def point(delay=None, length=None) -> dict:
        """One sweep point: `delay`/`length` pinned if given, randomized if None."""
        set_pulse(env, ranges, delay=delay, length=length)
        if args.reseed:
            reseed(args.seed)

        ms = args.target_dt * 1000.0
        d_txt = (f"{delay} ({delay * ms:.0f} ms)" if delay is not None
                 else f"random {ranges['delay'][0]}..{ranges['delay'][1]}")
        l_txt = (f"{length} ({(length - 1) * ms:.0f} ms open + {ms:.0f} ms close)"
                 if length is not None
                 else f"random {ranges['length'][0]}..{ranges['length'][1]}")
        print(f"\n--- {args.exp_name}: pulse delay={d_txt}  length={l_txt}")

        stats = evaluate(env, policy, args.episodes, args.max_steps, gs.device)
        used = observed_pulse(env)
        for problem in check_pulse(used, ranges, delay=delay, length=length):
            print(f"  [WARNING] {problem}")
        if stats.get("episodes"):
            print(f"  episodes={stats['episodes']}  success={stats['success_rate']:.1%}")
        else:
            print("  no episodes terminated -- raise --max-steps")

        return {"pulse_delay": delay, "pulse_length": length,
                "observed": used, "stats": stats}

    delay_sweep = [point(delay=d) for d in args.delays]
    length_sweep = [point(length=n) for n in args.lengths]

    meta = {
        "exp_name": args.exp_name,
        "env": env_spec,
        "obs_dim": int(getattr(env_cls, "OBS_DIM", 0)),
        "ckpt_iter": ckpt_iter,
        "ckpt_path": ckpt_path,
        "ckpt_train_score": ckpt_score,
        "randomize": True,
        "mix": args.mix,
        "normalize": args.normalization,
        "seed": args.seed,
        "reseed_per_point": args.reseed,
        "num_envs": args.num_envs,
        "episodes_per_point": args.episodes,
        "target_dt": args.target_dt,
        "delay_random_range": list(ranges["delay"]),
        "length_random_range": list(ranges["length"]),
    }
    result = {"meta": meta, "delay_sweep": delay_sweep, "length_sweep": length_sweep}

    print_report(args.exp_name, result)

    out_dir = os.path.join(log_dir, "eval")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"pulse_sweep_seed{args.seed}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {out_path}")
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(name: str, result: dict) -> None:
    meta = result["meta"]
    print()
    print("=" * 78)
    print(f"  {name}   env={meta['env']}   checkpoint iter {meta['ckpt_iter']}")
    print(f"  obs_dim={meta['obs_dim']}  randomize=True  mix={meta['mix']}  "
          f"normalize={meta['normalize']}  seed={meta['seed']}")
    print("=" * 78)

    d_lo, d_hi = meta.get("delay_random_range", ("?", "?"))
    l_lo, l_hi = meta.get("length_random_range", ("?", "?"))
    for title, key, xlabel in (
        (f"pulse DELAY sweep (length randomized {l_lo}..{l_hi})",
         "delay_sweep", "delay"),
        (f"pulse LENGTH sweep (delay randomized {d_lo}..{d_hi})",
         "length_sweep", "length"),
    ):
        print(f"\n  {title}")
        print(f"    {'steps':>6s} {'ms':>6s} {'episodes':>9s} {'success':>9s} "
              f"{'geom fail':>10s} {'regrasp lim':>12s} {'timeout':>9s} {'regrasps':>9s}")
        for pt in result[key]:
            s = pt["stats"]
            steps = pt["pulse_delay"] if xlabel == "delay" else pt["pulse_length"]
            ms = steps * meta["target_dt"] * 1000.0
            if not s.get("episodes"):
                print(f"    {steps:6d} {ms:6.0f} {'--':>9s}   (no episodes terminated)")
                continue
            mr = s.get("mean_regrasps")
            print(f"    {steps:6d} {ms:6.0f} {s['episodes']:9d} "
                  f"{s['success_rate']:8.1%} {s['geometric_fail_rate']:9.1%} "
                  f"{s['regrasp_limit_fail_rate']:11.1%} {s['timeout_rate']:8.1%} "
                  f"{mr:9.2f}")


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _series(result: dict, sweep_key: str, x_key: str):
    """(x, success_rate, binomial stderr, n) arrays for one test's sweep."""
    xs, ys, es, ns = [], [], [], []
    for pt in result[sweep_key]:
        s = pt["stats"]
        n = s.get("episodes", 0)
        if not n:
            continue
        p = s["success_rate"]
        xs.append(pt[x_key])
        ys.append(p)
        es.append((p * (1.0 - p) / n) ** 0.5)   # binomial standard error
        ns.append(n)
    order = np.argsort(xs)
    return (np.asarray(xs)[order], np.asarray(ys)[order],
            np.asarray(es)[order], np.asarray(ns)[order])


def plot_sweep(results: dict, sweep_key: str, x_key: str, xlabel: str,
               title: str, out_path: str, target_dt: float) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 5.0))

    any_data = False
    for name, result in results.items():
        x, y, e, n = _series(result, sweep_key, x_key)
        if not len(x):
            continue
        any_data = True
        ax.errorbar(x, y, yerr=e, marker="o", capsize=4, linewidth=2,
                    markersize=7, color=COLORS.get(name), label=LABELS.get(name, name))
        for xi, yi in zip(x, y):
            ax.annotate(f"{yi:.0%}", (xi, yi), textcoords="offset points",
                        xytext=(0, 9), ha="center", fontsize=8,
                        color=COLORS.get(name))

    if not any_data:
        plt.close(fig)
        print(f"[skip] {out_path}: no terminated episodes in any test")
        return

    all_x = sorted({int(v) for r in results.values() for v in _series(r, sweep_key, x_key)[0]})
    ax.set_xticks(all_x)
    ax.set_xticklabels([f"{v}\n({v * target_dt * 1000:.0f} ms)" for v in all_x])
    ax.set_xlabel(xlabel)
    ax.set_ylabel("success rate")
    ax.set_ylim(0.0, 1.0)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="best")

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f"Wrote {out_path}")


def make_plots(results: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    any_meta = next(iter(results.values()))["meta"]
    target_dt = any_meta.get("target_dt", 0.02)
    episodes = any_meta.get("episodes_per_point", "?")

    d_lo, d_hi = any_meta.get("delay_random_range", ("?", "?"))
    l_lo, l_hi = any_meta.get("length_random_range", ("?", "?"))

    plot_sweep(
        results, "delay_sweep", "pulse_delay", "PULSE_DELAY_STEPS",
        f"Success rate vs pulse delay\n"
        f"pulse length randomized {l_lo}..{l_hi}, all other randomization on\n"
        f"{episodes} episodes/point, bars = binomial SE",
        os.path.join(out_dir, "pulse_delay_sweep.png"), target_dt,
    )
    plot_sweep(
        results, "length_sweep", "pulse_length", "PULSE_LENGTH",
        f"Success rate vs pulse length\n"
        f"pulse delay randomized {d_lo}..{d_hi}, all other randomization on\n"
        f"{episodes} episodes/point, bars = binomial SE",
        os.path.join(out_dir, "pulse_length_sweep.png"), target_dt,
    )


# ---------------------------------------------------------------------------
# Multi-test driver
# ---------------------------------------------------------------------------

def collect(tests, seed: int) -> dict:
    """Load whatever per-test sweep JSONs exist on disk."""
    results = {}
    for name in tests:
        path = f"logs/{name}/eval/pulse_sweep_seed{seed}.json"
        if os.path.exists(path):
            with open(path) as f:
                results[name] = json.load(f)
        else:
            print(f"[note] missing {path}")
    return results


def run_all(args) -> None:
    """One subprocess per test: genesis only tolerates a single gs.init per process."""
    for name in args.tests:
        cmd = [sys.executable, os.path.abspath(__file__), "-e", name,
               "--episodes", str(args.episodes), "--num-envs", str(args.num_envs),
               "--after", str(args.after), "--window", str(args.window),
               "--max-steps", str(args.max_steps), "--seed", str(args.seed),
               "--dt", str(args.dt), "--target_dt", str(args.target_dt),
               "--delays", *[str(v) for v in args.delays],
               "--lengths", *[str(v) for v in args.lengths]]
        if not args.mix:
            cmd.append("--no-mix")
        if not args.normalization:
            cmd.append("--no-normalization")
        if not args.reseed:
            cmd.append("--no-reseed")

        print(f"\n>>> {' '.join(cmd)}")
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(f"[WARNING] {name} exited with code {proc.returncode}; skipping it")

    results = collect(args.tests, args.seed)
    if not results:
        print("\nNo results produced.")
        return

    with open("logs/pulse_sweep_comparison.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nWrote logs/pulse_sweep_comparison.json")
    make_plots(results, args.out_dir)


def main():
    p = argparse.ArgumentParser(
        description="Pulse delay / pulse length sweep for test1/test2/test3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-e", "--exp_name", type=str, default=None,
                   help="Run/log directory name under logs/ (e.g. test2)")
    p.add_argument("--all", action="store_true",
                   help=f"Sweep {', '.join(ALL_TESTS)} sequentially, then plot")
    p.add_argument("--tests", nargs="+", default=list(ALL_TESTS),
                   help="Run names used by --all")
    p.add_argument("--plot-only", action="store_true",
                   help="Re-plot from existing JSONs without running the sim")
    p.add_argument("--env", type=str, default=None,
                   help="Override the env module (default: read logs/<run>/env_cfg.pkl)")
    p.add_argument("--ckpt", type=int, default=None,
                   help="Force a checkpoint iteration instead of auto-selecting")
    p.add_argument("--after", type=int, default=450,
                   help="Only consider checkpoints at or after this iteration")
    p.add_argument("--window", type=int, default=25,
                   help="Trailing window (iterations) used to score each checkpoint")
    p.add_argument("--tag", type=str, default="Train/mean_reward",
                   help="TensorBoard scalar used for checkpoint selection")
    p.add_argument("--delays", nargs="+", type=int, default=list(DEFAULT_DELAYS),
                   help="PULSE_DELAY_STEPS values to sweep (length stays randomized)")
    p.add_argument("--lengths", nargs="+", type=int, default=list(DEFAULT_LENGTHS),
                   help="PULSE_LENGTH values to sweep (delay stays randomized)")
    p.add_argument("--episodes", type=int, default=60,
                   help="Episodes to collect per sweep point")
    p.add_argument("-B", "--num-envs", type=int, default=64,
                   help="Parallel envs during evaluation")
    p.add_argument("--max-steps", type=int, default=20000,
                   help="Safety cap on policy steps per sweep point")
    p.add_argument("--seed", type=int, default=1,
                   help="Shared seed so all tests face the same episode draws")
    p.add_argument("--no-reseed", dest="reseed", action="store_false", default=True,
                   help="Do not reseed between sweep points (independent draws)")
    p.add_argument("--dt", type=float, default=0.001)
    p.add_argument("--target_dt", type=float, default=0.02)
    p.add_argument("--out-dir", type=str, default="logs/plots",
                   help="Directory for the two PNGs")
    p.add_argument("--no-mix", dest="mix", action="store_false", default=True,
                   help="Disable --mix (training used it, so keep it on)")
    p.add_argument("--no-normalization", dest="normalization", action="store_false", default=True,
                   help="Disable obs normalization (training used it, so keep it on)")
    args = p.parse_args()

    if args.plot_only:
        tests = [args.exp_name] if args.exp_name else args.tests
        results = collect(tests, args.seed)
        if not results:
            print("Nothing to plot.")
            return
        make_plots(results, args.out_dir)
    elif args.all or args.exp_name is None:
        if args.exp_name is not None:
            args.tests = [args.exp_name]
        run_all(args)
    else:
        run_one(args)


if __name__ == "__main__":
    main()
