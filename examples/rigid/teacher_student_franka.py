"""
Teacher-student distillation for the Franka lift task.

Teacher  : PPO policy trained on env_franka_parallel_backup_2h (OBS_DIM=16,
           includes left/right finger contact-force magnitudes at indices 4-5).
           Checkpoints live in logs/<exp_name>-2H-control/.
Student  : MLP with the same architecture as the PPO actor, but consuming the
           14-dim observation of env_franka_parallel (teacher obs minus the two
           force channels). Trained by behaviour cloning on teacher rollouts.

All student artifacts are written to logs/<exp_name>-2H-control/student/:
    demo_data.pt        (student_obs, teacher_obs, action, value) samples from --demo
    student_policy.pt   best distilled weights from --train_student
    model_0.pt          rsl_rl OnPolicyRunner checkpoint: distilled actor mean,
                        calibrated exploration std (KL loss), distilled critic —
                        usable both for rollout and as a PPO fine-tuning init
    train_cfg.pkl       runner config matching model_0.pt
    bc_loss.png         distillation loss curves
    plots/              episode plots from --student_demo

Fine-tune the distilled student with PPO (unmodified trainer, 14-obs env):
    python3 examples/rigid/train_franka_ppo.py -e franka-lift-v1-student-ft \
        --resume logs/franka-lift-v1-2H-control/student/model_0.pt \
        --mix --zero --randomize --normalization --control-error

Usage (inside the genesis docker container, from /workspace):

    # 1) Roll out the 2H teacher (with the 2H domain randomization active)
    #    and record 100000 (student_obs, action) samples:
    python3 examples/rigid/teacher_student_franka.py -e franka-lift-v1 --ckpt 3200 --demo \
        --mix --normalization --zero --randomize --control-error

    # 2) Behaviour-clone the 14-obs student from the recorded samples:
    python3 examples/rigid/teacher_student_franka.py -e franka-lift-v1 --train_student

    # 3) DAgger rounds (repeat 2-3 times): roll out the CURRENT student inside
    #    the 2H env, relabel its visited states with teacher actions + values,
    #    aggregate into demo_data.pt, then retrain:
    python3 examples/rigid/teacher_student_franka.py -e franka-lift-v1 --ckpt 3200 --dagger \
        --mix --normalization --zero --randomize --control-error
    python3 examples/rigid/teacher_student_franka.py -e franka-lift-v1 --train_student

    # 4) Visualise the student in env_franka_parallel (14 obs), same criteria:
    python3 examples/rigid/teacher_student_franka.py -e franka-lift-v1 --student_demo --plot \
        --mix --normalization --zero --randomize --control-error

    # The rsl_rl-format checkpoint also works with the unmodified runner script:
    python3 examples/rigid/run_policy_franka_parallel.py -e franka-lift-v1-2H-control/student --plot --normalization
"""

import argparse
import os
import pickle
import time
from importlib import metadata
from itertools import chain

import matplotlib
matplotlib.use("Agg")  # headless - required inside Docker (no $DISPLAY)
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn

try:
    from .env_franka_parallel import FrankaEnvParallel as FrankaEnvStudent
except ImportError:
    from env_franka_parallel import FrankaEnvParallel as FrankaEnvStudent

try:
    from .env_franka_parallel_backup_2h import FrankaEnvParallel as FrankaEnvTeacher2H
except ImportError:
    from env_franka_parallel_backup_2h import FrankaEnvParallel as FrankaEnvTeacher2H

try:
    from .train_franka_ppo import get_train_cfg
except ImportError:
    from train_franka_ppo import get_train_cfg


# ---------------------------------------------------------------------------
# Teacher(16) -> Student(14) observation mapping
# ---------------------------------------------------------------------------
# The 2H teacher observation is the student observation plus two finger
# contact-force magnitudes at indices 4-5; every other channel (and its
# fixed normalization scale) matches one-to-one, so distillation is just
# dropping those two columns.

_TEACHER_FORCE_IDX = (FrankaEnvTeacher2H.OBS_LEFT_FORCE_MAG, FrankaEnvTeacher2H.OBS_RIGHT_FORCE_MAG)
TEACHER_TO_STUDENT_IDX = [i for i in range(FrankaEnvTeacher2H.OBS_DIM) if i not in _TEACHER_FORCE_IDX]


def _check_obs_compatibility():
    assert len(TEACHER_TO_STUDENT_IDX) == FrankaEnvStudent.OBS_DIM, (
        f"teacher obs minus force channels has {len(TEACHER_TO_STUDENT_IDX)} dims, "
        f"but student OBS_DIM is {FrankaEnvStudent.OBS_DIM}"
    )
    t_scale = [FrankaEnvTeacher2H.OBS_SCALE[i] for i in TEACHER_TO_STUDENT_IDX]
    s_scale = list(FrankaEnvStudent.OBS_SCALE)
    assert t_scale == s_scale, (
        "normalization scales of the shared channels differ between teacher and student envs:\n"
        f"  teacher (minus forces): {t_scale}\n"
        f"  student               : {s_scale}"
    )


def teacher_obs_to_student_obs(teacher_obs: torch.Tensor) -> torch.Tensor:
    """Drop the two force-magnitude channels: (N, 16) -> (N, 14)."""
    return teacher_obs[:, TEACHER_TO_STUDENT_IDX]


# ---------------------------------------------------------------------------
# Student policy (same MLP architecture as the PPO actor: 256-128-64, ELU)
# ---------------------------------------------------------------------------

def _make_mlp(in_dim: int, out_dim: int, hidden_dims=(256, 128, 64)) -> nn.Sequential:
    layers: list[nn.Module] = []
    prev = in_dim
    for h in hidden_dims:
        layers += [nn.Linear(prev, h), nn.ELU()]
        prev = h
    layers.append(nn.Linear(prev, out_dim))
    return nn.Sequential(*layers)


