"""
Compute and *optimise* the Franka Panda Z-axis end-effector manipulability.

Two modes
---------
  (default)   Evaluate metrics at a given joint configuration.
  --optimize           Find the joint configuration that maximises Z acceleration while
                       keeping the end-effector at *exactly the same* Cartesian pose
                       (position + orientation) as the starting config.
  --optimize --velocity  Same but maximises Z velocity instead of Z acceleration.

Metrics reported
----------------
  w_z        L2-norm manipulability: ||J_z||   (unit-sphere, geometry only)
  v_z_max    Max EE Z velocity  [m/s]  (velocity-limited LP, closed form)
  a_z_max    Max EE Z accel     [m/s²] (torque-limited LP, closed form)

Optimisation algorithm: null-space projected gradient ascent
-------------------------------------------------------------
  The 7-DOF arm leaves a 1-D null space when controlling the 6-D EE pose.
  At each step:
    1. Estimate  d(metric)/dq  by central finite differences.
    2. Project gradient onto  null(J) = span(I - J_pinv @ J).
    3. Take a step along that direction (no EE motion to first order).
    4. Run a short IK pass to cancel the second-order EE drift.
    5. Accept if a_z improved; otherwise shrink step size.

Usage
-----
    # evaluate home pose
    python examples/rigid/franka_z_manipulability.py --cpu

    # optimise Z acceleration from home pose (50 steps)
    python examples/rigid/franka_z_manipulability.py --cpu --optimize

    # optimise Z velocity instead
    python examples/rigid/franka_z_manipulability.py --cpu --optimize --velocity

    # optimise with custom starting config
    python examples/rigid/franka_z_manipulability.py --cpu --optimize \\
        --joint 0.5 -1.0 0.2 -2.0 0.1 2.5 0.9
"""

import argparse
from pathlib import Path

import numpy as np

import genesis as gs

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── Robot constants ──────────────────────────────────────────────────────────
ARM_JOINT_NAMES = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint7")
TAU_LIMITS = np.array([87.0, 87.0, 87.0, 87.0, 12.0, 12.0, 12.0])   # N·m
VEL_LIMITS = np.array([2.175, 2.175, 2.175, 2.175, 2.610, 2.610, 2.610])  # rad/s
# All named configurations to sweep with --all-configs
NAMED_CONFIGS = {
    "default":  np.array([ 0.0,    -0.82,   0.0,    -2.180,  0.0,     2.9,    0.78  ]),
    "config_1": np.array([-0.5369, -1.2342,  0.2409, -1.9031,  1.2753,  1.948,  0.2685]),
    "config_2": np.array([-0.3853, -0.8473,  0.2445, -2.1327,  0.7237,  2.8,    0.2657]),
    "config_3": np.array([-0.8644, -1.2959,  0.1575, -1.8924,  1.5296,  1.677,  0.2299]),
    "config_4": np.array([-0.3853, -1.130,   0.2445, -2.362,   0.7237,  2.8,    0.2657]),
    "config_5": np.array([-0.864,  -1.301,   0.8888, -1.895,  -0.173,   1.918, -1.275 ]),
    "config_6": np.array([0.0, 0.161, 0.0, -1.634, -0.095, 3.321, 0.780]),
    "config_7": np.array([0.1458, -1.1419,  0.2159, -2.557,   0.8126,  2.761,   0.1316]),
    "config_8": np.array([0.146, -1.421, 0.460, -2.562, 1.197, 2.760, 0.132]),
    "config_9": np.array([0.4499, -1.3096,  0.358,  -2.6437,  0.5923,  3.0015,  0.5502]),
}
Q_HOME = NAMED_CONFIGS["config_8"]   # active single-config default


# ── Internal helpers ─────────────────────────────────────────────────────────

def _recompute_mass_mat(solver):
    """Recompute mass matrix in-place after set_dofs_position (no scene.step needed)."""
    from genesis.engine.solvers.rigid.abd.forward_dynamics import kernel_compute_mass_matrix
    kernel_compute_mass_matrix(
        links_state=solver.links_state,
        links_info=solver.links_info,
        dofs_state=solver.dofs_state,
        dofs_info=solver.dofs_info,
        entities_info=solver.entities_info,
        rigid_global_info=solver._rigid_global_info,
        static_rigid_sim_config=solver._static_rigid_sim_config,
        decompose=False,
    )


