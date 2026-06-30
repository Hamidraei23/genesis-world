"""
PPO training for FrankaEnvParallel with bidirectional LSTM actor/critic.

Usage (from workspace root):
    python examples/rigid/train_franka_ppo_lstm.py
    python examples/rigid/train_franka_ppo_lstm.py -B 512 --max_iterations 1000
    python examples/rigid/train_franka_ppo_lstm.py --resume logs/franka-lift-lstm/model_100.pt

Inside docker (genesis container):
    docker exec genesis python /workspace/examples/rigid/train_franka_ppo_lstm.py -B 512
"""

from __future__ import annotations

import argparse
import os
import pickle
import shutil
import sys
from functools import reduce
from importlib import metadata
from pathlib import Path

import torch
import torch.nn as nn
from tensordict import TensorDict

try:
    if int(metadata.version("rsl-rl-lib").split(".")[0]) < 5:
        raise ImportError
except (metadata.PackageNotFoundError, ImportError) as e:
    raise ImportError("Please install 'rsl-rl-lib>=5.0.0'.") from e

from rsl_rl.modules import EmpiricalNormalization, HiddenState
from rsl_rl.modules.distribution import Distribution
from rsl_rl.runners import OnPolicyRunner
from rsl_rl.utils import resolve_callable, resolve_nn_activation

import genesis as gs

try:
    from .env_franka_parallel import FrankaEnvParallel
except ImportError:
    from env_franka_parallel import FrankaEnvParallel


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SEQUENCE_LENGTH = 50
MODEL_CLASS = "examples.rigid.train_franka_ppo_lstm:SequenceBidirectionalLSTMModel"


# ---------------------------------------------------------------------------
# Sequence observation wrapper
# ---------------------------------------------------------------------------


class ObservationHistoryWrapper:
    """Expose the last `sequence_length` policy observations as one observation window."""

    def __init__(self, env: FrankaEnvParallel, sequence_length: int):
        self.env = env
        self.sequence_length = sequence_length
        self.num_envs = env.num_envs
        self.num_actions = env.num_actions
        self.max_episode_length = env.max_episode_length
        self.device = env.device
        self.extras = env.extras
        self.cfg = {
            **env.cfg,
            "obs_history_length": sequence_length,
            "obs_dim_per_step": env.cfg["obs_dim"],
        }
        self._history: torch.Tensor | None = None

    @property
    def episode_length_buf(self) -> torch.Tensor:
        return self.env.episode_length_buf

    @episode_length_buf.setter
    def episode_length_buf(self, value: torch.Tensor) -> None:
        self.env.episode_length_buf = value

    def __getattr__(self, name: str):
        return getattr(self.env, name)

    def reset(self, envs_idx: torch.Tensor | None = None, *args, **kwargs) -> TensorDict:
        obs = self.env.reset(envs_idx, *args, **kwargs)
        flat = obs["policy"]
        if self._history is None:
            self._history = flat.unsqueeze(1).repeat(1, self.sequence_length, 1)
        elif envs_idx is None:
            self._history[:] = flat.unsqueeze(1)
        else:
            self._history[envs_idx] = flat[envs_idx].unsqueeze(1)
        return self.get_observations()

    def step(self, actions: torch.Tensor):
        obs, rewards, dones, extras = self.env.step(actions)
        self._append(obs["policy"], dones)
        return self.get_observations(), rewards, dones, extras

    def get_observations(self) -> TensorDict:
        if self._history is None:
            flat = self.env.get_observations()["policy"]
            self._history = flat.unsqueeze(1).repeat(1, self.sequence_length, 1)
        return TensorDict({"policy": self._history}, batch_size=[self.num_envs])

    def _append(self, flat_obs: torch.Tensor, dones: torch.Tensor) -> None:
        if self._history is None:
            self._history = flat_obs.unsqueeze(1).repeat(1, self.sequence_length, 1)
            return

        self._history = torch.roll(self._history, shifts=-1, dims=1)
        self._history[:, -1, :] = flat_obs

        done_idx = dones.bool().nonzero(as_tuple=False).squeeze(-1)
        if done_idx.numel() > 0:
            self._history[done_idx] = flat_obs[done_idx].unsqueeze(1)