class StudentPolicy(nn.Module):
    """Gaussian student actor: MLP mean + state-independent learnable std.
    Submodule named `mlp` so that state-dict keys (mlp.0.weight, ...) line up
    with rsl_rl's MLPModel actor keys; `log_std` maps onto the exported
    GaussianDistribution std_param (scalar space)."""

    def __init__(self, obs_dim: int = FrankaEnvStudent.OBS_DIM, num_actions: int = 3,
                 init_std: float = 0.3):
        super().__init__()
        self.obs_dim = obs_dim
        self.num_actions = num_actions
        self.mlp = _make_mlp(obs_dim, num_actions)
        self.log_std = nn.Parameter(torch.full((num_actions,), float(np.log(init_std))))

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        """Deterministic (mean) action."""
        return self.mlp(obs)

    @property
    def std(self) -> torch.Tensor:
        return self.log_std.exp().clamp(1e-4, 10.0)


class _GaussianStdShim(nn.Module):
    """Holds `std_param` so the exported actor state dict matches
    GaussianDistribution(std_type='scalar') from rsl_rl."""

    def __init__(self, num_actions: int, init_std: float):
        super().__init__()
        self.std_param = nn.Parameter(torch.full((num_actions,), init_std))


class _RslActorShim(nn.Module):
    """state_dict-compatible stand-in for the rsl_rl MLPModel actor."""

    def __init__(self, obs_dim: int, num_actions: int, init_std: float = 0.2):
        super().__init__()
        self.mlp = _make_mlp(obs_dim, num_actions)
        self.distribution = _GaussianStdShim(num_actions, init_std)


class _RslCriticShim(nn.Module):
    """state_dict-compatible stand-in for the rsl_rl MLPModel critic."""

    def __init__(self, obs_dim: int):
        super().__init__()
        self.mlp = _make_mlp(obs_dim, 1)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def teacher_log_dir(exp_name: str) -> str:
    return f"logs/{exp_name}-2H-control"


def student_dir(exp_name: str) -> str:
    return os.path.join(teacher_log_dir(exp_name), "student")


# ---------------------------------------------------------------------------
# --demo : teacher rollout -> (student_obs, action) dataset
# ---------------------------------------------------------------------------

def run_demo(args):
    from rsl_rl.runners import OnPolicyRunner
    import genesis as gs

    log_dir = teacher_log_dir(args.exp_name)
    out_dir = student_dir(args.exp_name)
    os.makedirs(out_dir, exist_ok=True)

    # ---- resolve teacher checkpoint ----
    if args.ckpt is not None:
        ckpt_path = os.path.join(log_dir, f"model_{args.ckpt}.pt")
    else:
        pts = [f for f in os.listdir(log_dir) if f.startswith("model_") and f.endswith(".pt")]
        if not pts:
            raise FileNotFoundError(f"No model_*.pt found in {log_dir}")
        pts.sort(key=lambda f: int(f.split("_")[1].split(".")[0]))
        ckpt_path = os.path.join(log_dir, pts[-1])
    print(f"Teacher checkpoint: {ckpt_path}")

    with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
        train_cfg = pickle.load(f)

    gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=args.seed)

    # randomize=True activates the 2H env's per-episode domain randomization
    # (pulse delay 0-4, pulse length 3-6, initial qvel noise, 3 mm obs noise,
    # zero-hold 0.1-0.3 s) on top of the always-on friction / finger-gain
    # randomization, so the dataset covers the same ranges used for 2H training.
    env = FrankaEnvTeacher2H(
        num_envs=args.num_envs,
        vis=False,
        record=False,
        dt=args.dt,
        target_dt=args.target_dt,
        mix=args.mix,
        normalize=args.normalization,
        randomize=args.randomize,
        zero=args.zero,
        control_error=args.control_error,
    )

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(ckpt_path)
    policy = runner.get_inference_policy(device=gs.device)
    teacher_critic = runner.alg.critic
    teacher_critic.eval()
    # State-independent exploration std of the teacher (GaussianDistribution, std_type="scalar")
    teacher_std = runner.alg.actor.distribution.std_param.detach().clamp(min=1e-6).cpu()
    print(f"Teacher exploration std: {teacher_std.tolist()}")

    # Buffers must be pinned to CPU explicitly: gs.init() makes cuda the torch
    # default device, and the saved dataset is CPU-side.
    n_total = args.num_samples
    obs_teacher_buf = torch.empty(n_total, FrankaEnvTeacher2H.OBS_DIM, dtype=torch.float32, device="cpu")
    obs_student_buf = torch.empty(n_total, FrankaEnvStudent.OBS_DIM, dtype=torch.float32, device="cpu")
    actions_buf = torch.empty(n_total, env.num_actions, dtype=torch.float32, device="cpu")
    values_buf = torch.empty(n_total, 1, dtype=torch.float32, device="cpu")

    # Desynchronise episode phases so the dataset is not dominated by
    # early-episode states (same trick as rsl_rl's init_at_random_ep_len).
    obs_td = env.reset()
    env.episode_length_buf = torch.randint_like(env.episode_length_buf, high=env.max_episode_length)

    collected = 0
    episodes = 0
    ep_rewards_done: list[float] = []
    running_ep_reward = torch.zeros(env.num_envs, device=gs.device)
    t0 = time.perf_counter()

    with torch.no_grad():
        while collected < n_total:
            obs = obs_td["policy"]  # (B, 16) pre-step teacher observation
            actions = policy(obs_td)  # (B, 3) deterministic teacher mean
            values = teacher_critic(obs_td)  # (B, 1) teacher value estimate

            take = min(env.num_envs, n_total - collected)
            obs_teacher_buf[collected:collected + take] = obs[:take].cpu()
            obs_student_buf[collected:collected + take] = teacher_obs_to_student_obs(obs)[:take].cpu()
            actions_buf[collected:collected + take] = actions[:take].cpu()
            values_buf[collected:collected + take] = values[:take].reshape(-1, 1).cpu()
            collected += take

            obs_td, rew_buf, reset_buf, _ = env.step(actions, update_visualizer=False)

            running_ep_reward += rew_buf
            done_idx = reset_buf.nonzero(as_tuple=False).squeeze(-1)
            if done_idx.numel() > 0:
                episodes += done_idx.numel()
                ep_rewards_done.extend(running_ep_reward[done_idx].tolist())
                running_ep_reward[done_idx] = 0.0

            if collected % (env.num_envs * 50) < env.num_envs or collected >= n_total:
                rate = collected / max(time.perf_counter() - t0, 1e-6)
                mean_ep_rew = np.mean(ep_rewards_done[-200:]) if ep_rewards_done else float("nan")
                print(
                    f"  collected {collected:7d}/{n_total}  ({rate:,.0f} samples/s)  "
                    f"episodes_done={episodes}  mean_ep_reward={mean_ep_rew:.1f}"
                )

    data = {
        "obs_student": obs_student_buf,
        "obs_teacher": obs_teacher_buf,
        "actions": actions_buf,
        "values": values_buf,
        "teacher_std": teacher_std,
        "meta": {
            "exp_name": args.exp_name,
            "teacher_ckpt": ckpt_path,
            "num_samples": n_total,
            "num_envs": args.num_envs,
            "mix": args.mix,
            "normalize": args.normalization,
            "randomize": args.randomize,
            "zero": args.zero,
            "control_error": args.control_error,
            "teacher_obs_dim": FrankaEnvTeacher2H.OBS_DIM,
            "student_obs_dim": FrankaEnvStudent.OBS_DIM,
            "teacher_to_student_idx": TEACHER_TO_STUDENT_IDX,
            "student_obs_scale": list(FrankaEnvStudent.OBS_SCALE),
            "dt": args.dt,
            "target_dt": args.target_dt,
            "episodes_completed": episodes,
        },
    }
    out_path = os.path.join(out_dir, "demo_data.pt")
    torch.save(data, out_path)
    print(f"\nSaved {n_total} samples ({episodes} completed episodes) -> {out_path}")
    print(f"Next: python3 examples/rigid/teacher_student_franka.py -e {args.exp_name} --train_student")


