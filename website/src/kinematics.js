import { Object3D, Quaternion, Vector3 } from 'three';

// Reference point on the gripper axis, level with the finger tips: the finger joint
// origin (0.0584 m along panda_hand +z) plus the finger mesh length (0.0539 m).
// panda_grasptarget sits 7.3 mm behind it, between the fingers rather than at their tips.
export const FINGERTIP_OFFSET = 0.1123;

// Every Cartesian target is expressed at this frame, and commanded rotations turn
// about it. It keeps panda_hand's orientation, so +z points out of the fingers and
// +y follows the finger travel.
export function createToolFrame(robot) {
  const tool = new Object3D();
  tool.name = 'panda_fingertip';
  tool.position.set(0, 0, FINGERTIP_OFFSET);
  robot.links.panda_hand.add(tool);
  robot.updateMatrixWorld(true);
  return tool;
}

// Damped least squares on the URDF's geometric Jacobian. Units are metres/radians.
// Solves from the current pose; a failed solve restores it rather than moving partway.
export function createCartesianSolver(robot, tool) {
  const joints = Array.from({ length: 7 }, (_, i) => robot.joints[`panda_joint${i + 1}`]);
  const position = new Vector3();
  const rotation = new Quaternion();
  const orientationWeight = 0.35;
  const setAngles = (values) => {
    joints.forEach((joint, i) => joint.setJointValue(values[i]));
    robot.updateMatrixWorld(true);
  };

  function error(target, orientation) {
    tool.getWorldPosition(position);
    tool.getWorldQuaternion(rotation);
    const translation = target.clone().sub(position);
    const angular = new Vector3();
    if (orientation) {
      const delta = orientation.clone().multiply(rotation.clone().invert()).normalize();
      if (delta.w < 0) delta.set(-delta.x, -delta.y, -delta.z, -delta.w);
      const sine = Math.hypot(delta.x, delta.y, delta.z);
      if (sine > 1e-10) angular.set(delta.x, delta.y, delta.z).multiplyScalar(2 * Math.atan2(sine, delta.w) / sine);
    }
    const vector = [...translation.toArray(), ...angular.multiplyScalar(orientationWeight).toArray()];
    return {
      vector,
      cost: vector.reduce((sum, value) => sum + value * value, 0),
      positionError: translation.length(),
      orientationError: angular.length() / orientationWeight,
    };
  }

  return function solve(target, orientation = null) {
    if (![...target.toArray(), ...(orientation?.toArray() || [])].every(Number.isFinite)) {
      return { success: false, positionError: Infinity };
    }
    const original = joints.map((joint) => joint.angle);
    setAngles(original);
    let current = error(target, orientation);
    for (let iteration = 0; iteration < 100; iteration++) {
      if (current.positionError < 0.0005 && current.orientationError < 0.003) {
        return { success: true, positionError: current.positionError };
      }
      const columns = joints.map((joint) => {
        const axis = joint.axis.clone().transformDirection(joint.matrixWorld);
        const origin = new Vector3().setFromMatrixPosition(joint.matrixWorld);
        const linear = axis.clone().cross(position.clone().sub(origin));
        const angular = axis.multiplyScalar(orientation ? orientationWeight : 0);
        return [...linear.toArray(), ...angular.toArray()];
      });
      // (J J^T + lambda^2 I) y = error; dq = J^T y.
      const matrix = Array.from({ length: 6 }, (_, row) => Array.from({ length: 6 }, (_, col) =>
        columns.reduce((sum, column) => sum + column[row] * column[col], 0) + (row === col ? 0.0004 : 0),
      ));
      const y = solveLinear(matrix, current.vector);
      const step = columns.map((column) => column.reduce((sum, value, i) => sum + value * y[i], 0));
      const maxStep = Math.max(0.12, ...step.map(Math.abs));
      const oldAngles = joints.map((joint) => joint.angle);
      let improved = false;
      for (const scale of [1, 0.5, 0.25, 0.125]) {
        setAngles(oldAngles.map((angle, i) => angle + step[i] * 0.12 / maxStep * scale));
        const candidate = error(target, orientation);
        if (candidate.cost < current.cost - 1e-12) {
          current = candidate;
          improved = true;
          break;
        }
      }
      if (!improved) break;
    }
    setAngles(original);
    return { success: false, positionError: current.positionError };
  };
}

function solveLinear(matrix, values) {
  const rows = matrix.map((row, i) => [...row, values[i]]);
  for (let col = 0; col < rows.length; col++) {
    let pivot = col;
    for (let row = col + 1; row < rows.length; row++) {
      if (Math.abs(rows[row][col]) > Math.abs(rows[pivot][col])) pivot = row;
    }
    [rows[col], rows[pivot]] = [rows[pivot], rows[col]];
    const divisor = rows[col][col];
    for (let k = col; k <= rows.length; k++) rows[col][k] /= divisor;
    for (let row = 0; row < rows.length; row++) {
      if (row === col) continue;
      const factor = rows[row][col];
      for (let k = col; k <= rows.length; k++) rows[row][k] -= factor * rows[col][k];
    }
  }
  return rows.map((row) => row[rows.length]);
}
