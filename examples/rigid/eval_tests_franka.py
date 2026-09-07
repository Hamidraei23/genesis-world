"""
Batch evaluation for the test1 / test2 / test3 training runs.

Each test trained in its own environment with its own observation layout, so a
policy can only ever be evaluated in the env it was trained in. This script
resolves that env automatically, picks the best post-randomization checkpoint by
reading the run's own TensorBoard curve, rolls out N episodes, and reports the
terminal-outcome breakdown instead of just mean reward.

Checkpoint selection ("look at the training plot"):
    Among model_<iter>.pt with iter >= --after (default 450, the --randomize-at
    boundary), score each by the trailing mean of Train/mean_reward over
    --window iterations and take the argmax. Trailing, so no future information
    leaks into the choice.

Usage:
    # One test
    python3 examples/rigid/eval_tests_franka.py -e test2 --episodes 200

    # All three sequentially (one subprocess each), then a comparison table
    python3 examples/rigid/eval_tests_franka.py --all --episodes 200

    # Clean (non-randomized) evaluation of the same weights
    python3 examples/rigid/eval_tests_franka.py --all --no-randomize

Outputs a JSON per test in logs/<test>/eval/ plus, in --all mode, a comparison
table and logs/eval_comparison.png.
"""

import argparse
import glob
import importlib
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


# Fallback when a run directory has no env_cfg.pkl (runs from before it existed)
TEST_ENV_FALLBACK = {
    "test1": "env_franka_parallel_original",
    "test2": "env_franka_parallel_backup_2h",
    "test3": "env_franka_parallel",
}

ALL_TESTS = ("test1", "test2", "test3")


# ---------------------------------------------------------------------------
# Env / checkpoint resolution
# ---------------------------------------------------------------------------

def load_env_class(spec: str):
    """Import an env module by name (or path) and return its env class."""
    module_spec, _, class_name = spec.partition(":")
    if module_spec.endswith(".py") or os.sep in module_spec:
        path = Path(module_spec).expanduser().resolve()
        sys.path.insert(0, str(path.parent))
        module_name = path.stem
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        module_name = module_spec

    module = importlib.import_module(module_name)
    if class_name:
        return getattr(module, class_name)
    if hasattr(module, "FrankaEnvParallel"):
        return module.FrankaEnvParallel
    candidates = [
        obj for _, obj in vars(module).items()
        if isinstance(obj, type) and obj.__module__ == module.__name__
        and hasattr(obj, "step") and hasattr(obj, "reset")
    ]
    if len(candidates) != 1:
        raise AttributeError(f"Cannot determine env class in {module_name}")
    return candidates[0]


def resolve_env_spec(log_dir: str, exp_name: str) -> str:
    """Which env module did this run train in?"""
    cfg_path = os.path.join(log_dir, "env_cfg.pkl")
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            cfg = pickle.load(f)
        if cfg.get("env"):
            return cfg["env"]
    key = exp_name.split("/")[0]
    if key in TEST_ENV_FALLBACK:
        print(f"[note] {log_dir}/env_cfg.pkl missing; falling back to {TEST_ENV_FALLBACK[key]}")
        return TEST_ENV_FALLBACK[key]
    raise FileNotFoundError(
        f"Cannot determine the env for {log_dir}: no env_cfg.pkl and '{key}' is not a known test name. "
        f"Pass --env explicitly."
    )


def read_scalar_series(log_dir: str, tag: str) -> dict[int, float]:
    """Merge a scalar tag across every event file in a run directory."""
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

    series: dict[int, float] = {}
    files = sorted(glob.glob(os.path.join(log_dir, "events.out.tfevents.*")))
    for path in files:
        acc = EventAccumulator(path, size_guidance={"scalars": 0})
        acc.Reload()
        if tag not in acc.Tags().get("scalars", []):
            continue
        for ev in acc.Scalars(tag):
            series[int(ev.step)] = float(ev.value)  # later files win on overlap
    return series


