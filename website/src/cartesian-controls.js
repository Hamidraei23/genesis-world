import { AxesHelper, Euler, MathUtils, Mesh, MeshBasicMaterial, SphereGeometry } from 'three';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { createCartesianSolver } from './kinematics.js';

// URDF roll/pitch/yaw turns about the fixed base X, then Y, then Z, which three.js
// expresses as the intrinsic 'ZYX' order.
const RPY_ORDER = 'ZYX';
const AXES = ['x', 'y', 'z'];
const ANGLES = ['roll', 'pitch', 'yaw'];

export function createCartesianControls({ robot, tool, scene, camera, renderer, orbit, stopDemo, onPoseChanged, angle }) {
  const $ = (id) => document.getElementById(id);
  const solve = createCartesianSolver(robot, tool);
  // The commanded angles, in radians. They stay authoritative between moves: reading
  // them back from the quaternion would flip branches whenever a step crosses the
  // pitch singularity, which the home pose sits close to.
  const command = new Euler(0, 0, 0, RPY_ORDER);
  const target = new Mesh(new SphereGeometry(0.008, 16, 12), new MeshBasicMaterial({ color: 0x71e6e1 }));
  target.add(new AxesHelper(0.075));
  target.visible = false;
  scene.add(target);
  // Mark the reference point on the robot itself, so the pivot is visible next to the target.
  const toolAxes = new AxesHelper(0.05);
  toolAxes.visible = false;
  tool.add(toolAxes);
  const drag = new TransformControls(camera, renderer.domElement);
  drag.setMode('translate');
  drag.setSpace('world');
  drag.setSize(0.65);
  scene.add(drag.getHelper());
  let active = false;

  const holdOrientation = () => $('lock-orientation').checked;

  function writeTarget() {
    for (const axis of AXES) {
      $(`target-${axis}`).value = target.position[axis].toFixed(4);
      $(`target-${axis}`).setCustomValidity('');
    }
    const digits = angle.unit() === 'radians' ? 4 : 2;
    ANGLES.forEach((name, index) => {
      $(`target-${name}`).value = angle.toDisplay(command[AXES[index]]).toFixed(digits);
      $(`target-${name}`).setCustomValidity('');
    });
  }
  // Adopt an orientation that came from somewhere else: the robot, or a gizmo drag.
  function readOrientation() {
    command.setFromQuaternion(target.quaternion, RPY_ORDER);
  }
  function readTarget() {
    const position = AXES.map((axis) => $(`target-${axis}`).valueAsNumber);
    const rpy = ANGLES.map((name) => $(`target-${name}`).valueAsNumber);
    if (!position.every(Number.isFinite)) return false;
    if (holdOrientation() && !rpy.every(Number.isFinite)) return false;
    target.position.fromArray(position);
    if (holdOrientation()) {
      const [roll, pitch, yaw] = rpy.map((value) => angle.toRadians(value));
      target.quaternion.setFromEuler(command.set(roll, pitch, yaw, RPY_ORDER));
    }
    return true;
  }
  function message(text, failed = false) {
    $('ik-status').textContent = text;
    $('ik-status').dataset.error = String(failed);
    target.material.color.setHex(failed ? 0xff8678 : 0x71e6e1);
  }
  function sync() {
    robot.updateMatrixWorld(true);
    tool.getWorldPosition(target.position);
    tool.getWorldQuaternion(target.quaternion);
    readOrientation();
    writeTarget();
    message('Target follows the current fingertip point.');
  }
  function move() {
    stopDemo();
    const result = solve(target.position, holdOrientation() ? target.quaternion : null);
    onPoseChanged();
    if (result.success) {
      message(`Target reached · error ${(result.positionError * 1000).toFixed(2)} mm`);
    } else {
      message('Target not solved within joint limits. Arm unchanged; try a closer target or release orientation.', true);
    }
  }
  // Stepping an angle leaves the target position alone, so the gripper turns about
  // the fingertip point instead of swinging it around.
  function rotateTarget(axis, radians) {
    command[axis] += radians;
    target.quaternion.setFromEuler(command);
    writeTarget();
    move();
  }
  function updateOrientationInputs() {
    const held = holdOrientation();
    for (const name of ANGLES) $(`target-${name}`).disabled = !held;
    $('rot-step').disabled = !held;
    $('drag-mode').querySelector('option[value="rotate"]').disabled = !held;
    if (!held && $('drag-mode').value === 'rotate') $('drag-mode').value = 'translate';
  }
  function updateDrag() {
    target.visible = active;
    toolAxes.visible = active;
    $('drag-mode').disabled = !$('drag-ee').checked;
    drag.setMode($('drag-mode').value);
    if (active && $('drag-ee').checked) drag.attach(target);
    else drag.detach();
  }
  function setDragMode(mode) {
    if (mode === 'rotate' && !holdOrientation()) return;
    $('drag-mode').value = mode;
    updateDrag();
  }
  function refresh() {
    const label = angle.unit() === 'radians' ? 'rad' : '°';
    document.querySelectorAll('[data-angle-unit]').forEach((node) => { node.textContent = label; });
    for (const option of $('rot-step').options) {
      const degrees = Number(option.value);
      option.textContent = angle.unit() === 'radians'
        ? `${MathUtils.degToRad(degrees).toFixed(3)} rad` : `${degrees}°`;
    }
    writeTarget();
  }
  function selectTab(cartesian) {
    active = cartesian;
    for (const [id, selected] of [['joint', !active], ['cartesian', active]]) {
      $(`${id}-tab`).setAttribute('aria-selected', String(selected));
      $(`${id}-tab`).tabIndex = selected ? 0 : -1;
      $(`${id}-panel`).hidden = !selected;
    }
    sync();
    updateOrientationInputs();
    updateDrag();
  }
  for (const id of ['joint', 'cartesian']) {
    $(`${id}-tab`).addEventListener('click', () => selectTab(id === 'cartesian'));
    $(`${id}-tab`).addEventListener('keydown', (event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        selectTab(event.key === 'Home' ? false : event.key === 'End' ? true : !active);
        $(active ? 'cartesian-tab' : 'joint-tab').focus();
      }
    });
  }
  $('drag-ee').addEventListener('change', updateDrag);
  $('drag-mode').addEventListener('change', updateDrag);
  $('lock-orientation').addEventListener('change', () => {
    tool.getWorldQuaternion(target.quaternion);
    readOrientation();
    writeTarget();
    updateOrientationInputs();
    updateDrag();
  });
  // G and R match the gizmo shortcuts used by the three.js editor and Blender.
  addEventListener('keydown', (event) => {
    if (!active || event.ctrlKey || event.metaKey || event.altKey) return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) return;
    if (event.key === 'g' || event.key === 'G') setDragMode('translate');
    if (event.key === 'r' || event.key === 'R') setDragMode('rotate');
  });
  drag.addEventListener('dragging-changed', (event) => {
    orbit.enabled = !event.value;
    if (event.value) {
      stopDemo();
      orbit.autoRotate = false;
      $('rotate').checked = false;
    }
  });
  drag.addEventListener('objectChange', () => { readOrientation(); writeTarget(); move(); });
  $('apply-target').addEventListener('click', () => {
    if (!readTarget()) {
      message('Enter a finite number for each target coordinate and angle.', true);
      return;
    }
    move();
  });
  for (const field of [...AXES, ...ANGLES]) {
    $(`target-${field}`).addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); $('apply-target').click(); }
    });
  }
  $('sync-target').addEventListener('click', () => { stopDemo(); sync(); });
  document.querySelectorAll('[data-jog]').forEach((button) => {
    button.addEventListener('click', () => {
      const [axis, sign] = button.dataset.jog.split(':');
      tool.getWorldPosition(target.position);
      target.position[axis] += Number(sign) * Number($('jog-step').value);
      writeTarget();
      move();
    });
  });
  document.querySelectorAll('[data-rot]').forEach((button) => {
    button.addEventListener('click', () => {
      const [axis, sign] = button.dataset.rot.split(':');
      rotateTarget(axis, Number(sign) * MathUtils.degToRad(Number($('rot-step').value)));
    });
  });
  refresh();
  sync();
  updateOrientationInputs();
  return { sync, refresh };
}