# ---------------------------------------------------------------------------
# --dagger : student rollouts in the 2H env, relabeled by the teacher
# ---------------------------------------------------------------------------

def run_dagger(args):
    from rsl_rl.runners import OnPolicyRunner
    import genesis as gs

    log_dir = teacher_log_dir(args.exp_name)
    out_dir = student_dir(args.exp_name)
    data_path = os.path.join(out_dir, "demo_data.pt")
    student_path = os.path.join(out_dir, "student_policy.pt")
    if not os.path.exists(student_path):
        raise FileNotFoundError(f"{student_path} not found - run --demo and --train_student first")
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"{data_path} not found - run --demo first (DAgger aggregates into it)")

    # ---- resolve teacher checkpoint ----
    if args.ckpt is not None:
        ckpt_path = os.path.join(log_dir, f"model_{args.ckpt}.pt")
    else:
        pts = [f for f in os.listdir(log_dir) if f.startswith("model_") and f.endswith(".pt")]
        if not pts:
            raise FileNotFoundError(f"No model_*.pt found in {log_dir}")
        pts.sort(key=lambda f: int(f.split("_")[1].split(".")[0]))
        ckpt_path = os.path.join(log_dir, pts[-1])
    print(f"Teacher checkpoint: {ckpt_path}")

    with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
        train_cfg = pickle.load(f)

    gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=args.seed)

    # The 2H env is required so the teacher can be queried with its privileged
    # (force-including) observation; the student acts from the 14-dim slice.
    env = FrankaEnvTeacher2H(
        num_envs=args.num_envs,
        vis=False,
        record=False,
        dt=args.dt,
        target_dt=args.target_dt,
        mix=args.mix,
        normalize=args.normalization,
        randomize=args.randomize,
        zero=args.zero,
        control_error=args.control_error,
    )

    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)
    runner.load(ckpt_path)
    teacher_policy = runner.get_inference_policy(device=gs.device)
    teacher_critic = runner.alg.critic
    teacher_critic.eval()
    teacher_std = runner.alg.actor.distribution.std_param.detach().clamp(min=1e-6).cpu()

    # ---- student policy that drives the rollout ----
    s_ckpt = torch.load(student_path, map_location=gs.device, weights_only=False)
    student = StudentPolicy(s_ckpt["meta"]["obs_dim"], s_ckpt["meta"]["num_actions"]).to(gs.device)
    student.load_state_dict(s_ckpt["model_state_dict"], strict=False)
    student.eval()
    print(f"Student driving rollouts: {student_path}  "
          f"(val MSE {s_ckpt['meta'].get('best_val_mse', float('nan')):.4f})  beta={args.beta}")

    # CPU-pinned buffers: gs.init() makes cuda the torch default device, and
    # these get concatenated with the CPU-side dataset loaded from disk.
    n_total = args.num_samples
    obs_teacher_buf = torch.empty(n_total, FrankaEnvTeacher2H.OBS_DIM, dtype=torch.float32, device="cpu")
    obs_student_buf = torch.empty(n_total, FrankaEnvStudent.OBS_DIM, dtype=torch.float32, device="cpu")
    actions_buf = torch.empty(n_total, env.num_actions, dtype=torch.float32, device="cpu")
    values_buf = torch.empty(n_total, 1, dtype=torch.float32, device="cpu")

    obs_td = env.reset()
    env.episode_length_buf = torch.randint_like(env.episode_length_buf, high=env.max_episode_length)

    def _count_term(name, done_idx):
        v = env.last_reward_terms.get(name)
        if torch.is_tensor(v) and v.numel() == env.num_envs:
            return int((v.reshape(-1)[done_idx] > 0.5).sum().item())
        return 0

    collected = 0
    episodes = successes = fails = timeouts = 0
    running_ep_reward = torch.zeros(env.num_envs, device=gs.device)
    ep_rewards_done: list[float] = []
    t0 = time.perf_counter()

    with torch.no_grad():
        while collected < n_total:
            teacher_obs = obs_td["policy"]                             # (B, 16)
            student_obs = teacher_obs_to_student_obs(teacher_obs)      # (B, 14)

            teacher_action = teacher_policy(obs_td)                    # relabel: what the teacher would do
            teacher_value = teacher_critic(obs_td)
            student_action = student(student_obs)                      # what actually steers the rollout

            take = min(env.num_envs, n_total - collected)
            obs_teacher_buf[collected:collected + take] = teacher_obs[:take].cpu()
            obs_student_buf[collected:collected + take] = student_obs[:take].cpu()
            actions_buf[collected:collected + take] = teacher_action[:take].cpu()
            values_buf[collected:collected + take] = teacher_value[:take].reshape(-1, 1).cpu()
            collected += take

            # Classic DAgger beta-mixing: execute the teacher's action with
            # probability beta (per env, per step). beta=0 -> pure student.
            exec_action = student_action
            if args.beta > 0.0:
                use_teacher = torch.rand(env.num_envs, 1, device=gs.device) < args.beta
                exec_action = torch.where(use_teacher, teacher_action, student_action)

            obs_td, rew_buf, reset_buf, _ = env.step(exec_action, update_visualizer=False)

            running_ep_reward += rew_buf
            done_idx = reset_buf.nonzero(as_tuple=False).squeeze(-1)
            if done_idx.numel() > 0:
                episodes += done_idx.numel()
                successes += _count_term("success", done_idx)
                fails += _count_term("fail", done_idx)
                timeouts += _count_term("timeout", done_idx)
                ep_rewards_done.extend(running_ep_reward[done_idx].tolist())
                running_ep_reward[done_idx] = 0.0

            if collected % (env.num_envs * 50) < env.num_envs or collected >= n_total:
                rate = collected / max(time.perf_counter() - t0, 1e-6)
                mean_ep_rew = np.mean(ep_rewards_done[-200:]) if ep_rewards_done else float("nan")
                sr = successes / episodes if episodes else float("nan")
                print(
                    f"  collected {collected:7d}/{n_total}  ({rate:,.0f} samples/s)  "
                    f"episodes={episodes}  student_success={sr:.1%}  mean_ep_reward={mean_ep_rew:.1f}"
                )

    print(f"\nStudent rollout stats (2H env): {episodes} episodes - "
          f"{successes} success / {fails} fail / {timeouts} timeout")

    # ---- aggregate into the existing dataset ----
    old = torch.load(data_path, map_location="cpu", weights_only=False)
    meta = old.get("meta", {})
    rounds = meta.get("dagger_rounds", [])
    rounds.append({
        "round": len(rounds) + 1,
        "num_samples": n_total,
        "beta": args.beta,
        "episodes": episodes,
        "student_success_rate": successes / episodes if episodes else None,
        "teacher_ckpt": ckpt_path,
    })
    meta["dagger_rounds"] = rounds

    old_values = old.get("values")
    if old_values is None:
        print("WARNING: existing dataset has no teacher values; aggregated dataset "
              "will only carry values for the DAgger samples' portion — regenerate "
              "--demo first if you want critic distillation over everything")
        old_values = torch.zeros(old["actions"].shape[0], 1)

    data = {
        "obs_student": torch.cat([old["obs_student"], obs_student_buf]),
        "obs_teacher": torch.cat([old["obs_teacher"], obs_teacher_buf]),
        "actions": torch.cat([old["actions"], actions_buf]),
        "values": torch.cat([old_values, values_buf]),
        "teacher_std": old.get("teacher_std", teacher_std),
        "meta": meta,
    }
    torch.save(data, data_path)
    total_n = data["actions"].shape[0]
    print(f"Aggregated {n_total} relabeled samples -> {data_path}  "
          f"(total {total_n}, DAgger rounds: {len(rounds)})")
    print(f"Next: python3 examples/rigid/teacher_student_franka.py -e {args.exp_name} --train_student")