def list_checkpoints(log_dir: str) -> list[tuple[int, str]]:
    out = []
    for path in glob.glob(os.path.join(log_dir, "model_*.pt")):
        stem = os.path.basename(path)[len("model_"):-len(".pt")]
        if stem.isdigit():
            out.append((int(stem), path))
    return sorted(out)


def select_checkpoint(log_dir: str, after: int, window: int, tag: str):
    """Best checkpoint at iteration >= after, scored by trailing mean of `tag`."""
    ckpts = list_checkpoints(log_dir)
    if not ckpts:
        raise FileNotFoundError(f"No model_*.pt in {log_dir}")

    # Skip the first `window` iterations after the switch: that stretch is the
    # post-randomization transient, and a trailing mean there would still be
    # dominated by pre-switch (non-randomized) reward.
    eligible = [(it, p) for it, p in ckpts if it >= after + window]
    if not eligible:
        eligible = [(it, p) for it, p in ckpts if it >= after]
        if eligible:
            print(f"[note] no checkpoint at iter >= {after + window}; "
                  f"scoring the transient region instead")
    if not eligible:
        raise FileNotFoundError(
            f"No checkpoint at iteration >= {after} in {log_dir} "
            f"(latest is {ckpts[-1][0]}). Training may not have passed the switch yet."
        )

    series = read_scalar_series(log_dir, tag)
    if not series:
        it, path = eligible[-1]
        print(f"[note] no '{tag}' in the event files; falling back to the latest checkpoint {it}")
        return it, path, float("nan"), series

    steps = np.array(sorted(series))
    values = np.array([series[s] for s in steps])

    scored = []
    for it, path in eligible:
        lo = max(it - window, after - 1)        # never average across the switch
        mask = (steps > lo) & (steps <= it)
        if not mask.any():                      # no logged points in the window
            mask = (steps > after - 1) & (steps <= it)
            if not mask.any():
                continue
        scored.append((float(values[mask].mean()), it, path))

    if not scored:
        it, path = eligible[-1]
        return it, path, float("nan"), series

    score, it, path = max(scored, key=lambda t: t[0])
    print(f"Checkpoint selection over {len(scored)} candidates at iter >= {after}:")
    for s, i, _ in sorted(scored, key=lambda t: -t[0])[:5]:
        marker = "  <-- selected" if i == it else ""
        print(f"    iter {i:5d}   trailing-{window} mean {tag} = {s:9.1f}{marker}")
    return it, path, score, series


# ---------------------------------------------------------------------------
# Rollout
# ---------------------------------------------------------------------------

def evaluate(env, policy, episodes: int, max_steps: int, device) -> dict:
    """Roll out until `episodes` episodes have terminated; classify each outcome.

    Outcomes are derived without touching the envs: `base_reward` from
    last_reward_terms is the terminal component alone (positive only on success,
    -250 on fail/timeout), and regrasp events are accumulated here so they
    survive the env's internal post-done reset.
    """
    n = env.num_envs
    term_limit = getattr(env, "REGRASP_TERMINATION_COUNT", None)

    ep_len = torch.zeros(n, dtype=torch.long, device=device)
    ep_ret = torch.zeros(n, device=device)
    ep_regrasps = torch.zeros(n, device=device)
    ep_zimp = torch.zeros(n, device=device)

    rec = {k: [] for k in ("outcome", "length", "ret", "regrasps", "zimp")}

    obs_td = env.reset()
    steps = 0
    while len(rec["outcome"]) < episodes and steps < max_steps:
        with torch.no_grad():
            actions = policy(obs_td)
        obs_td, rew, done, extras = env.step(actions)
        steps += 1

        terms = env.last_reward_terms          # captured before the internal reset
        regrasp_event = terms["regrasp_event"].to(device)
        ep_regrasps += regrasp_event
        ep_zimp += terms["z_improvement"].to(device) * regrasp_event
        ep_ret += rew.to(device)
        ep_len += 1

        done = done.to(device).bool()
        if not done.any():
            continue

        base = terms["base_reward"].to(device)
        timeout = extras.get("time_outs")
        timeout = (torch.zeros_like(done) if timeout is None
                   else timeout.to(device).bool())

        success = done & (base > 0.0)
        # A 7th regrasp is recorded as a fail; separate it from geometric fails
        limit_fail = done & ~success & (
            (ep_regrasps >= term_limit) if term_limit else torch.zeros_like(done)
        )
        timeout_only = done & ~success & ~limit_fail & timeout
        geom_fail = done & ~success & ~limit_fail & ~timeout_only

        for idx in done.nonzero(as_tuple=False).squeeze(-1).tolist():
            if len(rec["outcome"]) >= episodes:
                break
            if success[idx]:
                label = "success"
            elif limit_fail[idx]:
                label = "regrasp_limit_fail"
            elif timeout_only[idx]:
                label = "timeout"
            else:
                label = "geometric_fail"
            rec["outcome"].append(label)
            rec["length"].append(int(ep_len[idx]))
            rec["ret"].append(float(ep_ret[idx]))
            rec["regrasps"].append(float(ep_regrasps[idx]))
            rec["zimp"].append(float(ep_zimp[idx]))

        keep = ~done
        ep_len *= keep
        ep_ret *= keep
        ep_regrasps *= keep
        ep_zimp *= keep

    return summarize(rec, steps)


