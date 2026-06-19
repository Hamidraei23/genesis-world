import argparse
import time
from pathlib import Path

import numpy as np

import genesis as gs
import genesis.utils.geom as gu
from controller_franka import FrankaVelocityController

REPO_ROOT = Path(__file__).resolve().parents[2]


def save_motion_plot(
    plot_file,
    times,
    target_z,
    actual_z,
    target_z_vel,
    actual_z_vel,
    target_z_acc,
    actual_z_acc,
    position_error,
    qvel_norm,
):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(5, 1, figsize=(10, 14), sharex=True)

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

    axes[2].plot(times, target_z_acc, label="target az", linewidth=1.8)
    axes[2].plot(times, actual_z_acc, label="actual az", linewidth=1.2)
    axes[2].set_ylabel("z acceleration (m/s²)")
    axes[2].legend(loc="best")
    axes[2].grid(True, alpha=0.3)

    axes[3].plot(times, position_error, color="tab:red", linewidth=1.2)
    axes[3].set_ylabel("position error (m)")
    axes[3].grid(True, alpha=0.3)

    axes[4].plot(times, qvel_norm, color="tab:green", linewidth=1.2)
    axes[4].set_ylabel("||qvel||")
    axes[4].set_xlabel("time (s)")
    axes[4].grid(True, alpha=0.3)

    fig.suptitle("Franka End-Effector One-Shot Z Velocity-Control Motion")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved motion plot to {plot_path.resolve()}")


def save_torque_plot(plot_file, times, torques, torques_dot):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_path = Path(plot_file).expanduser()
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    torques = np.array(torques)      # shape (N, 7)
    torques_dot = np.array(torques_dot)  # shape (N, 7)
    labels = [f"J{j+1}" for j in range(torques.shape[1])]

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    for j, label in enumerate(labels):
        axes[0].plot(times, torques[:, j], label=label, linewidth=1.2)
    axes[0].set_ylabel("torque (N·m)")
    axes[0].legend(loc="best", ncol=4)
    axes[0].grid(True, alpha=0.3)

    for j, label in enumerate(labels):
        axes[1].plot(times, torques_dot[:, j], label=label, linewidth=1.2)
    axes[1].set_ylabel("torque rate (N·m/s)")
    axes[1].set_xlabel("time (s)")
    axes[1].legend(loc="best", ncol=4)
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("Franka Joint Torques")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    print(f"Saved torque plot to {plot_path.resolve()}")


