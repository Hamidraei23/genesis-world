"""
AXIS-SAC training for the Genesis deformable-sheet task (DeformSheetTaskEnv).

Ported from the MuJoCo reference trainer
``~/workspaces/ros_two/mujoco_ws/sac_mod_adp_plus.py``: vanilla SAC plus its "AXIS"
variant with **no lambda tuning** on the critic (`lambda_tuning=False` there), i.e.

  AXIS critic : the twin target-Q backup blends min and mean,
                    q_tgt = (1 - lambda_tgt) * min(Q1t, Q2t) + lambda_tgt * mean(Q1t, Q2t),
                where lambda_tgt is *correlation-driven* (not learned): the Pearson
                correlation of Q1t/Q2t over the batch, mapped from [0.965, 0.998] to
                [lambda_tgt_min, lambda_tgt_max]. The entropy term uses a state-dependent
                alpha_next = alpha0 * (1 + uncertainty_scale * u_hat), with u_hat a
                sigmoid(u_k * z)-normalized twin-Q disagreement.
  AXIS actor  : the policy objective replaces min(Q1, Q2) with a UCB aggregate,
                    q_agg = (1 - lambda_pi) * q_mean + lambda_pi * (q_mean + ucb_beta * q_std),
                where lambda_pi = sigmoid(raw_lambda_pi) is LEARNED (dedicated Adam,
                gradients from the actor loss), and the entropy weight is the same
                state-dependent alpha_s = alpha0 * (1 + uncertainty_scale * u_hat_s).

This is NOT the OAC path (`args.oac` in the reference) -- optimistic exploration via
Q-upper-bound mean shift is a different method and is deliberately not reproduced.

The critic side and actor side can each be vanilla or AXIS independently (--mode):

  axis-actor   AXIS actor  + vanilla critic
  axis-full    AXIS actor  + AXIS critic
  axis-critic  vanilla actor + AXIS critic
  vanilla      vanilla SAC (both sides vanilla)

Like the reference, every mode starts as vanilla SAC with entropy autotuning; once
--axis_start env transitions have been collected (reference: 8000 -- scale it up when
running many envs) the chosen AXIS component(s) come online and alpha freezes at its
last autotuned value. Pass --axis_start 0 to run AXIS from the first update.

Environments
------------
--num_envs 1 (default) uses the single-scene DeformSheetTaskEnv in training mode
(each episode starts from a random saved corner checkpoint, top_left/top_right).
--num_envs B > 1 uses DeformSheetTaskEnvParallel: ONE batched Genesis scene stepping
all B envs on the GPU per macro-action, with the same per-env alternating corner
starts. Each collection round adds B transitions to the buffer and then runs
--updates_per_round gradient updates (off-policy replay ratio knob).

Usage (run inside the genesis container):
    # single env, reference-style
    docker exec genesis python /workspace/examples/deformable/train_deform_axis_sac.py \
        --mode axis-full --desired_state 1

    # 1000 batched envs, ~1 hour budget (see measured SPS in logs)
    docker exec genesis python /workspace/examples/deformable/train_deform_axis_sac.py \
        --mode axis-full --desired_state 1 --num_envs 1000 \
        --total_timesteps 2000000 --buffer_size 1000000 \
        --learning_starts 50000 --axis_start 250000 --updates_per_round 32

--cpu runs both the Genesis physics and the networks on CPU; the default uses the GPU
backend for physics and CUDA for the networks.
"""

import argparse
import csv
import os
import random
import time
from types import SimpleNamespace

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

try:
    from .deform_sheet_env import DeformSheetTaskEnv
    from .deform_sheet_env_parallel import DeformSheetTaskEnvParallel
except ImportError:
    from deform_sheet_env import DeformSheetTaskEnv
    from deform_sheet_env_parallel import DeformSheetTaskEnvParallel

LOG_STD_MAX = 2
LOG_STD_MIN = -5


# ----------------- Networks (ported from the reference) -----------------


