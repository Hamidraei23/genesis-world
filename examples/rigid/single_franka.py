import argparse
import multiprocessing
import os
from pathlib import Path
import time

import genesis as gs
import genesis.utils.geom as gu
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]


def null_space_step(franka, ee_link, q_arm, q_arm_goal, step_size=0.02, damping=1e-3):
    """
    Advance the 7-DOF arm joints one step toward q_arm_goal while keeping the
    end-effector Cartesian pose fixed by projecting the motion into the
    null-space of the EE Jacobian.

    The null-space projector is  N = I - J⁺ J  where J⁺ is the damped
    pseudoinverse  J⁺ = Jᵀ (J Jᵀ + λ I)⁻¹.  Any joint velocity in the
    range of N produces zero EE velocity, so the EE remains stationary.

    Parameters
    ----------
    franka   : RigidEntity  (must have requires_jac_and_IK=True)
    ee_link  : RigidLink    target end-effector link (e.g. 'hand')
    q_arm    : np.ndarray shape (7,)  current arm joint positions
    q_arm_goal : np.ndarray shape (7,)  desired arm joint positions
    step_size  : float  max joint-space step per call (rad)
    damping    : float  Tikhonov regularisation λ for J⁺

    Returns
    -------
    np.ndarray shape (7,)  next arm joint targets
    """
    # Jacobian: (6, n_dofs) – take the 7 arm columns (fingers are passive here)
    J_full = franka.get_jacobian(ee_link)  # (6, 9)
    J = J_full[:, :7].cpu().numpy()        # (6, 7)

    # Damped pseudoinverse: J⁺ = Jᵀ (J Jᵀ + λ I)⁻¹
    JJT = J @ J.T                                           # (6, 6)
    J_pinv = J.T @ np.linalg.solve(JJT + damping * np.eye(6), np.eye(6))  # (7, 6)

    # Null-space projector  N = I - J⁺ J
    N = np.eye(7) - J_pinv @ J  # (7, 7)

    # Desired delta in joint space, normalised then scaled by step_size
    d = q_arm_goal - q_arm
    norm_d = np.linalg.norm(d)
    if norm_d < 1e-5:          # already at goal
        return q_arm
    dq = step_size * N @ (d / norm_d)
    return q_arm + dq


def assert_same_ee_pose(franka, ee_link, motors_dof_idx, q_a, q_b, pos_tol=1e-3, rot_tol=5e-3):
    """
    Verify that q_a and q_b produce the same end-effector Cartesian pose via FK.

    Temporarily drives the robot to each configuration (set_dofs_position),
    reads the EE link pos/quat, then restores q_a.  Raises ValueError if the
    positional or rotational discrepancy exceeds the given tolerances.

    Parameters
    ----------
    pos_tol : float   max allowed EE position error in metres  (default 0.5 mm)
    rot_tol : float   max allowed quaternion angular error in rad (default ~0.28 deg)
    """
    def _read_ee(q):
        franka.set_dofs_position(np.array(q, dtype=gs.np_float), motors_dof_idx)
        pos  = ee_link.get_pos().cpu().numpy().flatten()
        quat = ee_link.get_quat().cpu().numpy().flatten()  # (w, x, y, z)
        return pos, quat

    pos_a, quat_a = _read_ee(q_a)
    pos_b, quat_b = _read_ee(q_b)
    _read_ee(q_a)  # restore

    pos_err = float(np.linalg.norm(pos_a - pos_b))

    # Angular distance via dot product: θ = 2 * arccos(|q_a · q_b|)
    dot = float(np.clip(np.abs(np.dot(quat_a, quat_b)), -1.0, 1.0))
    rot_err = float(2.0 * np.arccos(dot))

    ok = pos_err <= pos_tol and rot_err <= rot_tol
    status = "PASS" if ok else "FAIL"
    print(
        f"[FK check] {status}  "
        f"pos_err={pos_err*1000:.3f} mm (tol {pos_tol*1000:.1f} mm)  "
        f"rot_err={np.degrees(rot_err):.4f} deg (tol {np.degrees(rot_tol):.2f} deg)"
    )
    if not ok:
        raise ValueError(
            f"q_home and q2 do NOT produce the same EE pose: "
            f"pos_err={pos_err*1000:.3f} mm, rot_err={np.degrees(rot_err):.4f} deg"
        )


