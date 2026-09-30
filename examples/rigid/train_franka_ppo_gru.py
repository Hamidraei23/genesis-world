"""PPO training with a GRU (recurrent) actor for FrankaEnvParallel-style envs.

Same environment, flags and reward table as train_franka_ppo.py; the only
difference is the policy architecture and the rollout length.

The actor is an rsl_rl ``RNNModel`` with ``rnn_type="gru"``: each observation
goes through a GRU whose hidden state carries across steps, and the GRU output
feeds the same MLP head used by the feedforward policy. The hidden state is
zeroed per environment on episode termination by the PPO algorithm.

BPTT horizon
------------
rsl_rl backpropagates through the whole rollout that PPO collected: the update
splits the rollout at episode boundaries, pads each segment and unrolls the GRU
over it. The gradient therefore reaches back at most ``num_steps_per_env``
steps, so that value *is* the GRU horizon. This script sets it to 64 by
default (``--horizon``).

Recurrent mini-batching also differs from the feedforward case: mini-batches
split over *environments*, not over time, so each of the ``num_mini_batches``
chunks holds ``num_envs / num_mini_batches`` full 64-step sequences.

Usage:
    python3 examples/rigid/train_franka_ppo_gru.py -e franka-lift-vori \
        --env env_franka_parallel --max_iterations 1000 --normalization -B 1024
"""

import atexit
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from train_franka_ppo import build_parser, load_env_class, run_training, validate_args


DEFAULT_HORIZON = 64


def get_train_cfg(exp_name: str, args) -> dict:
    """rsl_rl v5+ OnPolicyRunner config with a GRU actor (and optional GRU critic)."""
    rnn_kwargs = {
        "rnn_type": "gru",
        "rnn_hidden_dim": args.rnn_hidden_dim,
        "rnn_num_layers": args.rnn_layers,
    }

    actor = {
        "class_name": "RNNModel",
        # MLP head applied to the GRU output
        "hidden_dims": [256, 128, 64],
        "activation": "elu",
        "distribution_cfg": {
            "class_name": "GaussianDistribution",
            "init_std": 1.0,
            "std_type": "scalar",
        },
        **rnn_kwargs,
    }

    critic = {
        "class_name": "MLPModel" if args.mlp_critic else "RNNModel",
        "hidden_dims": [256, 128, 64],
        "activation": "elu",
    }
    if not args.mlp_critic:
        critic.update(rnn_kwargs)

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
        "actor": actor,
        "critic": critic,
        "obs_groups": {
            "actor": ["policy"],
            "critic": ["policy"],
        },
        # This is the GRU horizon: gradients reach back at most this many steps.
        "num_steps_per_env": args.horizon,
        "save_interval": 20,
        "run_name": exp_name,
        "logger": "tensorboard",
    }


# ---------------------------------------------------------------------------
# Weights & Biases (--wandb)
# ---------------------------------------------------------------------------
# Every scalar rsl_rl writes (losses, mean reward, episode length) and every
# scalar the reward table writes (RewardTerms/*, EpisodeReturn/*, Episodes/*,
# Diagnostics/*) goes to W&B as well as TensorBoard. Run config holds the CLI
# args, the env's constants (reward weights, thresholds, gains) and env.cfg.

