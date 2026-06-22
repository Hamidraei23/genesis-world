"""
Interactive Genesis gripper pulse test.

The arm holds still at the home position. Press SPACE in the Genesis viewer window
to trigger a gripper open→close pulse.

Usage:
    python3 examples/rigid/test_gripper_pulse_genesis.py
"""

import time
import threading
import torch
import genesis as gs
import genesis.vis.keybindings as keybindings

try:
    from .env_franka_parallel import FrankaEnvParallel
except ImportError:
    from env_franka_parallel import FrankaEnvParallel


_space_pressed = False
_lock = threading.Lock()

def _on_space_pressed():
    global _space_pressed
    with _lock:
        _space_pressed = True
    print("\n>>> SPACE pressed! <<<", flush=True)


def main():
    # Initialize Genesis for GPU
    gs.init(backend=gs.gpu)

    # Create a single environment
    env = FrankaEnvParallel(
        num_envs=1,
        vis=True,
    )

    # Register space bar callback with Genesis viewer
    keybind = keybindings.Keybind(
        name="trigger_pulse",
        key=keybindings.Key.SPACE,
        key_action=keybindings.KeyAction.PRESS,
        callback=_on_space_pressed,
    )
    env.scene.viewer.register_keybinds(keybind)

    print("=" * 60)
    print("Gripper Pulse Test (Genesis GPU)")
    print("  Press SPACE in the Genesis viewer window to trigger a pulse.")
    print("  The gripper will open for 5 steps, then snap closed.")
    print("  Close the viewer window to quit.")
    print("=" * 60)

    global _space_pressed
    
    # We will simulate high-level policy steps here.
    # The environment internally takes care of the low-level steps and rendering.

    try:
        while env.scene.viewer.is_alive():
            with _lock:
                triggered = _space_pressed
                if triggered:
                    _space_pressed = False

            # Action: [z_vel, grip_l, grip_r]
            # Hold z_vel at 0.0. 
            # To trigger the pulse logic inside env_franka_parallel.step(), 
            # we just need to send a single "open" command (> 0.0) so it detects a rising edge.
            if triggered:
                grip_cmd = 1.0  # Open (triggers pulse)
            else:
                grip_cmd = -1.0 # Closed (default)

            action = torch.tensor([[0.0, grip_cmd, grip_cmd]], dtype=torch.float32, device=env.device)

            # Step the environment (runs target_update_every sim steps)
            env.step(action)

            # Get raw force magnitudes from the internal unnormalized buffer
            # obs_buf[..., 4] is left_force_mag, obs_buf[..., 5] is right_force_mag
            lf = env.obs_buf[0, 4].item()
            rf = env.obs_buf[0, 5].item()

            # Check pulse state and print forces
            pulse_steps = env._gripper_pulse_steps[0].item()
            if pulse_steps > 0:
                # Determine current phase for printing
                if pulse_steps > 6:
                    phase = "delay"
                elif 2 <= pulse_steps <= 6:
                    phase = "OPEN"
                elif pulse_steps == 1:
                    phase = "CLOSE"
                else:
                    phase = "idle"

                print(f"  [{phase:>5s}] counter={pulse_steps:2d}  |lf|={lf:.3f}  |rf|={rf:.3f}")
            else:
                print(f"  [ idle]              |lf|={lf:.3f}  |rf|={rf:.3f}")

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        env.scene.viewer.stop()


if __name__ == "__main__":
    main()
