import re

with open('scratch/new_run_policy.py', 'r') as f:
    content = f.read()

# 1. Add matplotlib import at top
content = re.sub(
    r'(import numpy as np)',
    r'\1\nimport matplotlib\nmatplotlib.use("Agg")\nimport matplotlib.pyplot as plt',
    content
)

# 2. Replace the old plotting functions with the new ones
old_plot_funcs_regex = re.compile(r'def _shade_releases.*?def _resolve_checkpoint', re.DOTALL)

new_plot_funcs = """def _shade_releases(ax, release_spans):
    for idx, (s0, s1) in enumerate(release_spans):
        ax.axvspan(s0, s1, color="orange", alpha=0.25, label="release window" if idx == 0 else "")

def _plot_episode(steps_buf, release_spans, ep_idx, save_dir):
    steps = np.asarray(steps_buf["steps"])
    ee_z = np.asarray(steps_buf["ee_z"])
    cub_rel_z = np.asarray(steps_buf["cuboid_rel_z"])
    des_rel_z = np.asarray(steps_buf["desired_rel_z"])
    ft_dist = np.asarray(steps_buf["ft_dist"])
    lf_mag = np.asarray(steps_buf["lf_mag"])
    rf_mag = np.asarray(steps_buf["rf_mag"])
    reward = np.asarray(steps_buf["reward"])
    cum_reward = np.cumsum(reward)

    fig, axes = plt.subplots(5, 1, figsize=(12, 14), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Overview  (orange = gripper release)", fontsize=12)

    panels = [
        (axes[0], [(cub_rel_z, "cuboid_rel_z", "C0"), (des_rel_z, "desired_rel_z", "C3--")], "Z relative [m]", "Cuboid in-hand Z"),
        (axes[1], [(ee_z, "ee_pos_z", "C1")], "EE height [m]", "End-effector Z"),
        (axes[2], [(ft_dist, "fingertip_dist", "C2")], "Distance [m]", "Fingertip distance"),
        (axes[3], [(lf_mag, "|left_force|", "C4"), (rf_mag, "|right_force|", "C5")], "Force [N]", "Finger contact forces"),
        (axes[4], [(reward, "reward/step", "C6"), (cum_reward, "cumulative", "C7")], "Reward", "Reward"),
    ]
    for ax, series, ylabel, title in panels:
        for y, label, fmt in series:
            ax.plot(steps, y, fmt, label=label, linewidth=1.2)
        _shade_releases(ax, release_spans)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("High-level step")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_overview.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] overview  → {path}")

def _plot_motion(times, target_z_history, actual_z_history, target_z_vel_history, actual_z_vel_history, target_z_acc_history, actual_z_acc_history, position_error_history, release_spans_time, ep_idx, save_dir):
    times = np.asarray(times)
    ee_z = np.asarray(actual_z_history)
    target_z = np.asarray(target_z_history)
    actual_z_vel = np.asarray(actual_z_vel_history)
    target_z_vel = np.asarray(target_z_vel_history)
    actual_z_acc = np.asarray(actual_z_acc_history)
    target_z_acc = np.asarray(target_z_acc_history)
    pos_err = np.asarray(position_error_history)

    fig, axes = plt.subplots(4, 1, figsize=(12, 11), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Motion detail  (orange = gripper release)", fontsize=12)

    panels = [
        (axes[0], [(ee_z, "actual ee_z", "C0"), (target_z, "target z", "C3--")], "Z position [m]", "EE Z position (actual vs target)"),
        (axes[1], [(actual_z_vel, "actual z_vel", "C0"), (target_z_vel, "target z_vel", "C3--")], "Z velocity [m/s]", "Z velocity (actual vs target)"),
        (axes[2], [(actual_z_acc, "actual z_acc", "C0"), (target_z_acc, "target z_acc", "C3--")], "Z accel [m/s²]", "Z acceleration (actual vs commanded)"),
        (axes[3], [(pos_err, "position_error", "C5")], "Pos Error [m]", "EE Position Error"),
    ]
    for ax, series, ylabel, title in panels:
        for y, label, fmt in series:
            ax.plot(times, y, fmt, label=label, linewidth=1.2)
        _shade_releases(ax, release_spans_time)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("Time (s)")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_motion.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] motion    → {path}")

def _plot_torques(times, torque_history, release_spans_time, ep_idx, save_dir):
    times = np.asarray(times)
    torques = np.asarray([t[0] for t in torque_history])
    torques_dot = np.asarray([t[1] for t in torque_history])

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Torques  (orange = gripper release)", fontsize=12)

    labels = [f"J{j + 1}" for j in range(torques.shape[1])]
    for j, label in enumerate(labels):
        axes[0].plot(times, torques[:, j], label=label, linewidth=1.2)
    _shade_releases(axes[0], release_spans_time)
    axes[0].set_ylabel("torque (N*m)")
    axes[0].legend(loc="upper left", ncol=4, fontsize=7)
    axes[0].grid(True, alpha=0.3)

    for j, label in enumerate(labels):
        axes[1].plot(times, torques_dot[:, j], label=label, linewidth=1.2)
    _shade_releases(axes[1], release_spans_time)
    axes[1].set_ylabel("torque rate (N*m/s)")
    axes[1].set_xlabel("Time (s)")
    axes[1].legend(loc="upper left", ncol=4, fontsize=7)
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_torques.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] torques   → {path}")

def _plot_reward_parts(steps_buf, release_spans, ep_idx, save_dir):
    steps = np.asarray(steps_buf["steps"])
    keys = [k for k in steps_buf.keys() if k not in ["steps", "ee_z", "cuboid_rel_z", "desired_rel_z", "ft_dist", "lf_mag", "rf_mag", "reward"]]
    fig, axes = plt.subplots(len(keys), 1, figsize=(12, 2.5 * len(keys)), sharex=True)
    fig.suptitle(f"Episode {ep_idx} — Reward parts  (orange = gripper release)", fontsize=12)

    colors = [f"C{i}" for i in range(len(keys))]
    for ax, key, color in zip(axes, keys, colors):
        vals = np.asarray(steps_buf[key])
        ax.plot(steps, vals, color=color, linewidth=1.2, label=key)
        ax.axhline(0, color="k", linewidth=0.5, linestyle="--")
        _shade_releases(ax, release_spans)
        ax.set_ylabel(key, fontsize=8)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("High-level step")
    plt.tight_layout()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"ep_{ep_idx:03d}_reward_parts.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"  [PLOT] rew parts → {path}")

def _resolve_checkpoint"""