def apply_config(franka, q_arm, all_dof_idx):
    """Set arm joints (fingers parked at 0), propagate FK, recompute mass matrix."""
    q_full = np.append(q_arm, [0.0, 0.0]).astype(gs.np_float)
    franka.set_dofs_position(q_full, all_dof_idx)   # runs FK internally
    _recompute_mass_mat(franka._solver)


def get_metrics(franka, ee_link, arm_cols, tau_lim, vel_lim):
    """
    Return all Z-manipulability metrics at the current configuration.
    apply_config() must be called first.
    """
    J_np  = franka.get_jacobian(ee_link).cpu().numpy()     # (6, n_entity_dofs)
    J_arm = J_np[:, arm_cols]                              # (6, 7) full arm block
    J_z   = J_arm[2, :]                                   # (7,)   vz row

    M_np  = franka.get_mass_mat().cpu().numpy()            # (n_entity_dofs, n_entity_dofs)
    M_arm = M_np[np.ix_(arm_cols, arm_cols)]               # (7, 7)

    M_inv = np.linalg.inv(M_arm)
    f     = M_inv @ J_z                                    # LP coefficients for accel

    a_z = float(np.sum(np.abs(f)   * tau_lim))
    v_z = float(np.sum(np.abs(J_z) * vel_lim))
    w_z = float(np.linalg.norm(J_z))

    # ── Velocity ellipsoid (Yoshikawa 1984) ──────────────────────────────────
    # W_v = J J^T  ->  w_v = sqrt(det(W_v))
    # Singular values of J give the ellipsoid semi-axes.
    JJT       = J_arm @ J_arm.T                            # (6, 6)
    sv_v      = np.linalg.svd(J_arm, compute_uv=False)    # descending
    w_classic = float(np.sqrt(max(0.0, np.linalg.det(JJT))))
    cond_v    = float(sv_v[0] / sv_v[-1]) if sv_v[-1] > 1e-12 else float("inf")

    # ── Dynamic / acceleration ellipsoid (Yoshikawa 1985) ───────────────────
    # The set of EE accelerations for unit-norm torques: ddx = J M^{-1} tau
    # W_a = (J M^{-1})(J M^{-1})^T  ->  w_a = sqrt(det(W_a))
    A         = J_arm @ M_inv                              # (6, 7)  = J M^{-1}
    AAT       = A @ A.T                                    # (6, 6)
    sv_a      = np.linalg.svd(A, compute_uv=False)
    w_dynamic = float(np.sqrt(max(0.0, np.linalg.det(AAT))))
    cond_a    = float(sv_a[0] / sv_a[-1]) if sv_a[-1] > 1e-12 else float("inf")

    return dict(
        a_z=a_z, v_z=v_z, w_z=w_z,
        w_classic=w_classic, cond_v=cond_v,
        w_dynamic=w_dynamic, cond_a=cond_a,
        sv_v=sv_v, sv_a=sv_a,
        J_z=J_z, J_arm=J_arm, A=A, f=f, M_arm=M_arm,
        dq_opt =np.sign(J_z) * vel_lim,
        tau_opt=np.sign(f)   * tau_lim,
    )


# ── Optimiser ────────────────────────────────────────────────────────────────