def summarize(rec: dict, steps: int) -> dict:
    outcomes = rec["outcome"]
    m = len(outcomes)
    if m == 0:
        return {"episodes": 0, "note": "no episode terminated", "policy_steps": steps}

    arr = {k: np.asarray(rec[k], dtype=float) for k in ("length", "ret", "regrasps", "zimp")}
    labels = np.asarray(outcomes)
    is_succ = labels == "success"

    def frac(name):
        return float((labels == name).mean())

    total_regrasps = arr["regrasps"].sum()
    return {
        "episodes": m,
        "policy_steps": steps,
        "success_rate": float(is_succ.mean()),
        "geometric_fail_rate": frac("geometric_fail"),
        "regrasp_limit_fail_rate": frac("regrasp_limit_fail"),
        "timeout_rate": frac("timeout"),
        "mean_return": float(arr["ret"].mean()),
        "mean_length": float(arr["length"].mean()),
        "mean_length_on_success": float(arr["length"][is_succ].mean()) if is_succ.any() else None,
        "mean_regrasps": float(arr["regrasps"].mean()),
        "mean_regrasps_on_success": float(arr["regrasps"][is_succ].mean()) if is_succ.any() else None,
        "mean_z_improvement_per_regrasp": float(arr["zimp"].sum() / total_regrasps) if total_regrasps else None,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(name: str, meta: dict, stats: dict) -> None:
    print()
    print("=" * 72)
    print(f"  {name}   env={meta['env']}   checkpoint iter {meta['ckpt_iter']}")
    print(f"  obs_dim={meta['obs_dim']}  randomize={meta['randomize']}  "
          f"mix={meta['mix']}  normalize={meta['normalize']}  seed={meta['seed']}")
    print("=" * 72)
    if not stats.get("episodes"):
        print("  no episodes terminated -- raise --max-steps")
        return
    pct = lambda v: f"{v:6.1%}"
    print(f"  episodes                       : {stats['episodes']}")
    print(f"  success rate                   : {pct(stats['success_rate'])}")
    print(f"  geometric fail                 : {pct(stats['geometric_fail_rate'])}")
    print(f"  regrasp-limit fail             : {pct(stats['regrasp_limit_fail_rate'])}")
    print(f"  timeout                        : {pct(stats['timeout_rate'])}")
    print(f"  mean return                    : {stats['mean_return']:9.1f}")
    print(f"  mean episode length            : {stats['mean_length']:9.1f}")
    if stats["mean_length_on_success"] is not None:
        print(f"  mean length | success          : {stats['mean_length_on_success']:9.1f}")
        print(f"  mean regrasps | success        : {stats['mean_regrasps_on_success']:9.2f}")
    print(f"  mean regrasps                  : {stats['mean_regrasps']:9.2f}")
    if stats["mean_z_improvement_per_regrasp"] is not None:
        print(f"  mean z_improvement per regrasp : {stats['mean_z_improvement_per_regrasp']*1000:9.2f} mm")


def print_comparison(results: dict) -> None:
    names = list(results)
    print()
    print("=" * 96)
    print("  COMPARISON")
    print("=" * 96)
    hdr = f"  {'metric':32s}" + "".join(f"{n:>18s}" for n in names)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    rows = [
        ("observation dim", "obs_dim", "{:.0f}", False),
        ("checkpoint iter", "ckpt_iter", "{:.0f}", False),
        ("success rate", "success_rate", "{:.1%}", True),
        ("geometric fail", "geometric_fail_rate", "{:.1%}", True),
        ("regrasp-limit fail", "regrasp_limit_fail_rate", "{:.1%}", True),
        ("timeout", "timeout_rate", "{:.1%}", True),
        ("mean return", "mean_return", "{:.1f}", True),
        ("mean length | success", "mean_length_on_success", "{:.1f}", True),
        ("mean regrasps | success", "mean_regrasps_on_success", "{:.2f}", True),
        ("z_improvement / regrasp (mm)", "_zimp_mm", "{:.2f}", True),
    ]
    for label, key, fmt, from_stats in rows:
        cells = ""
        for n in names:
            src = results[n]["stats"] if from_stats else results[n]["meta"]
            if key == "_zimp_mm":
                v = src.get("mean_z_improvement_per_regrasp")
                v = None if v is None else v * 1000.0
            else:
                v = src.get(key)
            cells += f"{'--':>18s}" if v is None else f"{fmt.format(v):>18s}"
        print(f"  {label:32s}{cells}")
    print()


def plot_comparison(results: dict, out_path: str) -> None:
    names = list(results)
    labels = ["success", "geometric_fail", "regrasp_limit_fail", "timeout"]
    keys = ["success_rate", "geometric_fail_rate", "regrasp_limit_fail_rate", "timeout_rate"]

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(13, 4.5))

    x = np.arange(len(names))
    bottom = np.zeros(len(names))
    for label, key in zip(labels, keys):
        vals = np.array([results[n]["stats"].get(key, 0.0) or 0.0 for n in names])
        ax0.bar(x, vals, bottom=bottom, label=label)
        bottom += vals
    ax0.set_xticks(x, names)
    ax0.set_ylabel("fraction of episodes")
    ax0.set_title("Terminal outcome mix")
    ax0.legend(fontsize=8)
    ax0.grid(axis="y", alpha=0.3)

    succ = np.array([results[n]["stats"].get("success_rate", 0.0) or 0.0 for n in names])
    ax1.bar(x, succ, color="tab:green")
    for xi, v in zip(x, succ):
        ax1.text(xi, v, f"{v:.1%}", ha="center", va="bottom", fontsize=9)
    ax1.set_xticks(x, names)
    ax1.set_ylim(0, max(1.0, succ.max() * 1.2))
    ax1.set_ylabel("success rate")
    ax1.set_title("Success rate")
    ax1.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print(f"Comparison plot: {out_path}")