content = old_plot_funcs_regex.sub(new_plot_funcs, content)

# 3. Main loop and variable initialization changes
content = content.replace('    times = []\n    target_z_history = []', '''    times = []
    target_z_history = []
    actual_z_history = []
    target_z_vel_history = []
    actual_z_vel_history = []
    target_z_acc_history = []
    actual_z_acc_history = []
    position_error_history = []
    torque_history = []

    def _fresh_buffers():
        d = dict(steps=[], ee_z=[], cuboid_rel_z=[], desired_rel_z=[], ft_dist=[], lf_mag=[], rf_mag=[], reward=[])
        for k, _ in REWARD_PARTS:
            d[k] = []
        return d
    steps_buf = _fresh_buffers()''')

content = content.replace('    target_z_history = []\n    actual_z_history = []\n    target_z_vel_history = []\n    actual_z_vel_history = []\n    target_z_acc_history = []\n    actual_z_acc_history = []\n    z_error_history = []\n    position_error_history = []\n    qvel_norm_history = []\n    torque_history = []\n    reward_step_history = []\n    reward_history = []\n    episode_return_history = []\n    reward_parts_history = {key: [] for key, _ in REWARD_PARTS}\n    cuboid_z_error_step_times = []\n    cuboid_z_error_vals = []', '')

