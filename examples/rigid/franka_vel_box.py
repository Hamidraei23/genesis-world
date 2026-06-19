import argparse
import time
from pathlib import Path

import numpy as np

import genesis as gs
import genesis.utils.geom as gu

REPO_ROOT = Path(__file__).resolve().parents[2]
def save_motion_plot(plot_file, times, target_z, actual_z, target_z_vel, actual_z_vel, position_error, qvel_norm):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)

    axes[0].plot(times, target_z, label="target z", linewidth=1.8)
    axes[0].plot(times, actual_z, label="actual z", linewidth=1.2)
    axes[0].set_ylabel("z position (m)")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(times, target_z_vel, label="target vz", linewidth=1.8)
    axes[1].plot(times, actual_z_vel, label="actual vz", linewidth=1.2)
    axes[1].set_ylabel("z velocity (m/s)")
    axes[1].legend(loc="best")
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(times, position_error, color="tab:red", linewidth=1.2)
    axes[2].set_ylabel("position error (m)")
    axes[2].grid(True, alpha=0.3)

    axes[3].plot(times, qvel_norm, color="tab:green", linewidth=1.2)
    axes[3].set_ylabel("||qvel||")
    axes[3].set_xlabel("time (s)")
    axes[3].grid(True, alpha=0.3)

    fig.suptitle("Franka End-Effector Velocity-Control Motion")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved motion plot to {plot_path.resolve()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=False)
    parser.add_argument("-c", "--cpu", action="store_true", default=False)
    parser.add_argument("--steps", type=int, default=10000)
    parser.add_argument("--plot-file", type=str, default="franka_velocity_motion.png")
    args = parser.parse_args()
    dt = 0.001
    render_fps = 60
    render_every = max(1, int(round(1.0 / (render_fps * dt))))

    ########################## init ##########################
    gs.init(backend=gs.cpu if args.cpu else gs.gpu)

    ########################## create a scene ##########################
    scene = gs.Scene(
        sim_options=gs.options.SimOptions(
            dt=dt,
            substeps=1,
        ),
        rigid_options=gs.options.RigidOptions(
            # constraint_solver=gs.constraint_solver.Newton,
        ),
        viewer_options=gs.options.ViewerOptions(
            camera_pos=(3.5, 0.0, 2.5),
            camera_lookat=(0.0, 0.0, 0.5),
            camera_fov=40,
            max_FPS=render_fps,
        ),
        show_viewer=args.vis,
    )

    ########################## entities ##########################
    plane = scene.add_entity(
        gs.morphs.Plane(),
    )
    franka = scene.add_entity(
        gs.morphs.MJCF(file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/panda.xml")),
        visualize_contact=True,
    )
    # Free cuboid loaded separately from MJCF, then positioned between fingers after FK.
    cuboid = scene.add_entity(
        gs.morphs.MJCF(file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/box.xml")),
        surface=gs.surfaces.Plastic(color=(0.18, 0.42, 0.82)),
    )

    ########################## cameras ##########################
    cam_0 = scene.add_camera(
        res=(1280, 960),
        pos=(3.5, 0.0, 2.5),
        lookat=(0, 0, 0.5),
        fov=30,
        GUI=True,
    )
    ########################## build ##########################
    scene.build()

    motors_dof = np.arange(7)
    fingers_dof = np.arange(7, 9)

    # Gains and force limits used by the joint velocity controller.
    franka.set_dofs_kp(
        np.array([4500, 4500, 3500, 3500, 2000, 2000, 2000]),
        motors_dof,
    )
    franka.set_dofs_kv(
        np.array([450, 450, 350, 350, 200, 200, 200]),
        motors_dof,
    )
    franka.set_dofs_force_range(
        np.array([-87, -87, -87, -87, -12, -12, -12]),
        np.array([87, 87, 87, 87, 12, 12, 12]),
        motors_dof,
    )

    franka.set_dofs_kp(np.array([100, 100]), fingers_dof)
    franka.set_dofs_kv(np.array([10, 10]), fingers_dof)
    franka.set_dofs_force_range(np.array([-100, -100]), np.array([100, 100]), fingers_dof)

    q_home = np.array([0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78, 0.000251, 0.000251])
    franka.set_qpos(q_home)
    franka.control_dofs_position(q_home)

    # Compute cuboid home pose from hand link FK (offset 10.29 cm forward in hand frame)
    _hand = franka.get_link("hand")
    _hand_pos = _hand.get_pos().cpu().numpy().flatten()
    _hand_quat = _hand.get_quat().cpu().numpy().flatten()
    _local_offset = np.array([0.0, 0.0, 0.1029])
    _local_quat = np.array([0.00187891, -0.71790805, -0.00193768, -0.69613270])
    _local_quat /= np.linalg.norm(_local_quat)
    cuboid_home_pos = _hand_pos + gu.transform_by_quat(_local_offset, _hand_quat)
    cuboid_home_quat = gu.transform_quat_by_quat(_local_quat, _hand_quat)
    cuboid.set_pos(cuboid_home_pos, zero_velocity=True)
    cuboid.set_quat(cuboid_home_quat, zero_velocity=True)

    ee_link = franka.get_link("hand")
    target_center = ee_link.get_pos().cpu().numpy()
    target_quat = ee_link.get_quat().cpu().numpy()
    target_amplitude = 0.1
    target_omega = 10.0
    pos_gain = 8.0
    rot_gain = 4.0
    damping = 1e-4
    jacobian_regularizer = damping * np.eye(6)

    for i in range(100):
        franka.control_dofs_position(q_home)
        scene.step(update_visualizer=i % render_every == 0)

    times = []
    target_z_history = []
    actual_z_history = []
    target_z_vel_history = []
    actual_z_vel_history = []
    position_error_history = []
    qvel_norm_history = []

    t_real_start = time.perf_counter()
    for i in range(args.steps):
        t = i * dt
        target_pos = target_center + np.array([0.0, 0.0, target_amplitude * np.sin(target_omega * t)])
        target_vel = np.array([0.0, 0.0, target_amplitude * target_omega * np.cos(target_omega * t)])

        error_pos = target_pos - ee_link.get_pos().cpu().numpy()
        ee_quat = ee_link.get_quat().cpu().numpy()
        error_quat = gs.transform_quat_by_quat(gs.inv_quat(ee_quat), target_quat)
        error_rotvec = gs.quat_to_rotvec(error_quat)

        ee_velocity_cmd = np.concatenate([target_vel + pos_gain * error_pos, rot_gain * error_rotvec])
        jacobian = franka.get_jacobian(link=ee_link).cpu().numpy()[:, motors_dof]
        qvel = jacobian.T @ np.linalg.solve(jacobian @ jacobian.T + jacobian_regularizer, ee_velocity_cmd)

        franka.control_dofs_velocity(qvel, motors_dof)
        franka.control_dofs_position(np.array([0.000251, 0.000251]), fingers_dof)

        scene.step(update_visualizer=i % render_every == 0)
        actual_pos = ee_link.get_pos().cpu().numpy()
        actual_vel = ee_link.get_vel().cpu().numpy()
        times.append((i + 1) * dt)
        target_z_history.append(target_pos[2])
        actual_z_history.append(actual_pos[2])
        target_z_vel_history.append(target_vel[2])
        actual_z_vel_history.append(actual_vel[2])
        position_error_history.append(np.linalg.norm(target_pos - actual_pos))
        qvel_norm_history.append(np.linalg.norm(qvel))
        # cam_0.render()

        # Real-time sync: sleep until wall time catches up to simulation time
        t_sim = (i + 1) * dt
        t_wall = time.perf_counter() - t_real_start
        if t_wall < t_sim:
            time.sleep(t_sim - t_wall)

    save_motion_plot(
        args.plot_file,
        np.array(times),
        np.array(target_z_history),
        np.array(actual_z_history),
        np.array(target_z_vel_history),
        np.array(actual_z_vel_history),
        np.array(position_error_history),
        np.array(qvel_norm_history),
    )


if __name__ == "__main__":
    main()
