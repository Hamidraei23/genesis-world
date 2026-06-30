"""
Gymnasium wrapper for SAC training on FrankaMuJoCoEnv.

The wrapped MuJoCo environment keeps the same observation/action layout as the
Genesis PPO environment:
  obs:    (10,) float32
  action: (3,) in [-1, 1], [target_z_vel, finger_l, finger_r]
"""

from __future__ import annotations

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError as e:
    raise ImportError("Please install gymnasium to train the MuJoCo SAC environment.") from e

try:
    from .env_franka_mujoco import FrankaMuJoCoEnv
except ImportError:
    from env_franka_mujoco import FrankaMuJoCoEnv


class FrankaMuJoCoSACEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(
        self,
        *,
        dt: float = 0.001,
        target_dt: float = 0.02,
        limit_regrasp: bool = False,
        solid_up: bool = False,
        mix: bool = False,
        randomize: bool = False,
        normalize: bool = False,
        seed: int | None = None,
    ):
        super().__init__()
        self._seed = seed
        if seed is not None:
            np.random.seed(seed)

        self.env = FrankaMuJoCoEnv(
            vis=False,
            dt=dt,
            target_dt=target_dt,
            limit_regrasp=limit_regrasp,
            solid_up=solid_up,
            mix=mix,
            randomize=randomize,
            normalize=normalize,
        )

        obs_high = np.full((FrankaMuJoCoEnv.OBS_DIM,), np.inf, dtype=np.float32)
        self.observation_space = spaces.Box(-obs_high, obs_high, dtype=np.float32)
        self.action_space = spaces.Box(-1.0, 1.0, shape=(self.env.action_dim,), dtype=np.float32)
        if seed is not None:
            self.action_space.seed(seed)

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        if seed is not None:
            self._seed = seed
            np.random.seed(seed)
            self.action_space.seed(seed)
        obs = self.env.reset()
        return self.env.get_obs_flat().astype(np.float32), {}

    def step(self, action):
        action = np.asarray(action, dtype=np.float32)
        _, reward, done = self.env.step(action, auto_reset=False)
        obs = self.env.get_obs_flat().astype(np.float32)

        done_reason = self.env.last_done_reason
        truncated = bool(done and done_reason == "timeout")
        terminated = bool(done and not truncated)
        info = {
            "done_reason": done_reason,
            "is_success": done_reason == "success",
            "reward_terms": dict(self.env.last_reward_terms),
            "sim_step": self.env.sim_step,
            "episode_step": self.env.episode_step,
        }
        if done:
            info["terminal_observation"] = obs.copy()

        return obs, float(reward), terminated, truncated, info

    def close(self):
        self.env.close()
