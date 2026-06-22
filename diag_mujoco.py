"""Quick diagnostic: print every step's obs (unnormalized) for MuJoCo rollout."""
import os, pickle, numpy as np
from importlib import metadata

try:
    import torch
    from tensordict import TensorDict
    from rsl_rl.runners import OnPolicyRunner
except ImportError as e:
    raise ImportError("Need torch, tensordict, rsl-rl-lib>=5.0.0") from e

from examples.rigid.env_franka_mujoco import FrankaMuJoCoEnv

class _Proxy:
    num_envs = 1; num_actions = 3; extras = {}; cfg = {}
    def __init__(self):
        self.obs = torch.zeros(1, 10)
    def get_observations(self):
        return TensorDict({"policy": self.obs}, batch_size=[1])
    def reset(self):
        return self.get_observations()
    def step(self, _):
        return self.get_observations(), torch.zeros(1), torch.zeros(1, dtype=torch.bool), {}

log_dir = "logs/franka-lift-v1"
ckpt = os.path.join(log_dir, "model_240.pt")
with open(os.path.join(log_dir, "train_cfg.pkl"), "rb") as f:
    train_cfg = pickle.load(f)

env = FrankaMuJoCoEnv(vis=False, normalize=True)
runner = OnPolicyRunner(_Proxy(), train_cfg, log_dir, device=torch.device("cpu"))
runner.load(ckpt)
policy = runner.get_inference_policy(device=torch.device("cpu"))

obs_flat = env.get_obs_flat()
obs_td = TensorDict({"policy": torch.tensor(obs_flat, dtype=torch.float32).unsqueeze(0)}, batch_size=[1])

# Also get unnormalized for comparison
raw_obs = env.get_observation()
print(f"STEP 0 (raw dict): ee_pos={raw_obs['ee_pos']:.4f}  ee_vel={raw_obs['ee_vel']:.6f}  "
      f"lf={raw_obs['left_force_mag']:.4f}  rf={raw_obs['right_force_mag']:.4f}  "
      f"cuboid_rel_z={raw_obs['cuboid_rel_z']:.6f}  desired_rel_z={raw_obs['desired_rel_z']:.4f}")
print(f"STEP 0 (normalized flat): {obs_flat.tolist()}")

with torch.no_grad():
    for i in range(20):
        action = policy(obs_td).cpu().numpy().flatten()
        print(f"\n--- STEP {i+1} ---")
        print(f"  action: z_vel_cmd={action[0]:+.4f}  grip_l={action[1]:+.4f}  grip_r={action[2]:+.4f}")

        records, reward, done = env.step(action)

        raw = env.get_observation()
        obs_flat = env.get_obs_flat()
        obs_td = TensorDict({"policy": torch.tensor(obs_flat, dtype=torch.float32).unsqueeze(0)}, batch_size=[1])

        print(f"  raw:  ee_z={raw['ee_pos']:.4f}  ee_vel_z={raw['ee_vel']:+.6f}  "
              f"lf={raw['left_force_mag']:.4f}  rf={raw['right_force_mag']:.4f}  "
              f"cuboid_rel_z={raw['cuboid_rel_z']:+.6f}  desired={raw['desired_rel_z']:+.4f}")
        print(f"  norm: {[f'{x:+.4f}' for x in obs_flat.tolist()]}")
        print(f"  target_z={env.target_z:.4f}  target_z_vel={env.target_z_vel:+.4f}  rew={reward:.3f}")

        if done:
            print(f"  DONE: {env.last_done_reason}")
            break