def optimize_for_z(
    franka, ee_link,
    arm_local_idx, arm_cols, all_dof_idx,
    tau_lim, vel_lim,
    target_pos, target_quat,
    q_init,
    metric="a_z",
    n_steps=50,
    step_size=0.10,
    fd_eps=1e-4,
    ik_max_samples=5,
    ik_max_iters=30,
):
    """
    Null-space projected gradient ascent to maximise the chosen metric
    ("a_z" for Z acceleration, "v_z" for Z velocity) while preserving
    the EE Cartesian pose (position + orientation).

    Returns (best_q_arm, best_value, history)
    """
    import torch
    target_pos_t  = torch.tensor(target_pos,  dtype=gs.tc_float, device=gs.device)
    target_quat_t = torch.tensor(target_quat, dtype=gs.tc_float, device=gs.device)

    q_cur = q_init.copy()
    apply_config(franka, q_cur, all_dof_idx)
    best_val = get_metrics(franka, ee_link, arm_cols, tau_lim, vel_lim)[metric]
    best_q   = q_cur.copy()
    history  = [(0, best_val)]

    consecutive_shrinks = 0

    for step in range(1, n_steps + 1):
        # ── 1. Metrics and Jacobian at current config ────────────────────────
        apply_config(franka, q_cur, all_dof_idx)
        m_cur  = get_metrics(franka, ee_link, arm_cols, tau_lim, vel_lim)
        J_arm  = franka.get_jacobian(ee_link).cpu().numpy()[:, arm_cols]  # (6, 7)

        # ── 2. Central finite-difference gradient  d(metric) / dq ───────────
        grad = np.zeros(7)
        for i in range(7):
            dq = np.zeros(7); dq[i] = fd_eps
            apply_config(franka, q_cur + dq, all_dof_idx)
            val_p = get_metrics(franka, ee_link, arm_cols, tau_lim, vel_lim)[metric]
            apply_config(franka, q_cur - dq, all_dof_idx)
            val_m = get_metrics(franka, ee_link, arm_cols, tau_lim, vel_lim)[metric]
            grad[i] = (val_p - val_m) / (2 * fd_eps)

        # ── 3. Project onto null(J)  ─────────────────────────────────────────
        J_pinv  = np.linalg.pinv(J_arm)        # (7, 6)
        N       = np.eye(7) - J_pinv @ J_arm   # (7, 7) null-space projector
        grad_n  = N @ grad                     # component in null space

        norm_g = np.linalg.norm(grad_n)
        if norm_g < 1e-9:
            print(f"  [step {step:3d}]  null-space gradient vanished — converged.")
            break

        # ── 4. Step in null space + IK correction ────────────────────────────
        q_candidate = q_cur + step_size * (grad_n / norm_g)
        q_init_ik   = np.append(q_candidate, [0.0, 0.0]).astype(gs.np_float)

        q_ik = franka.inverse_kinematics(
            link=ee_link,
            pos=target_pos_t,
            quat=target_quat_t,
            init_qpos=q_init_ik,
            dofs_idx_local=arm_local_idx,
            max_samples=ik_max_samples,
            max_solver_iters=ik_max_iters,
            pos_tol=1e-4,
            rot_tol=2e-3,
        )
        q_new = q_ik.cpu().numpy()[arm_local_idx]

        # ── 5. Accept / shrink ───────────────────────────────────────────────
        apply_config(franka, q_new, all_dof_idx)
        val_new = get_metrics(franka, ee_link, arm_cols, tau_lim, vel_lim)[metric]

        if val_new >= m_cur[metric]:
            q_cur = q_new
            consecutive_shrinks = 0
        else:
            step_size *= 0.8
            consecutive_shrinks += 1

        if val_new > best_val:
            best_val = val_new
            best_q   = q_new.copy()

        history.append((step, val_new))

        if step % 10 == 0 or step == n_steps:
            print(
                f"  [step {step:3d}]  {metric} = {val_new:.4f}  "
                f"best = {best_val:.4f}  alpha = {step_size:.5f}"
            )

        if consecutive_shrinks >= 8:
            print(f"  [step {step:3d}]  step size too small — stopping early.")
            break

    return best_q, best_val, history


# ── Reporting ────────────────────────────────────────────────────────────────