def _start_joint_gui(joint_names, joint_limits, q_home, target_q, current_q, snap_event, stop_event):
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        print(f"Tkinter is not installed in this Python environment: {exc}", flush=True)
        stop_event.set()
        return

    root = tk.Tk()
    root.title("Franka Joint Controller")
    root.minsize(720, 420)

    slider_vars = []
    entry_vars = []
    current_labels = []
    lower_limits = [float(limit[0]) for limit in joint_limits]
    upper_limits = [float(limit[1]) for limit in joint_limits]

    header = ttk.Frame(root, padding=(10, 10, 10, 4))
    header.pack(fill=tk.X)
    ttk.Label(header, text="Joint").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
    ttk.Label(header, text="Target").grid(row=0, column=2, sticky=tk.W, padx=(10, 10))
    ttk.Label(header, text="Current").grid(row=0, column=3, sticky=tk.W)

    rows = ttk.Frame(root, padding=(10, 0, 10, 10))
    rows.pack(fill=tk.BOTH, expand=True)
    rows.columnconfigure(1, weight=1)

    def set_target(idx, value, update_slider=True):
        clipped = float(np.clip(value, lower_limits[idx], upper_limits[idx]))
        target_q[idx] = clipped
        if update_slider:
            slider_vars[idx].set(clipped)
        entry_vars[idx].set(f"{clipped:.3f}")

    for idx, name in enumerate(joint_names):
        ttk.Label(rows, text=name, width=14).grid(row=idx, column=0, sticky=tk.W, pady=4)

        slider_var = tk.DoubleVar(value=float(q_home[idx]))
        entry_var = tk.StringVar(value=f"{float(q_home[idx]):.3f}")
        slider_vars.append(slider_var)
        entry_vars.append(entry_var)

        slider = ttk.Scale(
            rows,
            from_=lower_limits[idx],
            to=upper_limits[idx],
            variable=slider_var,
            command=lambda value, i=idx: set_target(i, float(value), update_slider=False),
        )
        slider.grid(row=idx, column=1, sticky=tk.EW, padx=(0, 10), pady=4)

        entry = ttk.Entry(rows, width=10, textvariable=entry_var)
        entry.grid(row=idx, column=2, sticky=tk.W, padx=(0, 10), pady=4)

        def commit_entry(event=None, i=idx):
            try:
                set_target(i, float(entry_vars[i].get()))
            except ValueError:
                entry_vars[i].set(f"{float(target_q[i]):.3f}")

        entry.bind("<Return>", commit_entry)
        entry.bind("<FocusOut>", commit_entry)

        current_label = ttk.Label(rows, text=f"{q_home[idx]:.3f}", width=10)
        current_label.grid(row=idx, column=3, sticky=tk.W, pady=4)
        current_labels.append(current_label)

    footer = ttk.Frame(root, padding=(10, 0, 10, 10))
    footer.pack(fill=tk.X)

    def go_home():
        for idx, value in enumerate(q_home):
            set_target(idx, float(value))
        snap_event.set()

    def close():
        stop_event.set()
        root.destroy()

    ttk.Button(footer, text="Home", command=go_home).pack(side=tk.LEFT)
    ttk.Button(footer, text="Quit", command=close).pack(side=tk.RIGHT)

    def refresh_current_values():
        if stop_event.is_set():
            if root.winfo_exists():
                root.destroy()
            return
        for idx, label in enumerate(current_labels):
            label.config(text=f"{float(current_q[idx]):.3f}")
        root.after(100, refresh_current_values)

    root.protocol("WM_DELETE_WINDOW", close)
    refresh_current_values()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=False)
    parser.add_argument("-c", "--cpu", action="store_true", default=False)
    parser.add_argument(
        "--null-traj",
        action="store_true",
        default=False,
        help="Demo: drive arm from q_home to q2 via null-space motion (EE stays fixed).",
    )
    args = parser.parse_args()

    ########################## init ##########################
    gs.init(backend=gs.cpu if args.cpu else gs.gpu)

    ########################## create a scene ##########################
    scene = gs.Scene(
        rigid_options=gs.options.RigidOptions(
            # constraint_solver=gs.constraint_solver.Newton,
        ),
        viewer_options=gs.options.ViewerOptions(
            camera_pos=(3.5, 0.0, 2.5),
            camera_lookat=(0.0, 0.0, 0.5),
            camera_fov=40,
            max_FPS=60,
        ),
        show_viewer=args.vis,
    )

    ########################## entities ##########################
    plane = scene.add_entity(
        gs.morphs.Plane(),
    )
    franka = scene.add_entity(
        gs.morphs.MJCF(
            file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/panda.xml"),
            requires_jac_and_IK=True,  # needed for get_jacobian / null-space motion
        ),
        visualize_contact=True,
    )
    # Free cuboid – positioned between fingers after FK; size matches grasped_cuboid in the original MJCF
    cuboid = scene.add_entity(
        gs.morphs.Box(size=(0.025, 0.025, 0.15)),
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
    joints_name = (
        "joint1",
        "joint2",
        "joint3",
        "joint4",
        "joint5",
        "joint6",
        "joint7",
        "finger_joint1",
        "finger_joint2",
    )
    motors_dof_idx = [franka.get_joint(name).dofs_idx_local[0] for name in joints_name]

    franka.set_dofs_kp(
        np.array([4500, 4500, 3500, 3500, 2000, 2000, 2000, 100, 100]),
        motors_dof_idx,
    )
    franka.set_dofs_kv(
        np.array([450, 450, 350, 350, 200, 200, 200, 10, 10]),
        motors_dof_idx,
    )
    franka.set_dofs_force_range(
        np.array([-87, -87, -87, -87, -12, -12, -12, -100, -100]),
        np.array([87, 87, 87, 87, 12, 12, 12, 100, 100]),
        motors_dof_idx,
    )

    # q_home = np.array([0.0, -0.82, 0.0, -2.180, 0.0, 2.9, 0.78, 0.00511, 0.0051])
    # q_home = np.array([-0.864, -1.469, 0.381, -2.481, 1.606, 1.898, -0.205, 0.00511, 0.0051])

    # q_home = np.array([0.1458, -1.1419,  0.2159, -2.557,   0.8126,  2.761,   0.1316, 0.00511, 0.0051])
    q_home = np.array([0.146, -1.421, 0.460, -2.562, 1.197, 2.760, 0.132, 0.00511, 0.0051])
    # q_home = np.array([0.0, 0.161, 0.0, -1.634, -0.095, 3.321, 0.780, 0.00511, 0.0051])
    # q_home = np.array([0.7419,  0.0561, -0.7045, -0.7431, -1.4344,  1.9995, -1.4019, 0.00511, 0.0051])
    # q_home = np.array([-0.5369, -1.2342,  0.2409, -1.9031,  1.2753,  1.948,   0.2685, 0.00511, 0.0051])
    # q_home = np.array([-0.7104, -1.1092,  0.942,  -1.8806, -0.4216,  1.855,  -1.1006, 0.00511, 0.0051])
    # q_home = np.array([-0.3853, -0.8473,  0.2445, -2.1327,  0.7237,  2.8,     0.2657, 0.00511, 0.0051])
    franka.set_dofs_position(q_home, motors_dof_idx)
    franka.control_dofs_position(q_home, motors_dof_idx)

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

    # ── NULL-SPACE TRAJECTORY DEMO ───────────────────────────────────────────
    # When --null-traj is given the arm moves from q_home to q2 while the EE
    # Cartesian pose is kept fixed by projecting each step into the Jacobian
    # null-space.  Replace q2 with any other IK-solution for the same EE pose.
    if args.null_traj:
        ee_link = franka.get_link("hand")

        # ── Provide a second joint configuration here with identical FK ──────
        # Tip: find q2 via franka.inverse_kinematics(link=ee_link, pos=...,
        #      quat=..., init_qpos=<different seed>) after settling at q_home.
        # The example below uses the same q_home as both endpoints so you can
        # substitute a real alternative solution.
        # q2 = np.array([0.1458, -1.1419,  0.2159, -2.557,   0.8126,  2.761,   0.1316, 0.00511, 0.0051])  # identical to q_home for demonstration
        q2 = np.array([0.4499, -1.3096,  0.358,  -2.6437,  0.5923,  3.0015,  0.5502, 0.00511, 0.0051])
        # q2 = np.array([-0.1275,  0.1762,  0.0385, -1.5602, -0.7315,  3.3007,  1.4008,  0.00511,  0.0051])                                               # fingers
        # ─────────────────────────────────────────────────────────────────────

        # Safety guard: confirm both configs share the same EE Cartesian pose.
        assert_same_ee_pose(franka, ee_link, motors_dof_idx, q_home, q2)

        q_arm_current = q_home[:7].copy()
        q_arm_goal    = q2[:7].copy()
        fingers       = q_home[7:].copy()

        for step in range(5000):
            q_arm_current = null_space_step(
                franka, ee_link, q_arm_current, q_arm_goal,
                step_size=0.02, damping=1e-3,
            )
            command_q = np.concatenate([q_arm_current, fingers]).astype(gs.np_float)
            franka.control_dofs_position(command_q, motors_dof_idx)
            scene.step()
            if args.vis and not scene.viewer.is_alive():
                break
            if "PYTEST_VERSION" in os.environ:
                break
        return
    # ─────────────────────────────────────────────────────────────────────────

    if not args.vis:
        for _ in range(1000):
            scene.step()
            # cam_0.render()
        return

    joint_lower, joint_upper = franka.get_dofs_limit(motors_dof_idx)
    joint_lower = joint_lower.cpu().numpy().reshape(-1)
    joint_upper = joint_upper.cpu().numpy().reshape(-1)
    joint_lower = np.where(np.isfinite(joint_lower), joint_lower, -np.pi)
    joint_upper = np.where(np.isfinite(joint_upper), joint_upper, np.pi)

    manager = multiprocessing.Manager()
    target_q = manager.list(q_home.astype(float).tolist())
    current_q = manager.list(q_home.astype(float).tolist())
    snap_to_target = multiprocessing.Event()
    stop_event = multiprocessing.Event()
    joint_limits = np.stack((joint_lower, joint_upper), axis=1).astype(float).tolist()
    q_home_list = q_home.astype(float).tolist()

    gui_process = multiprocessing.Process(
        target=_start_joint_gui,
        args=(joints_name, joint_limits, q_home_list, target_q, current_q, snap_to_target, stop_event),
        daemon=True,
    )
    gui_process.start()

    while not stop_event.is_set() and scene.viewer.is_alive():
        command_q = np.array(target_q[:], dtype=gs.np_float)
        should_snap = snap_to_target.is_set()
        if should_snap:
            snap_to_target.clear()

        franka.control_dofs_position(command_q, motors_dof_idx)
        if should_snap:
            franka.set_dofs_position(command_q, motors_dof_idx)
            cuboid.set_pos(cuboid_home_pos, zero_velocity=True)
            cuboid.set_quat(cuboid_home_quat, zero_velocity=True)
            scene.visualizer.update(force=True)

        scene.step()
        actual_q = franka.get_dofs_position(motors_dof_idx).cpu().numpy().reshape(-1)
        current_q[:] = actual_q.astype(float).tolist()

        # cam_0.render()
        if "PYTEST_VERSION" in os.environ:
            break
        time.sleep(0.01)

    stop_event.set()
    gui_process.join(timeout=1.0)


if __name__ == "__main__":
    main()