# ---------------------------------------------------------------------------
# --train_student : behaviour cloning on the recorded dataset
# ---------------------------------------------------------------------------

def run_train_student(args):
    out_dir = student_dir(args.exp_name)
    data_path = os.path.join(out_dir, "demo_data.pt")
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"{data_path} not found - run with --demo first")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    data = torch.load(data_path, map_location="cpu", weights_only=False)
    obs = data["obs_student"].float()
    actions = data["actions"].float()
    values = data.get("values")
    n = obs.shape[0]
    print(f"Loaded {n} samples from {data_path}  (obs {tuple(obs.shape)}, actions {tuple(actions.shape)})")

    # Teacher exploration std for the KL loss. sigma_t = 0 degenerates the KL
    # into a Gaussian NLL on the teacher means (used for old datasets that
    # predate value/std recording).
    teacher_std = data.get("teacher_std")
    use_kl = args.loss == "kl"
    if use_kl and teacher_std is None:
        print("WARNING: dataset has no teacher_std (regenerate with --demo); "
              "falling back to NLL (KL with sigma_teacher=0)")
        teacher_std = torch.zeros(actions.shape[1])
    if values is None and use_kl:
        print("WARNING: dataset has no teacher values (regenerate with --demo); "
              "critic will be exported with random init")

    # ---- train / val split ----
    g = torch.Generator().manual_seed(args.seed)
    perm = torch.randperm(n, generator=g)
    n_val = max(1, int(n * args.val_fraction))
    val_idx, train_idx = perm[:n_val], perm[n_val:]
    obs_train, act_train = obs[train_idx].to(device), actions[train_idx].to(device)
    obs_val, act_val = obs[val_idx].to(device), actions[val_idx].to(device)
    if values is not None:
        val_train_t, val_val_t = values[train_idx].to(device), values[val_idx].to(device)
    print(f"train={len(train_idx)}  val={len(val_idx)}  device={device}  loss={args.loss}")

    model = StudentPolicy().to(device)
    critic = _RslCriticShim(model.obs_dim).to(device)
    train_critic = values is not None
    params = list(model.parameters()) + (list(critic.parameters()) if train_critic else [])
    optimizer = torch.optim.Adam(params, lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    sigma_t = teacher_std.to(device) if teacher_std is not None else None
    var_t = sigma_t.square() if sigma_t is not None else None

    def action_loss(mu_s: torch.Tensor, mu_t: torch.Tensor) -> torch.Tensor:
        if not use_kl:
            return torch.nn.functional.mse_loss(mu_s, mu_t)
        # Analytic KL( N(mu_t, sigma_t) || N(mu_s, sigma_s) ), summed over action
        # dims, averaged over the batch. Gradient wrt mu_s is (mu_s-mu_t)/sigma_s^2,
        # so tight-std dims (z-vel) dominate loose dims (gripper) automatically,
        # and sigma_s converges to sqrt(sigma_t^2 + E[(mu_t-mu_s)^2]) — a
        # calibrated exploration std for PPO fine-tuning.
        sigma_s = model.std
        kl = (
            sigma_s.log() - (sigma_t + 1e-8).log()
            + (var_t + (mu_t - mu_s).square()) / (2.0 * sigma_s.square())
            - 0.5
        )
        return kl.sum(dim=-1).mean()

    best_val = float("inf")
    best_state = {k: v.clone() for k, v in model.state_dict().items()}
    best_critic_state = {k: v.clone() for k, v in critic.state_dict().items()}
    best_epoch = 0
    train_losses, val_losses, val_mses, value_losses = [], [], [], []
    n_train = obs_train.shape[0]

    for epoch in range(1, args.epochs + 1):
        model.train()
        ep_perm = torch.randperm(n_train, device=device)
        total, total_value = 0.0, 0.0
        for start in range(0, n_train, args.batch_size):
            idx = ep_perm[start:start + args.batch_size]
            loss = action_loss(model(obs_train[idx]), act_train[idx])
            if train_critic:
                value_loss = torch.nn.functional.mse_loss(critic.mlp(obs_train[idx]), val_train_t[idx])
                total_value += value_loss.item() * idx.numel()
                loss = loss + args.value_coef * value_loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += loss.item() * idx.numel()
        scheduler.step()
        train_loss = total / n_train

        model.eval()
        with torch.no_grad():
            mu_val = model(obs_val)
            val_loss = action_loss(mu_val, act_val).item()
            val_mse = torch.nn.functional.mse_loss(mu_val, act_val).item()
            val_value_mse = (
                torch.nn.functional.mse_loss(critic.mlp(obs_val), val_val_t).item()
                if train_critic else float("nan")
            )
        train_losses.append(train_loss)
        val_losses.append(val_loss)
        val_mses.append(val_mse)
        value_losses.append(total_value / n_train if train_critic else float("nan"))

        if val_loss < best_val:
            best_val = val_loss
            best_epoch = epoch
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            best_critic_state = {k: v.clone() for k, v in critic.state_dict().items()}

        if epoch % max(1, args.epochs // 20) == 0 or epoch == 1:
            std_str = "[" + ", ".join(f"{s:.3f}" for s in model.std.tolist()) + "]"
            print(
                f"  epoch {epoch:4d}/{args.epochs}  train={train_loss:.4f}  "
                f"val={val_loss:.4f}  val_mse={val_mse:.4f}  "
                f"value_mse={val_value_mse:.1f}  std={std_str}  "
                f"best={best_val:.4f}@{best_epoch}  lr={scheduler.get_last_lr()[0]:.2e}"
            )

    model.load_state_dict(best_state)
    critic.load_state_dict(best_critic_state)
    student_std = model.std.detach().cpu()
    print(f"Best val loss {best_val:.6f} at epoch {best_epoch}  student std {student_std.tolist()}")

    # ---- save raw student weights ----
    os.makedirs(out_dir, exist_ok=True)
    student_path = os.path.join(out_dir, "student_policy.pt")
    torch.save(
        {
            "model_state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
            "critic_state_dict": {k: v.cpu() for k, v in critic.state_dict().items()},
            "meta": {
                "exp_name": args.exp_name,
                "obs_dim": model.obs_dim,
                "num_actions": model.num_actions,
                "loss": args.loss,
                "best_val_loss": best_val,
                "best_val_mse": val_mses[best_epoch - 1],
                "best_epoch": best_epoch,
                "epochs": args.epochs,
                "student_std": student_std.tolist(),
                "teacher_std": teacher_std.tolist() if teacher_std is not None else None,
                "critic_distilled": train_critic,
                "demo_meta": data.get("meta", {}),
            },
        },
        student_path,
    )
    print(f"Saved student weights -> {student_path}")

    # ---- save loss curves ----
    n_panels = 2 + int(train_critic)
    fig, axes = plt.subplots(n_panels, 1, figsize=(8, 3.5 * n_panels), sharex=True)
    axes[0].plot(train_losses, label=f"train {args.loss}")
    axes[0].plot(val_losses, label=f"val {args.loss}")
    axes[0].axvline(best_epoch - 1, color="k", linestyle="--", linewidth=0.8, label=f"best (ep {best_epoch})")
    axes[0].set_ylabel(f"{args.loss} loss")
    axes[0].set_title(f"Distillation - {args.exp_name} student ({args.loss} loss)")
    axes[1].plot(val_mses, label="val action MSE", color="C2")
    axes[1].set_yscale("log")
    axes[1].set_ylabel("action MSE")
    if train_critic:
        axes[2].plot(value_losses, label="train value MSE", color="C3")
        axes[2].set_yscale("log")
        axes[2].set_ylabel("value MSE")
    for ax in axes:
        ax.legend()
        ax.grid(True, linewidth=0.4, alpha=0.5)
    axes[-1].set_xlabel("epoch")
    plt.tight_layout()
    loss_png = os.path.join(out_dir, "bc_loss.png")
    fig.savefig(loss_png, dpi=120)
    plt.close(fig)
    print(f"Saved loss curves -> {loss_png}")

    # ---- export rsl_rl OnPolicyRunner-compatible checkpoint ----
    actor = _RslActorShim(model.obs_dim, model.num_actions)
    actor.mlp.load_state_dict({k: v.cpu() for k, v in model.mlp.state_dict().items()})
    with torch.no_grad():
        # GaussianDistribution std_type="scalar": std_param IS the std
        actor.distribution.std_param.copy_(student_std)
    export_critic = _RslCriticShim(model.obs_dim)
    export_critic.load_state_dict({k: v.cpu() for k, v in critic.state_dict().items()})
    # PPO builds one Adam over chain(actor, critic) params; a fresh state dict
    # with the same parameter layout is enough for OnPolicyRunner.load().
    ppo_like_optimizer = torch.optim.Adam(chain(actor.parameters(), export_critic.parameters()), lr=3e-4)
    ckpt = {
        "actor_state_dict": actor.state_dict(),
        "critic_state_dict": export_critic.state_dict(),
        "optimizer_state_dict": ppo_like_optimizer.state_dict(),
        "iter": 0,
        "infos": None,
    }
    ckpt_path = os.path.join(out_dir, "model_0.pt")
    torch.save(ckpt, ckpt_path)

    train_cfg = get_train_cfg(f"{args.exp_name}-student")
    with open(os.path.join(out_dir, "train_cfg.pkl"), "wb") as f:
        pickle.dump(train_cfg, f)
    print(f"Saved rsl_rl-compatible checkpoint -> {ckpt_path} (+ train_cfg.pkl)")

    # Suggest an evaluation command matching the criteria the data was collected with
    demo_meta = data.get("meta", {})
    crit = ""
    if demo_meta.get("mix"):
        crit += " --mix"
    if demo_meta.get("zero"):
        crit += " --zero"
    if demo_meta.get("randomize"):
        crit += " --randomize"
    if demo_meta.get("control_error"):
        crit += " --control-error"
    if not demo_meta.get("normalize", True):
        crit += " --no-normalization"
    ft_crit = crit + (" --normalization" if demo_meta.get("normalize", True) else "")
    print(
        "\nVisualise with either:\n"
        f"  python3 examples/rigid/teacher_student_franka.py -e {args.exp_name} --student_demo --plot{crit}\n"
        f"  python3 examples/rigid/run_policy_franka_parallel.py -e {args.exp_name}-2H-control/student --plot --normalization\n"
        "\nFine-tune with PPO (unmodified trainer, same env criteria):\n"
        f"  python3 examples/rigid/train_franka_ppo.py -e {args.exp_name}-student-ft "
        f"--resume {ckpt_path}{ft_crit}"
    )


# ---------------------------------------------------------------------------
# --student_demo : visualise the student in env_franka_parallel (14 obs)
# ---------------------------------------------------------------------------

def _fresh_demo_buffers():
    return dict(
        steps=[], ee_z=[], cuboid_rel_z=[], desired_rel_z=[],
        ft_dist=[], avg_force=[], reward=[],
        target_z=[], actual_z_vel=[], target_z_vel=[], target_z_acc=[],
    )


def _term_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if torch.is_tensor(value):
        t = value.detach().reshape(-1)
        return float(t[0].item()) if t.numel() > 0 else default
    arr = np.asarray(value).reshape(-1)
    return float(arr[0]) if arr.size > 0 else default


def _plot_student_episode(bufs, release_spans, ep_idx, save_dir):
    steps = np.asarray(bufs["steps"])

    def _shade(ax):
        for k, (s0, s1) in enumerate(release_spans):
            ax.axvspan(s0, s1, color="orange", alpha=0.25, label="release window" if k == 0 else "")

    panels = [
        ([("cuboid_rel_z", "C0", "-"), ("desired_rel_z", "C3", "--")], "Z relative [m]", "Cuboid in-hand Z"),
        ([("ee_z", "C1", "-"), ("target_z", "C3", "--")], "EE height [m]", "End-effector Z (actual vs target)"),
        ([("actual_z_vel", "C0", "-"), ("target_z_vel", "C3", "--")], "Z velocity [m/s]", "Z velocity"),
        ([("target_z_acc", "C5", "-")], "Z accel [m/s²]", "Commanded Z acceleration"),
        ([("ft_dist", "C2", "-")], "Distance [m]", "Fingertip distance"),
        ([("avg_force", "C4", "-")], "Force [N]", "Average finger contact force"),
        ([("reward", "C6", "-")], "Reward", "Reward per step"),
    ]
    fig, axes = plt.subplots(len(panels), 1, figsize=(12, 2.6 * len(panels)), sharex=True)
    fig.suptitle(f"Student episode {ep_idx}  (orange = gripper release)", fontsize=12)
    for ax, (series, ylabel, title) in zip(axes, panels):
        for key, color, ls in series:
            ax.plot(steps, np.asarray(bufs[key]), color=color, linestyle=ls, label=key, linewidth=1.2)
        _shade(ax)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)
    axes[-1].set_xlabel("High-level step")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"student_ep_{ep_idx:03d}.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] -> {path}")


