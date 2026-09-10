"""How long do candidate runs last, and which condition ends them?"""
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

names = ("z error", "lateral", "ee speed", "slip speed", "regrasp", "acc rms")
obs = env.reset()
run = torch.zeros(ENVS, dtype=torch.long, device=gs.device)
lengths, breakers = [], torch.zeros(len(names), device=gs.device)
with torch.no_grad():
    for _ in range(STEPS):
        obs, _, done, _ = env.step(policy(obs))
        rel, eev = env._relative_position(), env.ee_link.get_vel()
        err = (rel[:, 2] - env.desired_rel_z).abs()
        slip = ((rel[:, 2] - env._prev_cuboid_rel_z) / env.target_period).abs()
        checks = torch.stack([
            err <= 0.005, rel[:, :2].norm(dim=-1) <= 0.015, eev.norm(dim=-1) < 0.02,
            slip < 0.01, env._regrasp_count > 0,
            env.last_reward_terms["acceleration_rms"] <= env.SUCCESS_ACC_RMS_MAX,
        ])
        ok = checks.all(dim=0)
        ended = (~ok) & (run > 0)
        if ended.any():
            lengths.append(run[ended].clone())
            for i in range(len(names)):
                breakers[i] += (ended & ~checks[i]).sum()
        run = torch.where(ok, run + 1, torch.zeros_like(run))
        run = torch.where(done, torch.zeros_like(run), run)

lens = torch.cat(lengths).float()
need = round(env.SUCCESS_HOLD_SECONDS / env.target_period)
print(f"\ncandidate runs observed: {lens.numel()}")
print(f"steps needed for success: {need}\n")
print(f"{'run length (steps)':<22}{'count':>8}{'cumulative %':>14}")
total = lens.numel()
for n in (1, 2, 3, 4, 5, 6, 8, 10):
    c = (lens >= n).sum().item()
    print(f"  reached {n:>2}{'':<11}{c:>8}{100*c/total:>13.1f}%")
print(f"\nlongest run seen: {int(lens.max())} steps = {lens.max()*env.target_period*1000:.0f} ms")
print(f"\n{'condition that ended the run':<28}{'% of runs':>11}")
for name, c in zip(names, breakers.tolist()):
    print(f"  {name:<26}{100*c/total:>10.1f}%")
