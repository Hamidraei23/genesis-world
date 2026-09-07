"""
PPO training for FrankaEnvParallel-style environments with rsl_rl v5+.

Usage (from workspace root):
    python examples/rigid/train_franka_ppo.py
    python examples/rigid/train_franka_ppo.py -B 512 --max_iterations 1000
    python examples/rigid/train_franka_ppo.py --resume logs/franka-lift/model_100.pt

Any environment module can be selected with --env (default: env_franka_parallel):
    python examples/rigid/train_franka_ppo.py --env env_franka_parallel_june
    python examples/rigid/train_franka_ppo.py --env env_franka_parallel_bad:FrankaEnvParallepl
    python examples/rigid/train_franka_ppo.py --env /workspace/examples/rigid/env_franka_parallel_history.py

Flags the selected env's __init__ does not accept (e.g. --zero on an older
variant) are dropped with a warning instead of raising TypeError.

Inside docker (genesis container):
    docker exec genesis python /workspace/examples/rigid/train_franka_ppo.py -B 512
"""

import argparse
import importlib
import inspect
import os
import pickle
import shutil
import sys
from importlib import metadata
from pathlib import Path

try:
    if int(metadata.version("rsl-rl-lib").split(".")[0]) < 5:
        raise ImportError
except (metadata.PackageNotFoundError, ImportError) as e:
    raise ImportError("Please install 'rsl-rl-lib>=5.0.0'.") from e

from rsl_rl.runners import OnPolicyRunner

import genesis as gs


DEFAULT_ENV = "env_franka_parallel"


# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

def load_env_class(spec: str):
    """Resolve ``--env`` to an environment class.

    ``spec`` is ``<module or .py path>`` with an optional ``:ClassName`` suffix,
    e.g. ``env_franka_parallel_june``, ``env_franka_parallel_bad:FrankaEnvParallepl``
    or ``examples/rigid/env_franka_parallel_history.py``.
    """
    module_spec, _, class_name = spec.partition(":")

    if module_spec.endswith(".py") or os.sep in module_spec:
        path = Path(module_spec).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"--env file not found: {path}")
        # Import by name from its own directory so its sibling imports keep working
        sys.path.insert(0, str(path.parent))
        module_name = path.stem
    else:
        # Env modules live next to this script; make them importable from anywhere
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        module_name = module_spec

    module = importlib.import_module(module_name)

    if class_name:
        if not hasattr(module, class_name):
            raise AttributeError(f"{module_name} has no class '{class_name}'")
        return getattr(module, class_name)

    # Prefer the canonical name, else pick the single env-like class defined here
    if hasattr(module, "FrankaEnvParallel"):
        return module.FrankaEnvParallel

    candidates = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and obj.__module__ == module.__name__
        and hasattr(obj, "step")
        and hasattr(obj, "reset")
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AttributeError(f"No environment class found in {module_name}")
    names = ", ".join(c.__name__ for c in candidates)
    raise AttributeError(
        f"Multiple environment classes in {module_name} ({names}); "
        f"pick one with --env {module_spec}:ClassName"
    )