# ---------------------------------------------------------------------------
# Single-test driver
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

    kwargs = dict(
        num_envs=args.num_envs,
        vis=False,
        dt=args.dt,
        target_dt=args.target_dt,
        mix=args.mix,
        normalize=args.normalization,
        randomize=args.randomize,
    )
    import inspect
    accepted = inspect.signature(env_cls.__init__).parameters
    env = env_cls(**{k: v for k, v in kwargs.items() if k in accepted})

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=gs.device)

    stats = evaluate(env, policy, args.episodes, args.max_steps, gs.device)
    meta = {
        "exp_name": args.exp_name,
        "env": env_spec,
        "obs_dim": int(getattr(env_cls, "OBS_DIM", 0)),
        "ckpt_iter": ckpt_iter,
        "ckpt_path": ckpt_path,
        "ckpt_train_score": ckpt_score,
        "randomize": args.randomize,
        "mix": args.mix,
        "normalize": args.normalization,
        "seed": args.seed,
        "num_envs": args.num_envs,
    }
    print_report(args.exp_name, meta, stats)

    out_dir = os.path.join(log_dir, "eval")
    os.makedirs(out_dir, exist_ok=True)
    suffix = "rand" if args.randomize else "clean"
    out_path = os.path.join(out_dir, f"eval_{suffix}_seed{args.seed}.json")
    with open(out_path, "w") as f:
        json.dump({"meta": meta, "stats": stats}, f, indent=2)
    print(f"Wrote {out_path}")
    return {"meta": meta, "stats": stats}