def cyclic_z_reference(t, start_z, distance, accel_up=5.0, decel_mag=15.0):
    """
    Cyclic Z trajectory: accelerate up → brake to peak → mirror back down → repeat.
    The downward leg is the exact time-reversal of the upward leg.
    """
    if distance <= 0.0:
        return start_z, 0.0, 0.0

    v_peak = np.sqrt(2.0 * distance * accel_up * decel_mag / (accel_up + decel_mag))
    t_accel = v_peak / accel_up
    t_decel = v_peak / decel_mag
    half_period = t_accel + t_decel
    d_accel = 0.5 * accel_up * t_accel**2
    d_decel = 0.5 * decel_mag * t_decel**2  # == distance - d_accel

    tau = t % (2.0 * half_period)

    if tau < t_accel:                          # Phase 1: accel up
        z = start_z + 0.5 * accel_up * tau**2
        z_vel = accel_up * tau
        z_acc = accel_up
    elif tau < half_period:                    # Phase 2: brake to peak
        s = tau - t_accel
        z = start_z + d_accel + v_peak * s - 0.5 * decel_mag * s**2
        z_vel = v_peak - decel_mag * s
        z_acc = -decel_mag
    elif tau < half_period + t_decel:          # Phase 3: mirror of phase 2 (accel down)
        s = tau - half_period
        z = start_z + distance - 0.5 * decel_mag * s**2
        z_vel = -decel_mag * s
        z_acc = -decel_mag
    else:                                      # Phase 4: mirror of phase 1 (brake to start)
        s = tau - half_period - t_decel
        z = start_z + distance - d_decel - v_peak * s + 0.5 * accel_up * s**2
        z_vel = -v_peak + accel_up * s
        z_acc = accel_up

    return z, z_vel, z_acc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=False)
    parser.add_argument("-c", "--cpu", action="store_true", default=False)

    # The simulation now runs until you stop it manually.
    # This argument is kept only so old command lines do not break.
    parser.add_argument("--steps", type=int, default=10000)

    parser.add_argument("--plot-file", type=str, default="franka_velocity_motion.png")
    args = parser.parse_args()

    dt = 0.001
    target_dt = 0.02
    target_update_every = max(1, int(round(target_dt / dt)))
    target_period = target_update_every * dt
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
        gs.morphs.MJCF(
            file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/panda.xml")
        ),
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
    franka.set_dofs_force_range(
        np.array([-100, -100]),
        np.array([100, 100]),
        fingers_dof,
    )

    q_home = np.array([0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78, 0.01090, 0.01090])
    franka.set_qpos(q_home)
    franka.control_dofs_position(q_home)

    # Compute cuboid home pose from hand link FK (offset 10.29 cm forward in hand frame).
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

    left_finger = franka.get_link("left_finger")
    right_finger = franka.get_link("right_finger")
    fingertip_local_pos = np.array([0.0, 0.0055, 0.0445])

    def get_fingertip_pos(finger_link):
        finger_pos = finger_link.get_pos().cpu().numpy().flatten()
        finger_quat = finger_link.get_quat().cpu().numpy().flatten()
        return finger_pos + gu.transform_by_quat(fingertip_local_pos, finger_quat)

    def get_fingertip_distance():
        left_fingertip_pos = get_fingertip_pos(left_finger)
        right_fingertip_pos = get_fingertip_pos(right_finger)
        return np.linalg.norm(left_fingertip_pos - right_fingertip_pos)

    def get_finger_net_contact_forces():
        link_forces = franka.get_links_net_contact_force().cpu().numpy()
        return link_forces[left_finger.idx_local], link_forces[right_finger.idx_local]

    def print_finger_status(label):
        fingertip_distance = get_fingertip_distance()
        left_force, right_force = get_finger_net_contact_forces()
        print(f"{label}: fingertip distance = {fingertip_distance:.6f} m")
        print("left finger force:", left_force, "norm:", np.linalg.norm(left_force))
        print("right finger force:", right_force, "norm:", np.linalg.norm(right_force))

    print_finger_status("initial")

    ee_link = franka.get_link("hand")

    target_center = ee_link.get_pos().cpu().numpy()
    target_quat = ee_link.get_quat().cpu().numpy()

    # One-shot Z trajectory settings.
    target_distance = 0.1    # meters upward
    upward_accel = 5.0       # m/s^2
    braking_accel = 13.0     # m/s^2

    pos_gain = 8.0
    rot_gain = 4.0
    damping = 1e-4
    jacobian_regularizer = damping * np.eye(6)
    controller = FrankaVelocityController(
        franka=franka,
        scene=scene,
        ee_link=ee_link,
        motors_dof=motors_dof,
        target_center=target_center,
        target_quat=target_quat,
        pos_gain=pos_gain,
        rot_gain=rot_gain,
        jacobian_regularizer=jacobian_regularizer,
        dt=dt,
        target_period=target_period,
        steps_per_target=target_update_every,
        render_every=render_every,
    )

    def sample_target_z(t):
        return cyclic_z_reference(
            t=t,
            start_z=target_center[2],
            distance=target_distance,
            accel_up=upward_accel,
            decel_mag=braking_accel,
        )

    for i in range(100):
        franka.control_dofs_position(q_home)
        scene.step(update_visualizer=i % render_every == 0)

    times = []
    target_z_history = []
    actual_z_history = []
    target_z_vel_history = []
    actual_z_vel_history = []
    target_z_acc_history = []
    actual_z_acc_history = []
    position_error_history = []
    qvel_norm_history = []
    torque_history = []
    prev_torque = None

    prev_actual_z_vel = None
    gripper_target_sample = None
    sim_steps = 10000
    high_level_steps = sim_steps // target_update_every

    print(
        "Realistic smooth target stream enabled: "
        f"target_dt={target_period:.3f}s, sim_dt={dt:.3f}s, interpolation_delay={target_period:.3f}s"
    )

    t_real_start = time.perf_counter()
    i = 0

    try:
        for i in range(high_level_steps):
            if i == 0:
                print_finger_status("before motion start")
            start_sim_step = i * target_update_every
            t = start_sim_step * dt
            high_level_sample = sample_target_z(t)

            if gripper_target_sample is None:
                gripper_target_sample = high_level_sample
            if gripper_target_sample[0] >= 0.928:
                franka.control_dofs_position(np.array([0.0124, 0.0124]), fingers_dof)
            else:
                franka.control_dofs_position(np.array([0.000251, 0.000251]), fingers_dof)

            records = controller.step(
                target_z=high_level_sample[0],
                target_z_vel=high_level_sample[1],
                target_z_acc=high_level_sample[2],
                start_sim_step=start_sim_step,
            )
            gripper_target_sample = high_level_sample

            for record in records:
                actual_z_acc = (
                    (record.actual_vel[2] - prev_actual_z_vel) / dt
                    if prev_actual_z_vel is not None
                    else 0.0
                )
                prev_actual_z_vel = record.actual_vel[2]

                times.append(record.time)
                target_z_history.append(record.target_pos[2])
                actual_z_history.append(record.actual_pos[2])
                target_z_vel_history.append(record.target_vel[2])
                actual_z_vel_history.append(record.actual_vel[2])
                target_z_acc_history.append(record.target_z_acc)
                actual_z_acc_history.append(actual_z_acc)
                position_error_history.append(np.linalg.norm(record.target_pos - record.actual_pos))
                qvel_norm_history.append(np.linalg.norm(record.qvel))
                tau_dot = (
                    (record.tau - prev_torque) / dt if prev_torque is not None else np.zeros_like(record.tau)
                )
                prev_torque = record.tau
                torque_history.append((record.tau, tau_dot))

                if (record.sim_step + 1) % 1000 == 0:
                    print_finger_status(f"step {record.sim_step + 1}")

            # cam_0.render()

            # Real-time sync: sleep until wall time catches up to simulation time.
            t_sim = records[-1].time
            t_wall = time.perf_counter() - t_real_start

            if t_wall < t_sim:
                time.sleep(t_sim - t_wall)

    except KeyboardInterrupt:
        print("Simulation stopped manually.")

    if len(times) > 0:
        save_motion_plot(
            args.plot_file,
            np.array(times),
            np.array(target_z_history),
            np.array(actual_z_history),
            np.array(target_z_vel_history),
            np.array(actual_z_vel_history),
            np.array(target_z_acc_history),
            np.array(actual_z_acc_history),
            np.array(position_error_history),
            np.array(qvel_norm_history),
        )
        torque_plot_file = str(Path(args.plot_file).with_stem(Path(args.plot_file).stem + "_torques"))
        save_torque_plot(
            torque_plot_file,
            np.array(times),
            [t[0] for t in torque_history],
            [t[1] for t in torque_history],
        )


if __name__ == "__main__":
    main()