content = content.replace('''                reward_step_history.append(ep_len)
                reward_history.append(reward)
                episode_return_history.append(ep_reward)
                for key, _ in REWARD_PARTS:
                    reward_parts_history[key].append(env.last_reward_terms[key])''', '''                steps_buf["steps"].append(ep_len)
                steps_buf["ee_z"].append(unnorm_obs["ee_pos"])
                steps_buf["cuboid_rel_z"].append(unnorm_obs["cuboid_rel_z"])
                steps_buf["desired_rel_z"].append(unnorm_obs["desired_rel_z"])
                steps_buf["ft_dist"].append(env.get_fingertip_distance())
                steps_buf["lf_mag"].append(unnorm_obs["left_force_mag"])
                steps_buf["rf_mag"].append(unnorm_obs["right_force_mag"])
                steps_buf["reward"].append(reward)
                for key, _ in REWARD_PARTS:
                    steps_buf[key].append(float(env.last_reward_terms.get(key, 0.0)))''')

# 4. Handle break and reset logic
old_done_block = '''                if done:
                    if in_release and times:
                        release_spans_time.append((release_start_time, times[-1]))
                        release_spans_step.append((release_start_step, ep_len))
                        in_release = False
                    success = env.last_done_reason == "success"
                    ep_count += 1
                    result = "SUCCESS" if success else f"fail reason={env.last_done_reason or 'unknown'}"
                    print(f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  {result}")
                    break'''

new_done_block = '''                if done:
                    if in_release and times:
                        release_spans_time.append((release_start_time, times[-1]))
                        release_spans_step.append((release_start_step, ep_len))
                        in_release = False
                    success = env.last_done_reason == "success"
                    ep_count += 1
                    result = "SUCCESS" if success else f"fail reason={env.last_done_reason or 'unknown'}"
                    print(f"  [EP {ep_count}] len={ep_len}  reward={ep_reward:.1f}  {result}")
                    
                    if args.plot and len(times) > 0:
                        save_dir = os.path.join(log_dir, "plots")
                        _plot_episode(steps_buf, release_spans_step, ep_count, save_dir)
                        _plot_motion(times, target_z_history, actual_z_history, target_z_vel_history, actual_z_vel_history, target_z_acc_history, actual_z_acc_history, position_error_history, release_spans_time, ep_count, save_dir)
                        _plot_torques(times, torque_history, release_spans_time, ep_count, save_dir)
                        _plot_reward_parts(steps_buf, release_spans_step, ep_count, save_dir)

                    steps_buf = _fresh_buffers()
                    times = []
                    target_z_history = []
                    actual_z_history = []
                    target_z_vel_history = []
                    actual_z_vel_history = []
                    target_z_acc_history = []
                    actual_z_acc_history = []
                    position_error_history = []
                    torque_history = []
                    release_spans_time = []
                    release_spans_step = []
                    in_release = False
                    ep_reward = 0.0
                    ep_len = 0'''

content = content.replace(old_done_block, new_done_block)

# 5. Remove plot saving at end of file since it's already saved per episode
old_plot_end = '''    if args.plot:
        if len(times) > 0:
            save_policy_motion_plot(
                args.plot_file,
                np.array(times),
                np.array(target_z_history),
                np.array(actual_z_history),
                np.array(target_z_vel_history),
                np.array(actual_z_vel_history),
                np.array(target_z_acc_history),
                np.array(actual_z_acc_history),
                np.array(z_error_history),
                np.array(position_error_history),
                np.array(qvel_norm_history),
                release_spans=release_spans_time,
                cuboid_z_error_times=np.array(cuboid_z_error_step_times),
                cuboid_z_error=np.array(cuboid_z_error_vals),
            )
            torque_plot_file = str(Path(args.plot_file).with_stem(Path(args.plot_file).stem + "_torques"))
            save_torque_plot(
                torque_plot_file,
                np.array(times),
                [t[0] for t in torque_history],
                [t[1] for t in torque_history],
            )

        if len(reward_step_history) > 0:
            save_reward_plot(
                args.reward_plot_file,
                np.array(reward_step_history),
                np.array(reward_history),
                np.array(episode_return_history),
                release_spans=release_spans_step,
            )
            save_reward_parts_plot(
                args.reward_parts_plot_file,
                np.array(reward_step_history),
                {key: np.array(values) for key, values in reward_parts_history.items()},
                release_spans=release_spans_step,
            )'''

content = content.replace(old_plot_end, '')

with open('scratch/new_run_policy.py', 'w') as f:
    f.write(content)