def print_metrics(label, q_arm, m):
    np.set_printoptions(precision=4, suppress=True)
    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    print(f"  Joint config (arm)   : {np.round(q_arm, 4)}")
    print()
    print(f"  ── Velocity ellipsoid  (J J^T) ──────────────────────────")
    print(f"     w_vel  = sqrt(det(J J^T))  = {m['w_classic']:.6e}")
    print(f"     cond_v = sv_max/sv_min     = {m['cond_v']:.4f}")
    print(f"     sing. values of J          : {np.round(m['sv_v'], 5)}")
    print()
    print(f"  ── Acceleration ellipsoid  (J M⁻¹ M⁻ᵀ J^T) ────────────")
    print(f"     w_acc  = sqrt(det(A A^T))  = {m['w_dynamic']:.6e}   [A = J M⁻¹]")
    print(f"     cond_a = sv_max/sv_min     = {m['cond_a']:.4f}")
    print(f"     sing. values of J M⁻¹      : {np.round(m['sv_a'], 5)}")
    print()
    print(f"  ── Directional (Z-axis) ─────────────────────────────────")
    print(f"     w_z  = ||J_z||    = {m['w_z']:.6f}   (geometry only)")
    print(f"     v_z  = LP max vel = {m['v_z']:.6f}   m/s")
    print(f"     a_z  = LP max acc = {m['a_z']:.6f}   m/s^2")
    print()
    print(f"  Optimal joint vel  dq*  [rad/s] : {np.round(m['dq_opt'],  4)}")
    print(f"  Optimal joint torq tau* [N.m]   : {np.round(m['tau_opt'], 4)}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Franka Z-axis manipulability: evaluate and/or optimise."
    )
    parser.add_argument("--cpu",       action="store_true",
                        help="Use CPU backend.")
    parser.add_argument("--joint",     nargs=7, type=float, metavar="RAD",
                        default=None,
                        help="Starting arm joint angles [rad]. Default: home pose.")
    parser.add_argument("--ee-link",   default="hand",
                        help="End-effector link name (default: hand).")
    parser.add_argument("--optimize",  action="store_true",
                        help="Run null-space gradient ascent for max Z acceleration (or velocity with --velocity).")
    parser.add_argument("--velocity",    action="store_true",
                        help="When combined with --optimize, maximise Z velocity instead of Z acceleration.")
    parser.add_argument("--all-configs", action="store_true",
                        help="Evaluate all NAMED_CONFIGS defined in the file and print a comparison table.")
    parser.add_argument("--steps",     type=int,   default=50,
                        help="Optimisation steps (default: 50).")
    parser.add_argument("--step-size", type=float, default=0.10,
                        help="Initial null-space step size [rad] (default: 0.10).")
    args = parser.parse_args()

    q_start = np.array(args.joint) if args.joint is not None else Q_HOME.copy()

    # ── Genesis setup ─────────────────────────────────────────────────────────
    gs.init(backend=gs.cpu if args.cpu else gs.gpu)
    scene  = gs.Scene(show_viewer=False)
    franka = scene.add_entity(
        gs.morphs.MJCF(
            file=str(REPO_ROOT / "genesis/assets/xml/franka_emika_panda/panda.xml"),
            requires_jac_and_IK=True,
        ),
    )
    scene.build()

    all_joint_names = ARM_JOINT_NAMES + ("finger_joint1", "finger_joint2")
    all_dof_idx   = [franka.get_joint(n).dofs_idx_local[0] for n in all_joint_names]
    arm_local_idx = [franka.get_joint(n).dofs_idx_local[0] for n in ARM_JOINT_NAMES]
    # arm_cols: indices into entity-local mass-matrix / Jacobian (shape (9,9) / (6,9))
    arm_cols = np.array(arm_local_idx)

    ee_link = franka.get_link(args.ee_link)

    # ── Sweep all named configs ───────────────────────────────────────────────
    if args.all_configs:
        hdr = f"  {'Config':<12} {'w_vel':>12} {'cond_vel':>10} {'w_acc':>12} {'cond_acc':>10} {'w_z':>10} {'v_z m/s':>10} {'a_z m/s²':>11}"
        sep = f"  {'-'*12} {'-'*12} {'-'*10} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*11}"
        print(f"\n{'='*len(hdr.rstrip())}")
        print(f"  Velocity ellipsoid : w_vel = sqrt(det(J J^T)),   cond_vel = sv_max/sv_min of J")
        print(f"  Accel ellipsoid    : w_acc = sqrt(det(A A^T)),   cond_acc = sv_max/sv_min of A=JM⁻¹")
        print(f"{'='*len(hdr.rstrip())}")
        print(hdr)
        print(sep)
        for name, q_cfg in NAMED_CONFIGS.items():
            apply_config(franka, q_cfg, all_dof_idx)
            m = get_metrics(franka, ee_link, arm_cols, TAU_LIMITS, VEL_LIMITS)
            print(
                f"  {name:<12} "
                f"{m['w_classic']:>12.4e} {m['cond_v']:>10.3f} "
                f"{m['w_dynamic']:>12.4e} {m['cond_a']:>10.3f} "
                f"{m['w_z']:>10.4f} {m['v_z']:>10.4f} {m['a_z']:>11.4f}"
            )
        print(f"{'='*len(hdr.rstrip())}")
        return

    # ── Evaluate starting config ──────────────────────────────────────────────
    apply_config(franka, q_start, all_dof_idx)
    m_start = get_metrics(franka, ee_link, arm_cols, TAU_LIMITS, VEL_LIMITS)
    print_metrics("Start config" + (" (home)" if args.joint is None else ""), q_start, m_start)

    if not args.optimize:
        return

    metric     = "v_z" if args.velocity else "a_z"
    metric_lbl = "Z velocity [m/s]" if args.velocity else "Z accel [m/s^2]"
    opt_lbl    = "max Z velocity" if args.velocity else "max Z accel"

    # ── Capture target EE pose from FK at start config ────────────────────────
    target_pos  = ee_link.get_pos().cpu().numpy().flatten()
    target_quat = ee_link.get_quat().cpu().numpy().flatten()
    print(f"\n  Optimising for       : {opt_lbl}")
    print(f"  Target EE position   : {np.round(target_pos,  4)}")
    print(f"  Target EE quaternion : {np.round(target_quat, 4)}")
    print(f"\n  Running null-space gradient ascent "
          f"({args.steps} steps, step_size0 = {args.step_size:.3f} rad) ...\n")

    q_opt, val_opt, history = optimize_for_z(
        franka, ee_link,
        arm_local_idx, arm_cols, all_dof_idx,
        TAU_LIMITS, VEL_LIMITS,
        target_pos, target_quat,
        q_init=q_start,
        metric=metric,
        n_steps=args.steps,
        step_size=args.step_size,
    )

    # ── Report optimised result ───────────────────────────────────────────────
    apply_config(franka, q_opt, all_dof_idx)
    m_opt = get_metrics(franka, ee_link, arm_cols, TAU_LIMITS, VEL_LIMITS)
    print_metrics(f"Optimised config ({opt_lbl}, same EE pose)", q_opt, m_opt)

    # Verify EE pose was preserved
    ee_pos_f  = ee_link.get_pos().cpu().numpy().flatten()
    ee_quat_f = ee_link.get_quat().cpu().numpy().flatten()
    pos_err_mm  = float(np.linalg.norm(ee_pos_f - target_pos)) * 1000
    dot         = float(np.clip(abs(np.dot(ee_quat_f, target_quat)), 0.0, 1.0))
    rot_err_deg = float(np.degrees(2 * np.arccos(dot)))

    gain = (m_opt[metric] - m_start[metric]) / max(abs(m_start[metric]), 1e-9) * 100

    _POS_TOL_MM  = 0.5    # 0.5 mm
    _ROT_TOL_DEG = 0.28   # ~5e-3 rad

    pose_ok = pos_err_mm <= _POS_TOL_MM and rot_err_deg <= _ROT_TOL_DEG
    pose_status = "PASS" if pose_ok else "FAIL"

    np.set_printoptions(precision=4, suppress=True)
    print(f"\n{'='*65}")
    print(f"  Joint config comparison")
    print(f"{'='*65}")
    print(f"  Initial Q (arm) : {np.round(q_start, 4)}")
    print(f"  Final   Q (arm) : {np.round(q_opt,   4)}")
    print(f"  Delta   Q       : {np.round(q_opt - q_start, 4)}")
    print(f"\n  EE pose preservation error  [{pose_status}]:")
    print(f"    Position    : {pos_err_mm:.3f} mm   (tol {_POS_TOL_MM:.1f} mm)")
    print(f"    Orientation : {rot_err_deg:.4f} deg  (tol {_ROT_TOL_DEG:.2f} deg)")
    print(f"\n  {metric_lbl} improvement : {m_start[metric]:.4f} -> {m_opt[metric]:.4f}"
          f"   ({gain:+.1f}%)")

    if not pose_ok:
        raise ValueError(
            f"Optimised joint config does NOT preserve the target EE pose: "
            f"pos_err={pos_err_mm:.3f} mm (tol {_POS_TOL_MM} mm), "
            f"rot_err={rot_err_deg:.4f} deg (tol {_ROT_TOL_DEG} deg)"
        )


if __name__ == "__main__":
    main()