def build_env(env_cls, kwargs: dict):
    """Instantiate ``env_cls`` with only the kwargs its __init__ accepts."""
    params = inspect.signature(env_cls.__init__).parameters
    accepts_all = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    if accepts_all:
        return env_cls(**kwargs)

    supported = {k: v for k, v in kwargs.items() if k in params}
    dropped = {k: v for k, v in kwargs.items() if k not in params}
    for key, value in dropped.items():
        # Only complain about options the user actually turned on
        level = "WARNING" if value else "note"
        print(f"[{level}] {env_cls.__name__} does not accept '{key}' (requested {value!r}); ignoring")

    return env_cls(**supported)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_train_cfg(exp_name: str) -> dict:
    """rsl_rl v5+ OnPolicyRunner config dict."""
    return {
        "algorithm": {
            "class_name": "PPO",
            "clip_param": 0.2,
            "desired_kl": 0.01,
            "entropy_coef": 0.005,
            "gamma": 0.99,
            "lam": 0.95,
            "learning_rate": 3e-4,
            "max_grad_norm": 1.0,
            "num_learning_epochs": 5,
            "num_mini_batches": 4,
            "schedule": "adaptive",
            "use_clipped_value_loss": True,
            "value_loss_coef": 1.0,
        },
        "actor": {
            "class_name": "MLPModel",
            "hidden_dims": [256, 128, 64],
            "activation": "elu",
            "distribution_cfg": {
                "class_name": "GaussianDistribution",
                "init_std": 1.0,
                "std_type": "scalar",
            },
        },
        "critic": {
            "class_name": "MLPModel",
            "hidden_dims": [256, 128, 64],
            "activation": "elu",
        },
        # TensorDict key groups fed to actor / critic
        "obs_groups": {
            "actor": ["policy"],
            "critic": ["policy"],
        },
        "num_steps_per_env": 32,   # rollout steps collected per env per iteration
        "save_interval": 20,        # checkpoint every N iterations
        "run_name": exp_name,
        "logger": "tensorboard",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="PPO training for FrankaEnvParallel")
    parser.add_argument("-e", "--exp_name", type=str, default="franka-lift",
                        help="Experiment name; also used as log subdirectory")
    parser.add_argument("--env", type=str, default=DEFAULT_ENV,
                        help="Environment module to train on: a module name next to this script "
                             "(env_franka_parallel_june), a .py path, optionally with ':ClassName'")
    parser.add_argument("-B", "--num_envs", type=int, default=1024,
                        help="Number of parallel environments")
    parser.add_argument("--max_iterations", type=int, default=1000,
                        help="Total PPO update iterations")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--resume", type=str, default=None,
                        help="Path to a checkpoint .pt file to resume training from")
    parser.add_argument("--dt", type=float, default=0.001,
                        help="Physics sim timestep (seconds)")
    parser.add_argument("--target_dt", type=float, default=0.02,
                        help="High-level action period (seconds); must be a multiple of dt")
    parser.add_argument("--limit-regrasp", action="store_true",
                        help="Terminate an episode as failure on the 4th regrasp")
    parser.add_argument("--negative", action="store_true",
                        help="Use negative desired_rel_z (solid-up training mode)")
    parser.add_argument("--mix", action="store_true",
                        help="Mix normal and solid-up modes: randomly assign positive/negative desired_rel_z per env")
    parser.add_argument("--complex", action="store_true",
                        help="Use complex force/regrasp reward (default: simple EE height shaping reward)")
    parser.add_argument("--randomize", action="store_true",
                        help="Enable domain randomization: obs noise and initial joint velocity perturbation")
    parser.add_argument("--randomize-at", type=int, default=None, metavar="ITER",
                        help="Curriculum: train with randomization OFF, then switch it ON at this iteration "
                             "and continue to --max_iterations in the same run (no checkpoint reload)")
    parser.add_argument("--normalization", action="store_true",
                        help="Enable fixed observation normalization (scales each obs channel to ~[-1, 1])")
    parser.add_argument("--zero", action="store_true",
                        help="Enable post-pulse zero-z-velocity + close-gripper hold inside the environment")
    parser.add_argument("--control-error", action="store_true",
                        help="Add per-episode constant z-velocity command bias sampled in [-0.05, -0.03] U [0.03, 0.05]")
    args = parser.parse_args()

    if args.randomize_at is not None:
        if args.randomize:
            parser.error("--randomize-at and --randomize are mutually exclusive: "
                         "--randomize-at already turns randomization on mid-run")
        if not 0 < args.randomize_at < args.max_iterations:
            parser.error(f"--randomize-at must be in (0, --max_iterations={args.max_iterations})")

    # Resolve the env class first so a bad --env fails before logs are touched
    env_cls = load_env_class(args.env)
    print(f"Environment: {env_cls.__module__}.{env_cls.__name__}")

    log_dir = f"logs/{args.exp_name}"
    train_cfg = get_train_cfg(args.exp_name)

    # Fresh run: wipe old logs; resume: keep them
    if args.resume is None:
        if os.path.exists(log_dir):
            shutil.rmtree(log_dir)
    os.makedirs(log_dir, exist_ok=True)

    with open(f"{log_dir}/train_cfg.pkl", "wb") as f:
        pickle.dump(train_cfg, f)

    # Record which env this run used, so eval/replay can reproduce the setup
    with open(f"{log_dir}/env_cfg.pkl", "wb") as f:
        pickle.dump({
            "env": args.env,
            "class": env_cls.__name__,
            "mix": args.mix,
            "normalize": args.normalization,
            "randomize": args.randomize,
            "randomize_at": args.randomize_at,
            "zero": args.zero,
            "control_error": args.control_error,
            "limit_regrasp": args.limit_regrasp,
            "solid_up": args.negative,
            "dt": args.dt,
            "target_dt": args.target_dt,
        }, f)

    gs.init(
        backend=gs.gpu,
        precision="32",
        logging_level="warning",
        seed=args.seed,
        performance_mode=True,
    )

    env = build_env(
        env_cls,
        dict(
            num_envs=args.num_envs,
            vis=False,
            dt=args.dt,
            target_dt=args.target_dt,
            limit_regrasp=args.limit_regrasp,
            solid_up=args.negative,
            mix=args.mix,
            randomize=args.randomize,
            normalize=args.normalization,
            zero=args.zero,
            control_error=args.control_error,
        ),
    )

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)

    if args.resume is not None:
        runner.load(args.resume)
        print(f"Resumed from checkpoint: {args.resume}")

    if args.randomize_at is None:
        runner.learn(num_learning_iterations=args.max_iterations, init_at_random_ep_len=True)
    else:
        if not hasattr(env, "randomize"):
            raise RuntimeError(
                f"--randomize-at requires an env with a 'randomize' attribute; "
                f"{env_cls.__name__} has none"
            )

        # Phase 1: randomization off
        runner.learn(num_learning_iterations=args.randomize_at, init_at_random_ep_len=True)

        # Flip randomization on in place. Every env read of self.randomize happens at
        # runtime (obs noise per step; pulse/gain/velocity sampling per episode reset),
        # so this takes effect immediately without rebuilding the scene.
        setattr(env, "randomize", True)
        # Step past the last completed iteration so phase 2 does not repeat it
        runner.current_learning_iteration += 1
        print(f"\n=== Randomization ON at iteration {runner.current_learning_iteration} ===\n")

        # Phase 2: same process, same optimizer state, same log dir
        runner.learn(
            num_learning_iterations=args.max_iterations - args.randomize_at,
            init_at_random_ep_len=False,
        )


if __name__ == "__main__":
    main()
