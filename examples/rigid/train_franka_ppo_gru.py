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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from train_franka_ppo import build_parser, run_training, validate_args


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


def main():
    parser = build_parser(description="PPO training with a GRU actor for FrankaEnvParallel")
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

    run_training(args, get_train_cfg(args.exp_name, args))


if __name__ == "__main__":
    main()