# ---------------------------------------------------------------------------
# Bidirectional LSTM model for rsl_rl
# ---------------------------------------------------------------------------


def _build_head(
    input_dim: int,
    output_dim: int | tuple[int, ...] | list[int],
    hidden_dims: tuple[int, ...] | list[int],
    activation: str,
) -> nn.Module:
    layers: list[nn.Module] = []
    last_dim = input_dim
    activation_mod = resolve_nn_activation(activation)

    for hidden_dim in hidden_dims:
        layers.append(nn.Linear(last_dim, hidden_dim))
        layers.append(activation_mod)
        last_dim = hidden_dim

    if isinstance(output_dim, int):
        layers.append(nn.Linear(last_dim, output_dim))
    else:
        total_out_dim = reduce(lambda x, y: x * y, output_dim)
        layers.append(nn.Linear(last_dim, total_out_dim))
        layers.append(nn.Unflatten(dim=-1, unflattened_size=output_dim))

    return nn.Sequential(*layers)


class SequenceBidirectionalLSTMModel(nn.Module):
    """
    rsl_rl-compatible actor/critic model using a bidirectional LSTM over fixed observation history.

    The model is non-recurrent from rsl_rl's storage perspective: the environment supplies a causal
    past-to-current observation window of length `sequence_length`, and the LSTM consumes that full window.
    """

    is_recurrent: bool = False

    def __init__(
        self,
        obs: TensorDict,
        obs_groups: dict[str, list[str]],
        obs_set: str,
        output_dim: int,
        hidden_dims: tuple[int, ...] | list[int] = (),
        activation: str = "elu",
        obs_normalization: bool = False,
        distribution_cfg: dict | None = None,
        sequence_length: int = SEQUENCE_LENGTH,
        lstm_hidden_dim: int = 128,
        lstm_num_layers: int = 1,
        lstm_dropout: float = 0.0,
    ) -> None:
        super().__init__()

        self.obs_groups = obs_groups[obs_set]
        self.sequence_length = sequence_length
        self.obs_dim = self._get_step_obs_dim(obs)
        self.obs_normalization = obs_normalization
        self.obs_normalizer = EmpiricalNormalization(self.obs_dim) if obs_normalization else nn.Identity()

        dropout = lstm_dropout if lstm_num_layers > 1 else 0.0
        self.lstm = nn.LSTM(
            input_size=self.obs_dim,
            hidden_size=lstm_hidden_dim,
            num_layers=lstm_num_layers,
            dropout=dropout,
            bidirectional=True,
            batch_first=True,
        )
        self.lstm_num_layers = lstm_num_layers
        self.lstm_hidden_dim = lstm_hidden_dim
        self.lstm_output_dim = 2 * lstm_hidden_dim

        if distribution_cfg is not None:
            dist_cfg = dict(distribution_cfg)
            dist_class: type[Distribution] = resolve_callable(dist_cfg.pop("class_name"))  # type: ignore
            self.distribution: Distribution | None = dist_class(output_dim, **dist_cfg)
            head_output_dim = self.distribution.input_dim
        else:
            self.distribution = None
            head_output_dim = output_dim

        self.head = _build_head(self.lstm_output_dim, head_output_dim, hidden_dims, activation)
        if self.distribution is not None:
            self.distribution.init_mlp_weights(self.head)

        self._inference_history: torch.Tensor | None = None

    def forward(
        self,
        obs: TensorDict,
        masks: torch.Tensor | None = None,
        hidden_state: HiddenState = None,
        stochastic_output: bool = False,
    ) -> torch.Tensor:
        if masks is not None or hidden_state is not None:
            raise ValueError("SequenceBidirectionalLSTMModel expects fixed history windows, not rsl_rl RNN batches.")

        sequence = self._get_sequence(obs)
        sequence = self.obs_normalizer(sequence)
        _, (hidden, _) = self.lstm(sequence)
        hidden = hidden.view(self.lstm_num_layers, 2, sequence.shape[0], self.lstm_hidden_dim)
        latent = torch.cat((hidden[-1, 0], hidden[-1, 1]), dim=-1)
        head_output = self.head(latent)

        if self.distribution is not None:
            if stochastic_output:
                self.distribution.update(head_output)
                return self.distribution.sample()
            return self.distribution.deterministic_output(head_output)
        return head_output

    def reset(self, dones: torch.Tensor | None = None, hidden_state: HiddenState = None) -> None:
        if hidden_state is not None:
            raise ValueError("SequenceBidirectionalLSTMModel does not use hidden_state.")
        if self._inference_history is None:
            return
        if dones is None:
            self._inference_history = None
            return
        done_idx = dones.bool().nonzero(as_tuple=False).squeeze(-1)
        if done_idx.numel() > 0:
            self._inference_history[done_idx] = 0.0

    def get_hidden_state(self) -> HiddenState:
        return None

    def detach_hidden_state(self, dones: torch.Tensor | None = None) -> None:
        pass

    @property
    def output_mean(self) -> torch.Tensor:
        return self.distribution.mean  # type: ignore

    @property
    def output_std(self) -> torch.Tensor:
        return self.distribution.std  # type: ignore

    @property
    def output_entropy(self) -> torch.Tensor:
        return self.distribution.entropy  # type: ignore

    @property
    def output_distribution_params(self) -> tuple[torch.Tensor, ...]:
        return self.distribution.params  # type: ignore

    def get_output_log_prob(self, outputs: torch.Tensor) -> torch.Tensor:
        return self.distribution.log_prob(outputs)  # type: ignore

    def get_kl_divergence(
        self, old_params: tuple[torch.Tensor, ...], new_params: tuple[torch.Tensor, ...]
    ) -> torch.Tensor:
        return self.distribution.kl_divergence(old_params, new_params)  # type: ignore

    def update_normalization(self, obs: TensorDict) -> None:
        if self.obs_normalization:
            sequence = self._get_sequence(obs, update_inference_history=False)
            self.obs_normalizer.update(sequence.reshape(-1, self.obs_dim))  # type: ignore

    def _get_sequence(self, obs: TensorDict, *, update_inference_history: bool = True) -> torch.Tensor:
        obs_parts = [obs[obs_group] for obs_group in self.obs_groups]

        if all(part.dim() == 3 for part in obs_parts):
            sequence = torch.cat(obs_parts, dim=-1)
            if sequence.shape[1] != self.sequence_length:
                raise ValueError(
                    f"Expected observation history length {self.sequence_length}, got {sequence.shape[1]}."
                )
            return sequence

        if all(part.dim() == 2 for part in obs_parts):
            flat = torch.cat(obs_parts, dim=-1)
            if not update_inference_history:
                return flat.unsqueeze(1).repeat(1, self.sequence_length, 1)
            return self._update_inference_history(flat)

        shapes = [tuple(part.shape) for part in obs_parts]
        raise ValueError(f"All observation groups must be flat or sequence tensors, got shapes {shapes}.")

    def _update_inference_history(self, flat: torch.Tensor) -> torch.Tensor:
        if self._inference_history is None or self._inference_history.shape[0] != flat.shape[0]:
            self._inference_history = flat.unsqueeze(1).repeat(1, self.sequence_length, 1)
        else:
            self._inference_history = torch.roll(self._inference_history, shifts=-1, dims=1)
            self._inference_history[:, -1, :] = flat
        return self._inference_history

    def _get_step_obs_dim(self, obs: TensorDict) -> int:
        obs_dim = 0
        for obs_group in self.obs_groups:
            shape = obs[obs_group].shape
            if len(shape) == 2:
                obs_dim += shape[-1]
            elif len(shape) == 3:
                if shape[1] != self.sequence_length:
                    raise ValueError(
                        f"Expected '{obs_group}' sequence length {self.sequence_length}, got shape {shape}."
                    )
                obs_dim += shape[-1]
            else:
                raise ValueError(
                    f"SequenceBidirectionalLSTMModel supports flat or sequence observations, "
                    f"got shape {shape} for '{obs_group}'."
                )
        return obs_dim


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_train_cfg(exp_name: str) -> dict:
    """rsl_rl v5+ OnPolicyRunner config dict with bidirectional LSTM actor and critic."""
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
            "class_name": MODEL_CLASS,
            "hidden_dims": [],
            "activation": "elu",
            "sequence_length": SEQUENCE_LENGTH,
            "lstm_hidden_dim": 128,
            "lstm_num_layers": 1,
            "lstm_dropout": 0.0,
            "distribution_cfg": {
                "class_name": "GaussianDistribution",
                "init_std": 1.0,
                "std_type": "scalar",
            },
        },
        "critic": {
            "class_name": MODEL_CLASS,
            "hidden_dims": [],
            "activation": "elu",
            "sequence_length": SEQUENCE_LENGTH,
            "lstm_hidden_dim": 128,
            "lstm_num_layers": 1,
            "lstm_dropout": 0.0,
        },
        # TensorDict key groups fed to actor / critic
        "obs_groups": {
            "actor": ["policy"],
            "critic": ["policy"],
        },
        "num_steps_per_env": SEQUENCE_LENGTH,
        "save_interval": 20,
        "run_name": exp_name,
        "logger": "tensorboard",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="PPO training for FrankaEnvParallel with bidirectional LSTMs")
    parser.add_argument(
        "-e",
        "--exp_name",
        type=str,
        default="franka-lift-lstm",
        help="Experiment name; also used as log subdirectory",
    )
    parser.add_argument("-B", "--num_envs", type=int, default=1024, help="Number of parallel environments")
    parser.add_argument("--max_iterations", type=int, default=1000, help="Total PPO update iterations")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Path to a checkpoint .pt file to resume training from",
    )
    parser.add_argument("--dt", type=float, default=0.001, help="Physics sim timestep (seconds)")
    parser.add_argument(
        "--target_dt",
        type=float,
        default=0.02,
        help="High-level action period (seconds); must be a multiple of dt",
    )
    parser.add_argument(
        "--limit-regrasp",
        action="store_true",
        help="Terminate an episode as failure on the 4th regrasp",
    )
    parser.add_argument("--negative", action="store_true", help="Use negative desired_rel_z (solid-up training mode)")
    parser.add_argument(
        "--mix",
        action="store_true",
        help="Mix normal and solid-up modes: randomly assign positive/negative desired_rel_z per env",
    )
    parser.add_argument(
        "--complex",
        action="store_true",
        help="Use complex force/regrasp reward (default: simple EE height shaping reward)",
    )
    parser.add_argument(
        "--randomize",
        action="store_true",
        help="Enable domain randomization: obs noise and initial joint velocity perturbation",
    )
    parser.add_argument(
        "--normalization",
        action="store_true",
        help="Enable fixed observation normalization (scales each obs channel to ~[-1, 1])",
    )
    args = parser.parse_args()

    log_dir = f"logs/{args.exp_name}"
    train_cfg = get_train_cfg(args.exp_name)

    # Fresh run: wipe old logs; resume: keep them
    if args.resume is None:
        if os.path.exists(log_dir):
            shutil.rmtree(log_dir)
    os.makedirs(log_dir, exist_ok=True)

    with open(f"{log_dir}/train_cfg.pkl", "wb") as f:
        pickle.dump(train_cfg, f)

    gs.init(
        backend=gs.gpu,
        precision="32",
        logging_level="warning",
        seed=args.seed,
        performance_mode=True,
    )

    env = FrankaEnvParallel(
        num_envs=args.num_envs,
        vis=False,
        dt=args.dt,
        target_dt=args.target_dt,
        limit_regrasp=args.limit_regrasp,
        solid_up=args.negative,
        mix=args.mix,
        randomize=args.randomize,
        normalize=args.normalization,
    )
    env = ObservationHistoryWrapper(env, SEQUENCE_LENGTH)

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)

    if args.resume is not None:
        runner.load(args.resume)
        print(f"Resumed from checkpoint: {args.resume}")

    runner.learn(num_learning_iterations=args.max_iterations, init_at_random_ep_len=True)


if __name__ == "__main__":
    main()