def run_student_demo(args):
    import genesis as gs

    out_dir = student_dir(args.exp_name)
    student_path = os.path.join(out_dir, "student_policy.pt")
    if not os.path.exists(student_path):
        raise FileNotFoundError(f"{student_path} not found - run with --train_student first")

    gs.init(backend=gs.gpu, precision="32", logging_level="warning")

    env = FrankaEnvStudent(
        num_envs=1,
        vis=args.vis,
        record=args.record,
        dt=args.dt,
        target_dt=args.target_dt,
        mix=args.mix,
        normalize=args.normalization,
        randomize=args.randomize,
        zero=args.zero,
        control_error=args.control_error,
    )

    ckpt = torch.load(student_path, map_location=gs.device, weights_only=False)
    model = StudentPolicy(ckpt["meta"]["obs_dim"], ckpt["meta"]["num_actions"]).to(gs.device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    print(f"Loaded student weights: {student_path}  (val MSE {ckpt['meta'].get('best_val_mse', float('nan')):.6f})")

    if args.record:
        env.cam.start_recording()

    obs_td = env.reset()
    obs_scale = torch.tensor(FrankaEnvStudent.OBS_SCALE, device=gs.device)

    high_level_steps = args.steps if args.steps > 0 else float("inf")
    bufs = _fresh_demo_buffers()
    release_spans: list[tuple[int, int]] = []
    in_release = False
    release_start = 0
    force_thresh = env.REGRASP_FORCE_THRESHOLD
    ep_reward, ep_len, ep_count = 0.0, 0, 0
    successes, fails, timeouts = 0, 0, 0
    t_real_start = time.perf_counter()

    print(
        f"target_dt={env.target_period:.3f}s  sim_dt={env.dt:.3f}s  "
        f"steps_per_target={env.target_update_every}  replay_speed={args.replay_speed:.2f}x"
    )
    print(f"Running {'forever' if args.steps == 0 else high_level_steps} high-level steps...")

    i = 0
    try:
        with torch.no_grad():
            while i < high_level_steps:
                obs = obs_td["policy"].clone()  # (1, 14) pre-step observation
                unnorm_obs = obs * obs_scale if env.normalize else obs

                target_z = env.target_z[0].item()
                actions = model(obs)
                obs_td, rew_buf, reset_buf, _ = env.step(actions, update_visualizer=not args.vis)

                reward_cur = rew_buf[0].item()
                ep_reward += reward_cur
                ep_len += 1

                if args.record:
                    env.cam.render()

                avg_force = _term_float(env.last_reward_terms.get("avg_force"))
                cub_rel_z = unnorm_obs[0, FrankaEnvStudent.OBS_CUBOID_REL_Z].item()
                des_rel_z = unnorm_obs[0, FrankaEnvStudent.OBS_DESIRED_REL_Z].item()

                bufs["steps"].append(ep_len)
                bufs["ee_z"].append(unnorm_obs[0, FrankaEnvStudent.OBS_EE_POS_Z].item())
                bufs["cuboid_rel_z"].append(cub_rel_z)
                bufs["desired_rel_z"].append(des_rel_z)
                bufs["ft_dist"].append(float(env.get_fingertip_distance()))
                bufs["avg_force"].append(avg_force)
                bufs["reward"].append(reward_cur)
                bufs["target_z"].append(target_z)
                bufs["actual_z_vel"].append(unnorm_obs[0, FrankaEnvStudent.OBS_EE_VEL_Z].item())
                bufs["target_z_vel"].append(unnorm_obs[0, FrankaEnvStudent.OBS_TARGET_Z_VEL].item())
                bufs["target_z_acc"].append(unnorm_obs[0, FrankaEnvStudent.OBS_TARGET_Z_ACC].item())

                currently_released = avg_force < force_thresh
                if currently_released and not in_release:
                    in_release, release_start = True, ep_len
                elif not currently_released and in_release:
                    release_spans.append((release_start, ep_len))
                    in_release = False

                if i % 100 == 0:
                    print(
                        f"step {env.sim_step:7d}  z={bufs['ee_z'][-1]:.4f}  "
                        f"z_vel={bufs['actual_z_vel'][-1]:+.3f}  avg_force={avg_force:.3f}  "
                        f"cuboid_rel_z={cub_rel_z:+.4f}  desired={des_rel_z:+.4f}  "
                        f"err={abs(cub_rel_z - des_rel_z) * 1000:.2f}mm  ep_rew={ep_reward:.1f}"
                    )

                if reset_buf[0].item():
                    if in_release:
                        release_spans.append((release_start, ep_len))
                        in_release = False
                    success = _term_float(env.last_reward_terms.get("success")) > 0.5
                    fail = _term_float(env.last_reward_terms.get("fail")) > 0.5
                    timeout = _term_float(env.last_reward_terms.get("timeout")) > 0.5
                    ep_count += 1
                    successes += int(success)
                    fails += int(fail)
                    timeouts += int(timeout)
                    outcome = "SUCCESS" if success else "fail" if fail else "timeout" if timeout else "done"
                    print(
                        f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  "
                        f"regrasps={len(release_spans)}  {outcome}  "
                        f"(totals: {successes} success / {fails} fail / {timeouts} timeout)"
                    )
                    if args.plot and bufs["steps"]:
                        _plot_student_episode(bufs, release_spans, ep_count,
                                              save_dir=os.path.join(out_dir, "plots"))
                    bufs = _fresh_demo_buffers()
                    release_spans = []
                    ep_reward, ep_len = 0.0, 0

                if args.vis and (i + 1) % args.render_every == 0:
                    t_sim = env.sim_step * env.dt / args.replay_speed
                    t_wall = time.perf_counter() - t_real_start
                    if t_wall < t_sim:
                        time.sleep(t_sim - t_wall)
                    env.scene.visualizer.update(force=False, auto=True)

                i += 1

    except KeyboardInterrupt:
        print("\nStopped by user.")
        if args.plot and bufs["steps"]:
            if in_release:
                release_spans.append((release_start, ep_len))
            ep_count += 1
            _plot_student_episode(bufs, release_spans, ep_count,
                                  save_dir=os.path.join(out_dir, "plots"))

    if args.record and env.cam is not None:
        env.cam.stop_recording(save_to_filename="franka_student_policy.mp4", fps=60)
        print("Saved franka_student_policy.mp4")

    if ep_count > 0:
        print(f"\nSummary: {ep_count} episodes - {successes} success / {fails} fail / {timeouts} timeout")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Teacher(2H, 16 obs) -> Student(14 obs) distillation for Franka lift")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true",
                      help="Roll out the 2H teacher and record (student_obs, action) samples")
    mode.add_argument("--train_student", action="store_true",
                      help="Behaviour-clone the student from the recorded samples")
    mode.add_argument("--dagger", action="store_true",
                      help="DAgger round: roll out the current student in the 2H env, relabel "
                           "visited states with teacher actions/values, aggregate into demo_data.pt")
    mode.add_argument("--student_demo", action="store_true",
                      help="Visualise the trained student in env_franka_parallel (14 obs)")

    parser.add_argument("-e", "--exp_name", type=str, default="franka-lift-v1",
                        help="Experiment name; teacher logs at logs/<exp_name>-2H-control")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--dt", type=float, default=0.001)
    parser.add_argument("--target_dt", type=float, default=0.02)
    parser.add_argument("--normalization", action=argparse.BooleanOptionalAction, default=True,
                        help="Fixed observation normalization (must match teacher training; default on)")
    parser.add_argument("--mix", action="store_true", default=False,
                        help="Randomly assign positive/negative desired_rel_z per env (demo / student_demo)")
    parser.add_argument("--zero", action="store_true", default=False,
                        help="Enable the env's post-pulse zero-z-velocity + close-gripper hold (demo / student_demo)")
    parser.add_argument("--control-error", action="store_true", default=False,
                        help="Per-episode constant z-velocity command bias (demo / student_demo)")

    # --demo options
    parser.add_argument("--ckpt", type=int, default=None,
                        help="Teacher checkpoint iteration (e.g. 3200). Defaults to latest.")
    parser.add_argument("--num_samples", type=int, default=100_000,
                        help="Number of (obs, action) samples to record")
    parser.add_argument("-B", "--num_envs", type=int, default=64,
                        help="Parallel envs for data collection (fewer envs = more full episodes per env)")
    parser.add_argument("--randomize", action=argparse.BooleanOptionalAction, default=None,
                        help="2H domain randomization. Default: on for --demo/--dagger, off for --student_demo.")

    # --dagger options
    parser.add_argument("--beta", type=float, default=0.0,
                        help="DAgger mixing: probability of executing the teacher's action instead of "
                             "the student's during rollout (0 = pure student, classic after round 1)")

    # --train_student options
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--batch_size", type=int, default=4096)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--val_fraction", type=float, default=0.05)
    parser.add_argument("--loss", choices=("kl", "mse"), default="kl",
                        help="Distillation loss: 'kl' (Gaussian KL to teacher; learns exploration std, "
                             "recommended for PPO fine-tuning) or 'mse' (mean regression only)")
    parser.add_argument("--value_coef", type=float, default=1.0,
                        help="Weight of the critic value-distillation loss")

    # --student_demo options
    parser.add_argument("--no-vis", dest="vis", action="store_false", default=True,
                        help="Disable interactive viewer (headless)")
    parser.add_argument("--record", action="store_true", default=False,
                        help="Record offscreen video to franka_student_policy.mp4")
    parser.add_argument("--steps", type=int, default=10000,
                        help="Total high-level steps for --student_demo (0 = until Ctrl-C)")
    parser.add_argument("--plot", action="store_true", default=False,
                        help="Save per-episode diagnostic plots (student_demo)")
    parser.add_argument("--replay-speed", type=float, default=4.0)
    parser.add_argument("--render-every", type=int, default=1)

    args = parser.parse_args()

    needs_teacher = args.demo or args.dagger
    try:
        if int(metadata.version("rsl-rl-lib").split(".")[0]) < 5 and needs_teacher:
            raise ImportError
    except (metadata.PackageNotFoundError, ValueError):
        if needs_teacher:
            raise ImportError("Please install 'rsl-rl-lib>=5.0.0' (needed to load the teacher).")

    if args.record:
        args.vis = False
    if args.randomize is None:
        args.randomize = needs_teacher  # full 2H randomization for data generation

    _check_obs_compatibility()

    if args.demo:
        run_demo(args)
    elif args.dagger:
        run_dagger(args)
    elif args.train_student:
        run_train_student(args)
    else:
        run_student_demo(args)


if __name__ == "__main__":
    main()