class SoftQNetwork(nn.Module):
    def __init__(self, obs_dim: int, act_dim: int):
        super().__init__()
        self.fc1 = nn.Linear(obs_dim + act_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)

    def forward(self, x, a):
        x = torch.cat([x, a], 1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.fc3(x)


class Actor(nn.Module):
    def __init__(self, obs_dim: int, act_dim: int):
        super().__init__()
        self.fc1 = nn.Linear(obs_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc_mean = nn.Linear(256, act_dim)
        self.fc_logstd = nn.Linear(256, act_dim)
        # DeformSheetTaskEnv actions live in [-1, 1]^6 -> scale 1, bias 0 (kept as buffers
        # so the checkpoint format matches the reference Actor).
        self.register_buffer("action_scale", torch.ones(act_dim, dtype=torch.float32))
        self.register_buffer("action_bias", torch.zeros(act_dim, dtype=torch.float32))

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        mean = self.fc_mean(x)
        log_std = torch.tanh(self.fc_logstd(x))
        log_std = LOG_STD_MIN + 0.5 * (LOG_STD_MAX - LOG_STD_MIN) * (log_std + 1)
        return mean, log_std

    def get_action(self, x):
        mean, log_std = self(x)
        std = log_std.exp()
        normal = torch.distributions.Normal(mean, std)
        x_t = normal.rsample()
        y_t = torch.tanh(x_t)
        action = y_t * self.action_scale + self.action_bias
        log_prob = normal.log_prob(x_t)
        log_prob -= torch.log(self.action_scale * (1 - y_t.pow(2)) + 1e-6)
        log_prob = log_prob.sum(1, keepdim=True)
        deterministic_action = torch.tanh(mean) * self.action_scale + self.action_bias
        return action, log_prob, deterministic_action


# ----------------- Replay buffer (self-contained; no SB3/gym dependency) -----------------


class ReplayBuffer:
    def __init__(self, capacity: int, obs_dim: int, act_dim: int, device: torch.device):
        self.capacity = capacity
        self.device = device
        self.obs = np.zeros((capacity, obs_dim), dtype=np.float32)
        self.next_obs = np.zeros((capacity, obs_dim), dtype=np.float32)
        self.actions = np.zeros((capacity, act_dim), dtype=np.float32)
        self.rewards = np.zeros((capacity, 1), dtype=np.float32)
        self.dones = np.zeros((capacity, 1), dtype=np.float32)
        self.pos = 0
        self.full = False

    def add(self, obs, next_obs, action, reward, done):
        self.obs[self.pos] = obs
        self.next_obs[self.pos] = next_obs
        self.actions[self.pos] = action
        self.rewards[self.pos] = reward
        self.dones[self.pos] = done
        self.pos = (self.pos + 1) % self.capacity
        if self.pos == 0:
            self.full = True

    def add_batch(self, obs, next_obs, actions, rewards, dones):
        n = obs.shape[0]
        idx = (self.pos + np.arange(n)) % self.capacity
        self.obs[idx] = obs
        self.next_obs[idx] = next_obs
        self.actions[idx] = actions
        self.rewards[idx] = rewards.reshape(-1, 1)
        self.dones[idx] = dones.reshape(-1, 1)
        if self.pos + n >= self.capacity:
            self.full = True
        self.pos = (self.pos + n) % self.capacity

    def __len__(self):
        return self.capacity if self.full else self.pos

    def sample(self, batch_size: int) -> SimpleNamespace:
        idx = np.random.randint(0, len(self), size=batch_size)
        to = lambda a: torch.as_tensor(a[idx], device=self.device)
        return SimpleNamespace(
            observations=to(self.obs),
            next_observations=to(self.next_obs),
            actions=to(self.actions),
            rewards=to(self.rewards),
            dones=to(self.dones),
        )


# ----------------- Helpers -----------------


def pearson_corr(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    a_c = a - a.mean()
    b_c = b - b.mean()
    return (a_c * b_c).mean() / (a_c.std(unbiased=False) * b_c.std(unbiased=False) + 1e-8)


def u_hat_from_disagreement(q_std: torch.Tensor, u_k: float) -> torch.Tensor:
    """Reference disagreement normalization: z-score across the batch, squashed by
    sigmoid(u_k * z), clamped to [0, 1]."""
    z = (q_std - q_std.mean()) / (q_std.std() + 1e-8)
    return torch.sigmoid(u_k * z).clamp(0.0, 1.0)


def env_done(termination, truncated) -> bool:
    return bool(np.asarray(termination).any()) or bool(np.asarray(truncated).any())


# ----------------- Learner: vanilla / AXIS updates for all 4 modes -----------------


class SACLearner:
    """Holds networks/optimizers and performs one reference-style update per call:
    a critic step every call, plus (every policy_frequency-th call) policy_frequency
    actor steps and the alpha autotune step -- exactly the reference's cadence."""

    def __init__(self, obs_dim: int, act_dim: int, args, device: torch.device):
        self.args = args
        self.device = device
        self.actor = Actor(obs_dim, act_dim).to(device)
        self.qf1 = SoftQNetwork(obs_dim, act_dim).to(device)
        self.qf2 = SoftQNetwork(obs_dim, act_dim).to(device)
        self.qf1_target = SoftQNetwork(obs_dim, act_dim).to(device)
        self.qf2_target = SoftQNetwork(obs_dim, act_dim).to(device)
        self.qf1_target.load_state_dict(self.qf1.state_dict())
        self.qf2_target.load_state_dict(self.qf2.state_dict())

        # AXIS actor aggregator: lambda_pi = sigmoid(raw_lambda_pi), learned from the actor
        # loss. (With no_lambda_tuning the critic-side lambda_tgt is corr-driven, so there
        # is no raw_lambda_tgt parameter at all.)
        self.raw_lambda_pi = torch.zeros(1, device=device, requires_grad=True)

        self.q_optimizer = optim.Adam(list(self.qf1.parameters()) + list(self.qf2.parameters()), lr=args.q_lr)
        self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=args.policy_lr)
        self.agg_optimizer = optim.Adam([self.raw_lambda_pi], lr=args.q_lr)

        self.target_entropy = -float(act_dim)
        self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
        self.a_optimizer = optim.Adam([self.log_alpha], lr=args.q_lr)
        self.alpha = self.log_alpha.exp().item() if args.autotune else args.alpha

        self.update_count = 0

    @torch.no_grad()
    def act(self, obs: torch.Tensor, deterministic: bool = False) -> torch.Tensor:
        stoch, _, det = self.actor.get_action(obs)
        return (det if deterministic else stoch).clamp(-1.0, 1.0)

    def update(self, rb: ReplayBuffer, actor_axis_on: bool, critic_axis_on: bool, autotune_active: bool) -> dict:
        args = self.args
        data = rb.sample(args.batch_size)
        self.update_count += 1

        alpha0 = self.log_alpha.exp().detach() if autotune_active else torch.as_tensor(self.alpha, device=self.device)

        # ---------- Critic target ----------
        with torch.no_grad():
            next_actions, next_log_pi, _ = self.actor.get_action(data.next_observations)
            qf1_next_target = self.qf1_target(data.next_observations, next_actions)
            qf2_next_target = self.qf2_target(data.next_observations, next_actions)

            if not critic_axis_on:
                # Vanilla SAC: min backup, constant alpha
                min_q_next = torch.min(qf1_next_target, qf2_next_target)
                soft_value_next = (min_q_next - alpha0 * next_log_pi).view(-1)
                lambda_tgt = None
            else:
                # AXIS critic: corr-driven min/mean blend + disagreement-scaled alpha
                q_next_mean = 0.5 * (qf1_next_target + qf2_next_target)
                q_next_min = torch.min(qf1_next_target, qf2_next_target)
                q_next_std = (qf1_next_target - qf2_next_target).abs() / np.sqrt(2.0)

                u_hat_next = u_hat_from_disagreement(q_next_std, args.u_k)
                alpha_next = alpha0 * (1.0 + args.uncertainty_scale * u_hat_next)
                entropy_next = (alpha_next * next_log_pi).view(-1)

                corr_tgt = pearson_corr(qf1_next_target, qf2_next_target)
                corr_min, corr_max = 0.965, 0.998
                t = ((corr_tgt - corr_min) / (corr_max - corr_min)).clamp(0.0, 1.0)
                lambda_tgt = args.lambda_tgt_min + (args.lambda_tgt_max - args.lambda_tgt_min) * t

                q_next_tgt = (1.0 - lambda_tgt) * q_next_min + lambda_tgt * q_next_mean
                soft_value_next = q_next_tgt.view(-1) - entropy_next

            rewards_b = data.rewards.flatten()
            dones_b = data.dones.flatten()
            next_q_value = rewards_b + (1.0 - dones_b) * args.gamma * soft_value_next

        # ---------- Critic update ----------
        qf1_a_values = self.qf1(data.observations, data.actions).view(-1)
        qf2_a_values = self.qf2(data.observations, data.actions).view(-1)
        qf1_loss = F.mse_loss(qf1_a_values, next_q_value)
        qf2_loss = F.mse_loss(qf2_a_values, next_q_value)
        qf_loss = qf1_loss + qf2_loss

        self.q_optimizer.zero_grad()
        qf_loss.backward()
        self.q_optimizer.step()

        diag = {
            "losses/qf1_loss": qf1_loss.item(),
            "losses/qf2_loss": qf2_loss.item(),
            "losses/qf_loss": qf_loss.item() / 2.0,
            "q/target_mean": next_q_value.mean().item(),
            "q/q1_q2_corr": pearson_corr(qf1_a_values.detach(), qf2_a_values.detach()).item(),
            "q/reward_mean_batch": rewards_b.mean().item(),
            "losses/alpha": self.alpha if autotune_active else float(alpha0),
        }
        if lambda_tgt is not None:
            diag["axis/lambda_tgt"] = float(lambda_tgt)

        # ---------- Actor update (delayed, reference cadence) ----------
        if self.update_count % args.policy_frequency == 0:
            for _ in range(args.policy_frequency):
                pi, log_pi, _ = self.actor.get_action(data.observations)
                qf1_pi = self.qf1(data.observations, pi)
                qf2_pi = self.qf2(data.observations, pi)

                if not actor_axis_on:
                    min_q_pi = torch.min(qf1_pi, qf2_pi)
                    actor_loss = (alpha0 * log_pi - min_q_pi).mean()
                    diag["alpha/mean_used"] = float(alpha0)
                else:
                    # AXIS actor: disagreement-scaled alpha + learned UCB mixing
                    q_mean_pi = 0.5 * (qf1_pi + qf2_pi)
                    q_std_pi = (qf1_pi - qf2_pi).abs() / np.sqrt(2.0)

                    u_hat_s = u_hat_from_disagreement(q_std_pi.detach(), args.u_k)
                    alpha_s = alpha0 * (1.0 + args.uncertainty_scale * u_hat_s)
                    diag["alpha/mean_used"] = alpha_s.mean().item()

                    lambda_pi = torch.sigmoid(self.raw_lambda_pi)
                    q_pi_ucb = q_mean_pi + args.ucb_beta * q_std_pi
                    q_pi_agg = (1.0 - lambda_pi) * q_mean_pi + lambda_pi * q_pi_ucb

                    actor_loss = (alpha_s * log_pi - q_pi_agg).mean()
                    diag["axis/lambda_pi"] = torch.sigmoid(self.raw_lambda_pi).item()

                self.actor_optimizer.zero_grad()
                if actor_axis_on:
                    self.agg_optimizer.zero_grad()
                actor_loss.backward()
                self.actor_optimizer.step()
                if actor_axis_on:
                    self.agg_optimizer.step()
                diag["losses/actor_loss"] = actor_loss.item()

                # ---------- Alpha autotune (frozen once AXIS is online, like the reference) ----------
                if autotune_active:
                    with torch.no_grad():
                        _, log_pi_for_alpha, _ = self.actor.get_action(data.observations)
                    alpha_loss = (-self.log_alpha.exp() * (log_pi_for_alpha + self.target_entropy)).mean()
                    self.a_optimizer.zero_grad()
                    alpha_loss.backward()
                    self.a_optimizer.step()
                    self.alpha = self.log_alpha.exp().item()

        # ---------- Target network updates ----------
        if self.update_count % args.target_network_frequency == 0:
            with torch.no_grad():
                for param, target_param in zip(self.qf1.parameters(), self.qf1_target.parameters()):
                    target_param.data.mul_(1 - args.tau)
                    target_param.data.add_(args.tau * param.data)
                for param, target_param in zip(self.qf2.parameters(), self.qf2_target.parameters()):
                    target_param.data.mul_(1 - args.tau)
                    target_param.data.add_(args.tau * param.data)

        return diag

    def save(self, save_dir: str, tag):
        torch.save(self.actor.state_dict(), os.path.join(save_dir, f"actor_{tag}.pth"))
        torch.save(self.qf1.state_dict(), os.path.join(save_dir, f"qf1_{tag}.pth"))
        torch.save(self.qf2.state_dict(), os.path.join(save_dir, f"qf2_{tag}.pth"))


# ----------------- Args -----------------


def parse_args():
    p = argparse.ArgumentParser(description="AXIS-SAC training for DeformSheetTaskEnv (Genesis)")
    p.add_argument("--mode", type=str, default="axis-full",
                   choices=["axis-actor", "axis-full", "axis-critic", "vanilla"],
                   help="Which sides run AXIS vs vanilla: axis-actor = AXIS actor + vanilla critic; "
                        "axis-full = both AXIS; axis-critic = vanilla actor + AXIS critic; "
                        "vanilla = plain SAC.")
    p.add_argument("--desired_state", type=int, default=2, choices=[0, 1, 2],
                   help="Target sheet shape the reward optimizes (0 flat / 1 diagonal fold / 2 edge fold).")
    p.add_argument("--cpu", action="store_true", default=False,
                   help="Run Genesis physics AND the networks on CPU (default: GPU physics + CUDA networks).")
    p.add_argument("--num_envs", type=int, default=1,
                   help="1 = single-scene env; >1 = ONE batched Genesis scene stepping all envs together "
                        "(DeformSheetTaskEnvParallel).")
    p.add_argument("--updates_per_round", type=int, default=32,
                   help="Batched path only: gradient updates per collection round (replay-ratio knob).")
    p.add_argument("--vis", action="store_true", default=False, help="Viewer (env 0 / batched scene).")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--exp_name", type=str, default="deform_axis_sac")

    # SAC hyperparameters -- defaults follow the MuJoCo reference (sac_mod_adp_plus.py Args)
    p.add_argument("--total_timesteps", type=int, default=200000,
                   help="Total env transitions to collect (across all envs).")
    p.add_argument("--buffer_size", type=int, default=100000)
    p.add_argument("--learning_starts", type=int, default=6000,
                   help="Env transitions collected before updates begin. Scale with --num_envs.")
    p.add_argument("--batch_size", type=int, default=256)
    p.add_argument("--gamma", type=float, default=0.99)
    p.add_argument("--tau", type=float, default=0.005)
    p.add_argument("--policy_lr", type=float, default=0.8e-3)
    p.add_argument("--q_lr", type=float, default=1.6e-3)
    p.add_argument("--policy_frequency", type=int, default=2)
    p.add_argument("--target_network_frequency", type=int, default=1)
    p.add_argument("--alpha", type=float, default=0.2,
                   help="Entropy coefficient used when autotune is off (and after AXIS freezes it).")
    p.add_argument("--no-autotune", dest="autotune", action="store_false", default=True,
                   help="Disable automatic entropy tuning (reference default: on).")

    # AXIS hyperparameters (reference defaults)
    p.add_argument("--axis_start", type=int, default=8000,
                   help="Env transitions collected before the AXIS component(s) come online (reference: "
                        "8000 at 1 env); alpha autotuning freezes there. Scale with --num_envs. "
                        "0 = AXIS from the first update. Ignored for --mode vanilla.")
    p.add_argument("--uncertainty_scale", type=float, default=0.0,
                   help="k in alpha(s) = alpha0 * (1 + k * u_hat(s)).")
    p.add_argument("--ucb_beta", type=float, default=0.5,
                   help="beta in the actor's UCB aggregate q_mean + beta * q_std.")
    p.add_argument("--u_k", type=float, default=2.0,
                   help="Slope of the sigmoid disagreement normalization u_hat = sigmoid(u_k * z).")
    p.add_argument("--lambda_tgt_min", type=float, default=0.05)
    p.add_argument("--lambda_tgt_max", type=float, default=0.95)

    # Logging / eval / checkpoints (in env transitions)
    p.add_argument("--save_every", type=int, default=2000)
    p.add_argument("--eval_every", type=int, default=2000)
    p.add_argument("--eval_episodes", type=int, default=1, help="Single-env path only.")
    p.add_argument("--log_every", type=int, default=1000)
    p.add_argument("--heartbeat_secs", type=float, default=30.0,
                   help="Print periodic progress heartbeat in batched mode every N seconds.")
    return p.parse_args()


# ----------------- Single-env loop (reference-style, 1 update per env step) -----------------


def run_single_env(args, learner: SACLearner, rb: ReplayBuffer, save_dir: str, log_scalar, device):
    env = DeformSheetTaskEnv(training=True, desired_state=args.desired_state, vis=args.vis, cpu=args.cpu)
    obs, _ = env.reset(desired_state=args.desired_state)

    eval_csv_path = os.path.join(save_dir, "eval_returns.csv")
    start_time = time.time()
    episode_count = 0
    axis_announced = False
    actor_axis = args.mode in ("axis-actor", "axis-full")
    critic_axis = args.mode in ("axis-critic", "axis-full")

    for global_step in range(args.total_timesteps):
        axis_on = args.mode != "vanilla" and global_step >= args.axis_start
        if axis_on and not axis_announced:
            print(f"[step {global_step}] AXIS coming online (mode={args.mode}); alpha frozen at {learner.alpha:.4f}")
            axis_announced = True
        autotune_active = args.autotune and not axis_on

        x = torch.as_tensor(obs, dtype=torch.float32, device=device).unsqueeze(0)
        action = learner.act(x)[0].cpu().numpy()

        next_obs, reward, termination, truncated, infos = env.step(action)
        done = env_done(termination, truncated)
        rb.add(obs, next_obs, action, float(reward), float(done))
        obs = next_obs

        if done:
            episode_count += 1
            ep_ret = float(infos["episodic_reward"])
            success = float(env.success_score)
            log_scalar("charts/episodic_return", ep_ret, global_step)
            log_scalar("charts/success_score", success, global_step)
            if episode_count % 20 == 0:
                corner = os.path.basename(str(env._checkpoint_path))
                print(f"[step {global_step}] episode {episode_count} ({corner}): "
                      f"return={ep_ret:.1f} success_score={success:.1f}")
            obs, _ = env.reset(desired_state=args.desired_state)

        if global_step > args.learning_starts:
            diag = learner.update(rb, actor_axis and axis_on, critic_axis and axis_on, autotune_active)

            if global_step % args.log_every == 0:
                for k, v in diag.items():
                    log_scalar(k, v, global_step)
                sps = int(global_step / (time.time() - start_time + 1e-9))
                log_scalar("charts/SPS", sps, global_step)
                print(f"[step {global_step}] qf_loss={diag['losses/qf_loss']:.3f} "
                      f"alpha={diag['losses/alpha']:.4f} corr={diag['q/q1_q2_corr']:.4f} SPS={sps}")

            if global_step % args.save_every == 0:
                learner.save(save_dir, global_step // args.save_every)

            if global_step % args.eval_every == 0:
                returns, successes = [], []
                for _ in range(args.eval_episodes):
                    e_obs, _ = env.reset(desired_state=args.desired_state)
                    e_done, e_infos = False, {}
                    while not e_done:
                        xe = torch.as_tensor(e_obs, dtype=torch.float32, device=device).unsqueeze(0)
                        e_act = learner.act(xe, deterministic=True)[0].cpu().numpy()
                        e_obs, _, e_term, e_trunc, e_infos = env.step(e_act)
                        e_done = env_done(e_term, e_trunc)
                    returns.append(float(e_infos["episodic_reward"]))
                    successes.append(float(env.success_score))
                _log_eval(eval_csv_path, log_scalar, global_step, returns, successes)
                obs, _ = env.reset(desired_state=args.desired_state)

    learner.save(save_dir, "final")


# ----------------- Batched loop (one round = num_envs transitions) -----------------


def run_parallel(args, learner: SACLearner, rb: ReplayBuffer, save_dir: str, log_scalar, device):
    print("building batched Genesis scene (first-time GPU kernel compilation may take a while)...", flush=True)
    env = DeformSheetTaskEnvParallel(
        num_envs=args.num_envs, desired_state=args.desired_state, vis=args.vis, cpu=args.cpu
    )
    print("scene built; resetting envs...", flush=True)
    obs = env.reset()  # (B, 19) torch on gs.device
    print("reset done; starting collection loop.", flush=True)

    eval_csv_path = os.path.join(save_dir, "eval_returns.csv")
    start_time = time.time()
    episode_count = 0
    axis_announced = False
    actor_axis = args.mode in ("axis-actor", "axis-full")
    critic_axis = args.mode in ("axis-critic", "axis-full")
    B = args.num_envs
    rounds = args.total_timesteps // B
    next_log = next_save = next_eval = 0
    last_heartbeat = time.time()

    print(f"batched collection: {rounds} rounds x {B} envs = {rounds * B} transitions, "
          f"{args.updates_per_round} updates/round", flush=True)

    for r in range(rounds):
        samples = r * B
        axis_on = args.mode != "vanilla" and samples >= args.axis_start
        if axis_on and not axis_announced:
            print(f"[{samples} samples] AXIS coming online (mode={args.mode}); alpha frozen at {learner.alpha:.4f}",
                  flush=True)
            axis_announced = True
        autotune_active = args.autotune and not axis_on

        if time.time() - last_heartbeat >= args.heartbeat_secs:
            phase = "updating" if samples > args.learning_starts else "collecting"
            print(f"[{samples} samples | round {r}/{rounds}] heartbeat phase={phase} "
                  f"rb={len(rb)} episodes={episode_count}", flush=True)
            last_heartbeat = time.time()

        if r == 0:
            print("[round 0] stepping envs for the first time (GPU kernels compile here, please wait)...", flush=True)
        _round_t0 = time.time()
        actions = learner.act(obs.to(device))
        next_obs, reward, termination, truncated, infos = env.step(actions.to(env.device))
        done = (termination | truncated).float()
        if r < 3:
            print(f"[round {r}/{rounds}] step done in {time.time() - _round_t0:.2f}s "
                  f"(samples={samples}, rb={len(rb)})", flush=True)
            last_heartbeat = time.time()

        rb.add_batch(
            obs.cpu().numpy(),
            next_obs.cpu().numpy(),
            actions.cpu().numpy(),
            reward.cpu().numpy(),
            done.cpu().numpy(),
        )
        obs = next_obs

        done_mask = done.bool()
        if done_mask.any():
            n_done = int(done_mask.sum())
            episode_count += n_done
            ep_ret = infos["episodic_reward"][done_mask].mean().item()
            success = infos["success_score"][done_mask].mean().item()
            log_scalar("charts/episodic_return", ep_ret, samples)
            log_scalar("charts/success_score", success, samples)

        if samples > args.learning_starts:
            diag = {}
            for _ in range(args.updates_per_round):
                diag = learner.update(rb, actor_axis and axis_on, critic_axis and axis_on, autotune_active)

            if samples >= next_log:
                next_log = samples + args.log_every
                for k, v in diag.items():
                    log_scalar(k, v, samples)
                sps = int(samples / (time.time() - start_time + 1e-9))
                log_scalar("charts/SPS", sps, samples)
                print(f"[{samples} samples | round {r}/{rounds}] qf_loss={diag['losses/qf_loss']:.3f} "
                      f"alpha={diag['losses/alpha']:.4f} corr={diag['q/q1_q2_corr']:.4f} "
                      f"episodes={episode_count} SPS={sps}", flush=True)

            if samples >= next_save:
                next_save = samples + args.save_every
                learner.save(save_dir, samples)

            if samples >= next_eval:
                next_eval = samples + args.eval_every
                obs = _eval_parallel(env, learner, device, eval_csv_path, log_scalar, samples)

    learner.save(save_dir, "final")


def _eval_parallel(env: DeformSheetTaskEnvParallel, learner: SACLearner, device, csv_path, log_scalar, samples):
    """Deterministic eval on the whole batch: reset all envs, run until each has finished
    one episode, record its first-episode return/success. Returns fresh training obs."""
    obs = env.reset()
    B = env.num_envs
    finished = torch.zeros(B, dtype=torch.bool, device=env.device)
    returns = torch.zeros(B, device=env.device)
    successes = torch.zeros(B, device=env.device)
    for _ in range(env.max_steps + 1):
        actions = learner.act(obs.to(device), deterministic=True)
        obs, _, term, trunc, infos = env.step(actions.to(env.device))
        newly = (term | trunc) & ~finished
        returns[newly] = infos["episodic_reward"][newly]
        successes[newly] = infos["success_score"][newly]
        finished |= newly
        if finished.all():
            break
    _log_eval(csv_path, log_scalar, samples, returns[finished].cpu().tolist(), successes[finished].cpu().tolist())
    return env.reset()


def _log_eval(csv_path, log_scalar, step, returns, successes):
    mean_return = float(np.mean(returns))
    mean_success = float(np.mean(successes))
    log_scalar("evaluation/mean_return", mean_return, step)
    log_scalar("evaluation/mean_success_score", mean_success, step)
    write_header = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["global_step", "mean_return", "mean_success_score"])
        w.writerow([step, mean_return, mean_success])
    print(f"[{step} samples] EVAL mean_return={mean_return:.1f} mean_success_score={mean_success:.1f}", flush=True)


# ----------------- Main -----------------


def main():
    args = parse_args()
    actor_axis = args.mode in ("axis-actor", "axis-full")
    critic_axis = args.mode in ("axis-critic", "axis-full")

    run_name = f"{args.exp_name}__{args.mode}__state{args.desired_state}__seed{args.seed}__{int(time.time())}"
    save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", run_name)
    os.makedirs(save_dir, exist_ok=True)

    try:
        from torch.utils.tensorboard import SummaryWriter
        writer = SummaryWriter(os.path.join(save_dir, "tb"))
    except ImportError:
        writer = None
        print("tensorboard not available -- falling back to stdout/CSV logging only")

    def log_scalar(tag, value, step):
        if writer is not None:
            writer.add_scalar(tag, value, step)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True

    device = torch.device("cuda" if torch.cuda.is_available() and not args.cpu else "cpu")

    obs_dim = DeformSheetTaskEnv.OBS_DIM
    act_dim = 6
    learner = SACLearner(obs_dim, act_dim, args, device)
    rb = ReplayBuffer(args.buffer_size, obs_dim, act_dim, device)

    print("=" * 78)
    print(f"AXIS-SAC on DeformSheetTaskEnv  --  mode={args.mode}  "
          f"(actor={'AXIS' if actor_axis else 'vanilla'}, critic={'AXIS' if critic_axis else 'vanilla'})")
    print(f"  desired_state={args.desired_state}  num_envs={args.num_envs}  device={device}  "
          f"genesis_backend={'cpu' if args.cpu else 'gpu'}")
    if args.mode != "vanilla":
        print(f"  AXIS online at {args.axis_start} collected transitions (alpha freezes there); "
              f"ucb_beta={args.ucb_beta} u_k={args.u_k} uncertainty_scale={args.uncertainty_scale} "
              f"lambda_tgt in [{args.lambda_tgt_min}, {args.lambda_tgt_max}] (corr-driven, no tuning)")
    print(f"  logs/checkpoints -> {save_dir}")
    print("=" * 78)

    if args.num_envs > 1:
        run_parallel(args, learner, rb, save_dir, log_scalar, device)
    else:
        run_single_env(args, learner, rb, save_dir, log_scalar, device)

    if writer is not None:
        writer.close()
    print(f"done -- final weights and logs in {save_dir}")


if __name__ == "__main__":
    main()