def run_all(args) -> None:
    """One subprocess per test: genesis only tolerates a single gs.init per process."""
    results = {}
    for name in args.tests:
        cmd = [sys.executable, os.path.abspath(__file__), "-e", name,
               "--episodes", str(args.episodes), "--num-envs", str(args.num_envs),
               "--after", str(args.after), "--window", str(args.window),
               "--max-steps", str(args.max_steps), "--seed", str(args.seed),
               "--dt", str(args.dt), "--target_dt", str(args.target_dt)]
        if not args.mix:
            cmd.append("--no-mix")
        if not args.normalization:
            cmd.append("--no-normalization")
        if not args.randomize:
            cmd.append("--no-randomize")

        print(f"\n>>> {' '.join(cmd)}")
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(f"[WARNING] {name} exited with code {proc.returncode}; skipping it")
            continue

        suffix = "rand" if args.randomize else "clean"
        path = f"logs/{name}/eval/eval_{suffix}_seed{args.seed}.json"
        if os.path.exists(path):
            with open(path) as f:
                results[name] = json.load(f)

    if len(results) >= 2:
        print_comparison(results)
        plot_comparison(results, "logs/eval_comparison.png")
        with open("logs/eval_comparison.json", "w") as f:
            json.dump(results, f, indent=2)
        print("Wrote logs/eval_comparison.json")
    elif results:
        print("\nOnly one test produced results; nothing to compare.")
    else:
        print("\nNo results produced.")


def main():
    p = argparse.ArgumentParser(
        description="Evaluate the test1/test2/test3 policies in their own envs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-e", "--exp_name", type=str, default=None,
                   help="Run/log directory name under logs/ (e.g. test2)")
    p.add_argument("--all", action="store_true",
                   help=f"Evaluate {', '.join(ALL_TESTS)} sequentially, then compare")
    p.add_argument("--tests", nargs="+", default=list(ALL_TESTS),
                   help="Run names used by --all")
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
    p.add_argument("--episodes", type=int, default=200,
                   help="Episodes to collect per test")
    p.add_argument("-B", "--num-envs", type=int, default=64,
                   help="Parallel envs during evaluation")
    p.add_argument("--max-steps", type=int, default=20000,
                   help="Safety cap on policy steps per test")
    p.add_argument("--seed", type=int, default=1,
                   help="Shared seed so all tests face the same episode draws")
    p.add_argument("--dt", type=float, default=0.001)
    p.add_argument("--target_dt", type=float, default=0.02)
    p.add_argument("--no-mix", dest="mix", action="store_false", default=True,
                   help="Disable --mix (training used it, so keep it on)")
    p.add_argument("--no-normalization", dest="normalization", action="store_false", default=True,
                   help="Disable obs normalization (training used it, so keep it on)")
    p.add_argument("--no-randomize", dest="randomize", action="store_false", default=True,
                   help="Evaluate without domain randomization")
    args = p.parse_args()

    if args.all or args.exp_name is None:
        if args.exp_name is not None:
            args.tests = [args.exp_name]
        run_all(args)
    else:
        run_one(args)


if __name__ == "__main__":
    main()
