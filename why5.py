"""Authoritative run lengths from the env's own success_steps, plus the true breaker.

The earlier script recomputed slip speed after env.step() had already advanced
_prev_cuboid_rel_z, so its slip term was always zero. This one snapshots the
relative position before each step and reads success_candidate from the env.
"""
import pickle, sys, torch, genesis as gs
sys.path.insert(0, "examples/rigid")
from rsl_rl.runners import OnPolicyRunner
from env_franka_regrasp import FrankaEnvParallel

LOG, CKPT, ENVS, STEPS = "logs/franka-regrasp-v35", 720, 256, 900
gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=1, performance_mode=True)
env = FrankaEnvParallel(num_envs=ENVS, vis=False, dt=0.001, target_dt=0.02,
                        mix=False, solid_up=False, normalize=True)
runner = OnPolicyRunner(env, pickle.load(open(f"{LOG}/train_cfg.pkl", "rb")), LOG, device=gs.device)
runner.load(f"{LOG}/model_{CKPT}.pt")
policy = runner.get_inference_policy(device=gs.device)

names = ("z error", "lateral", "ee vz", "ee lateral vel", "slip speed", "regrasp", "acc rms", "fail")
obs = env.reset()
prev_rel = env._relative_position()[:, 2].clone()
peak_run = torch.zeros(ENVS, device=gs.device)
run_hist, breakers = [], torch.zeros(len(names), device=gs.device)
outcome = {"success": 0, "fail": 0, "timeout": 0}
prev_steps = torch.zeros(ENVS, device=gs.device)
with torch.no_grad():
    for _ in range(STEPS):
        obs, _, done, _ = env.step(policy(obs))
        t = env.last_reward_terms
        rel, eev = env._relative_position(), env.ee_link.get_vel()
        err = (rel[:, 2] - env.desired_rel_z).abs()
        slip = ((rel[:, 2] - prev_rel) / env.target_period).abs()
        prev_rel = rel[:, 2].clone()
        checks = torch.stack([
            err <= 0.005, rel[:, :2].norm(dim=-1) <= 0.015,
            eev[:, 2].abs() < env.SUCCESS_EE_VZ_MAX,
            eev[:, :2].norm(dim=-1) < env.SUCCESS_EE_LATERAL_VEL_MAX,
            slip < 0.01, env._regrasp_count > 0,
            t["acceleration_rms"] <= env.SUCCESS_ACC_RMS_MAX, t["fail"] < 0.5,
        ])
        steps_now = t["success_steps"]
        # A run ended when the env's own counter dropped without the episode ending.
        broke = (steps_now < prev_steps) & ~done
        if broke.any():
            run_hist.append(prev_steps[broke].clone())
            for i in range(len(names)):
                breakers[i] += (broke & ~checks[i]).sum()
        prev_steps = torch.where(done, torch.zeros_like(steps_now), steps_now)
        peak_run = torch.maximum(peak_run, steps_now)
        if done.any():
            for key in outcome:
                outcome[key] += int(t[key][done].sum())
            peak_run[done] = 0

need = round(env.SUCCESS_HOLD_SECONDS / env.target_period)
total_ep = sum(outcome.values())
print(f"\nsteps needed for success: {need}")
print(f"\n{'outcome':<10}{'episodes':>10}{'share':>9}")
for key, n in outcome.items():
    print(f"{key:<10}{n:>10}{100*n/max(total_ep,1):>8.1f}%")
if run_hist:
    lens = torch.cat(run_hist)
    print(f"\nbroken runs: {lens.numel()}, longest {int(lens.max())} steps")
    print(f"{'run reached':<16}{'count':>8}{'share':>9}")
    for n in range(1, need + 2):
        c = int((lens >= n).sum())
        print(f"  {n:>2} steps{'':<6}{c:>8}{100*c/lens.numel():>8.1f}%")
    print(f"\n{'condition that broke the run':<30}{'% of breaks':>13}")
    for name, c in zip(names, breakers.tolist()):
        print(f"  {name:<28}{100*c/lens.numel():>12.1f}%")
