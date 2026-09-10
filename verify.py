"""Same checkpoint, new success gate: does it actually succeed now?"""
import pickle, sys, torch, genesis as gs
sys.path.insert(0, "examples/rigid")
from rsl_rl.runners import OnPolicyRunner
from env_franka_regrasp import FrankaEnvParallel

LOG, CKPT, ENVS, STEPS = "logs/franka-regrasp-v34", 260, 256, 900
gs.init(backend=gs.gpu, precision="32", logging_level="warning", seed=1, performance_mode=True)
env = FrankaEnvParallel(num_envs=ENVS, vis=False, dt=0.001, target_dt=0.02,
                        mix=False, solid_up=False, normalize=True)
runner = OnPolicyRunner(env, pickle.load(open(f"{LOG}/train_cfg.pkl", "rb")), LOG, device=gs.device)
runner.load(f"{LOG}/model_{CKPT}.pt")
policy = runner.get_inference_policy(device=gs.device)

obs = env.reset()
outcome = {"success": 0, "fail": 0, "timeout": 0}
run = torch.zeros(ENVS, dtype=torch.long, device=gs.device)
best_run = torch.zeros(ENVS, dtype=torch.long, device=gs.device)
runs_seen = []
with torch.no_grad():
    for _ in range(STEPS):
        obs, _, done, _ = env.step(policy(obs))
        t = env.last_reward_terms
        run = torch.where(t["success_candidate"] > 0, run + 1, torch.zeros_like(run))
        best_run = torch.maximum(best_run, run)
        if done.any():
            for key in outcome:
                outcome[key] += int(t[key][done].sum())
            runs_seen.append(best_run[done].clone())
            best_run[done] = 0; run[done] = 0

total = sum(outcome.values())
need = round(env.SUCCESS_HOLD_SECONDS / env.target_period)
print(f"\nsteps needed for success now: {need}\n")
print(f"{'outcome':<10}{'episodes':>10}{'share':>9}")
for key, n in outcome.items():
    print(f"{key:<10}{n:>10}{100*n/max(total,1):>8.1f}%")
b = torch.cat(runs_seen).float()
print(f"\nbest candidate run per episode, steps")
for p in (0.5, 0.75, 0.9, 1.0):
    print(f"  p{int(p*100):<3}{torch.quantile(b, p).item():>7.1f}")
