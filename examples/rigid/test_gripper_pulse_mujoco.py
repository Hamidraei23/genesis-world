"""
Interactive MuJoCo gripper pulse test.

The arm holds still at the home position. Press SPACE in the MuJoCo viewer
to trigger a gripper open→close pulse.

Usage:
    python3 examples/rigid/test_gripper_pulse_mujoco.py
"""

import time
import threading
import numpy as np
import mujoco
import mujoco.viewer

try:
    from .env_franka_mujoco import FrankaMuJoCoEnv
except ImportError:
    from env_franka_mujoco import FrankaMuJoCoEnv


# ── Pulse state machine ──────────────────────────────────────────────
PULSE_DELAY_STEPS = 1
PULSE_START = 6 + PULSE_DELAY_STEPS

# Shared flag set by the viewer's key callback
_space_pressed = False
_lock = threading.Lock()


def _key_callback(key):
    """Called by the MuJoCo viewer on any keypress."""
    global _space_pressed
    # MuJoCo key codes: space = 32 (ASCII)
    if key == 32:
        with _lock:
            _space_pressed = True
        print("\n>>> SPACE pressed! <<<", flush=True)


def main():
    # Create env WITHOUT its own viewer (vis=False)
    env = FrankaMuJoCoEnv(
        vis=False,
        dt=0.001,
        target_dt=0.02,
        playback_speed=0.5,
        gravity_compensation=True,
    )

    # Launch our own viewer with the key callback
    viewer = mujoco.viewer.launch_passive(
        env.model, env.data, key_callback=_key_callback
    )

    gripper_min = env.gripper_pos_min
    gripper_max = env.gripper_pos_max
    pulse_counter = 0

    print("=" * 60)
    print("Gripper Pulse Test")
    print("  Press SPACE in the MuJoCo viewer window to trigger a pulse.")
    print("  The gripper will open for 5 steps, then snap closed.")
    print("  Close the viewer window to quit.")
    print("=" * 60)

    global _space_pressed
    t_wall_start = time.perf_counter()
    sim_step = 0
    render_every = env.render_every

    try:
        while viewer.is_running():
            # ── Check for space trigger ──
            with _lock:
                triggered = _space_pressed and pulse_counter == 0
                if triggered:
                    _space_pressed = False
            if triggered:
                pulse_counter = PULSE_START
                print(f"[step {sim_step:6d}] PULSE triggered! "
                      f"({PULSE_DELAY_STEPS} delay + 5 open + 1 close)")

            # ── Determine gripper position ──
            if pulse_counter > 6:
                gripper_pos = gripper_min.copy()
                phase = "delay"
            elif 2 <= pulse_counter <= 6:
                gripper_pos = gripper_max.copy()
                phase = "OPEN"
            elif pulse_counter == 1:
                gripper_pos = gripper_min.copy()
                phase = "CLOSE"
            else:
                gripper_pos = gripper_min.copy()
                phase = "idle"

            # ── Force extraction ──
            left_f, right_f = env.get_finger_net_contact_forces()
            lf = np.linalg.norm(left_f)
            rf = np.linalg.norm(right_f)
            ft_dist = env.get_fingertip_distance()

            if pulse_counter > 0:
                print(f"  [{phase:>5s}] counter={pulse_counter:2d}  "
                      f"grip=[{gripper_pos[0]:.6f}, {gripper_pos[1]:.6f}]  "
                      f"|lf|={lf:.3f}  |rf|={rf:.3f}  ft_dist={ft_dist:.4f}")
                pulse_counter = max(0, pulse_counter - 1)
            else:
                print(f"  [ idle]              "
                      f"grip=[{gripper_pos[0]:.6f}, {gripper_pos[1]:.6f}]  "
                      f"|lf|={lf:.3f}  |rf|={rf:.3f}  ft_dist={ft_dist:.4f}")

            # ── Send commands ──
            env.data.ctrl[env.finger_actuators] = gripper_pos
            env.data.ctrl[env.arm_actuators] = env.q_home[:7]

            for _ in range(env.target_update_every):
                env._apply_arm_gravity_compensation()
                mujoco.mj_step(env.model, env.data)
                sim_step += 1
                if sim_step % render_every == 0:
                    viewer.sync()

            # Playback pacing
            t_sim = sim_step * env.dt
            t_wall = time.perf_counter() - t_wall_start
            if env.playback_speed and t_wall < t_sim / env.playback_speed:
                time.sleep(t_sim / env.playback_speed - t_wall)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        viewer.close()


if __name__ == "__main__":
    main()