def _plain(value):
    """Keep only values W&B can store in its config."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        items = [_plain(v) for v in value]
        return items if all(v is not _SKIP for v in items) else _SKIP
    if isinstance(value, dict):
        out = {str(k): _plain(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not _SKIP}
    return _SKIP


_SKIP = object()


def _make_wandb_writer_class():
    import wandb
    from torch.utils.tensorboard import SummaryWriter
    from rsl_rl.utils import WandbLogWriter

    class FrankaWandbLogWriter(WandbLogWriter):
        """TensorBoard + W&B, one W&B run for the whole process.

        rsl_rl's own writer passes a start_method wandb>=0.30 rejects, calls asdict()
        on env.cfg (a plain dict here), and finishes the run at the end of every
        learn() call, which would split a --randomize-at run in two.
        """

        def __init__(self, log_dir, project_name, entity=None, run_config=None):
            SummaryWriter.__init__(self, log_dir, flush_secs=10)
            self.logged_videos = set()
            # Scalars for one iteration, sent to W&B as a single row.
            self._pending: dict = {}
            self._pending_it = None
            if wandb.run is None:
                wandb.init(
                    project=project_name,
                    entity=entity or os.environ.get("WANDB_ENTITY") or os.environ.get("WANDB_USERNAME"),
                    name=os.path.basename(os.path.normpath(log_dir)),
                    dir=log_dir,
                    config=run_config or {},
                )
                # Plot everything against "iteration" instead of W&B's own step, so
                # a scalar arriving for an earlier iteration is kept, not dropped.
                wandb.define_metric("iteration")
                wandb.define_metric("*", step_metric="iteration")
                print(f"W&B run: {wandb.run.url}", flush=True)
            atexit.register(self._flush)

        def add_scalar(self, tag, scalar_value, global_step=None, walltime=None, new_style=False):
            SummaryWriter.add_scalar(
                self, tag, scalar_value, global_step=global_step, walltime=walltime, new_style=new_style
            )
            if global_step != self._pending_it:
                self._flush()
                self._pending_it = global_step
            self._pending[tag] = float(scalar_value)

        def _flush(self):
            if self._pending and wandb.run is not None:
                row = dict(self._pending)
                if self._pending_it is not None:
                    row["iteration"] = int(self._pending_it)
                wandb.log(row)
            self._pending = {}

        def store_config(self, env_cfg, train_cfg):
            env_dict = env_cfg if isinstance(env_cfg, dict) else vars(env_cfg)
            wandb.config.update(
                {"train_cfg": _plain(train_cfg), "env_cfg": _plain(env_dict)}, allow_val_change=True
            )

        def save_video(self, video, it):
            if video.name not in self.logged_videos:
                wandb.log({"video": wandb.Video(str(video), format="mp4"), "iteration": int(it)})
                self.logged_videos.add(video.name)

        def stop(self):
            # Send the last iteration and close TensorBoard; the W&B run itself
            # finishes when the process exits.
            self._flush()
            self.close()

    return FrankaWandbLogWriter


def enable_wandb(args, train_cfg: dict) -> None:
    """Route rsl_rl's logger to W&B for this run."""
    import rsl_rl.utils.logger as rsl_logger

    writer_cls = _make_wandb_writer_class()
    env_cls = load_env_class(args.env)
    env_constants = {
        k: _plain(getattr(env_cls, k)) for k in dir(env_cls) if k.isupper() and not k.startswith("_")
    }
    logger_cfg = {
        "class_name": writer_cls,
        "project_name": args.wandb_project,
        "entity": args.wandb_entity,
        "run_config": {
            "args": _plain(vars(args)),
            "env_class": f"{env_cls.__module__}.{env_cls.__name__}",
            "env_constants": {k: v for k, v in env_constants.items() if v is not _SKIP},
        },
    }

    # rsl_rl pops class_name out of cfg["logger"] each time learn() builds its
    # writer, so a second learn() (--randomize-at) would crash. Hand it a fresh
    # copy on every call instead; train_cfg.pkl keeps the plain "tensorboard".
    original_init = rsl_logger.Logger.init_logging_writer

    def init_logging_writer(self):
        self.cfg["logger"] = dict(logger_cfg)
        original_init(self)

    rsl_logger.Logger.init_logging_writer = init_logging_writer


def main():
    parser = build_parser(description="PPO training with a GRU actor for FrankaEnvParallel")
    parser.add_argument("--wandb", action="store_true",
                        help="Also log to Weights & Biases (TensorBoard logging continues)")
    parser.add_argument("--wandb-project", type=str, default="franka-regrasp",
                        help="W&B project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
                        help="W&B user or team (default: WANDB_ENTITY / WANDB_USERNAME, then your default)")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                        help=f"GRU BPTT horizon = rollout steps per env (default: {DEFAULT_HORIZON})")
    parser.add_argument("--rnn-hidden-dim", type=int, default=256,
                        help="GRU hidden state size")
    parser.add_argument("--rnn-layers", type=int, default=1,
                        help="Number of stacked GRU layers")
    parser.add_argument("--mlp-critic", action="store_true",
                        help="Keep the critic feedforward instead of giving it its own GRU")
    args = parser.parse_args()
    validate_args(parser, args)

    if args.horizon < 1:
        parser.error("--horizon must be at least 1")
    if args.num_envs % 4 != 0:
        parser.error("-B/--num_envs must be divisible by num_mini_batches (4) for recurrent mini-batching")

    print(f"GRU actor: hidden={args.rnn_hidden_dim} layers={args.rnn_layers} "
          f"horizon={args.horizon} critic={'MLP' if args.mlp_critic else 'GRU'}")

    train_cfg = get_train_cfg(args.exp_name, args)
    if args.wandb:
        enable_wandb(args, train_cfg)
    run_training(args, train_cfg)


if __name__ == "__main__":
    main()
