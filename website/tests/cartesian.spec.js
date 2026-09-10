import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';

test.beforeEach(async ({ page }) => {
  const source = (await readFile('src/viewer.js', 'utf8')).replace(
    'tool = createToolFrame(robot);',
    'tool = createToolFrame(robot); window.testViewer = { robot, tool, scene, camera, controls };',
  );
  const bundle = await build({
    stdin: { contents: source, resolveDir: process.cwd() + '/src', loader: 'js' },
    bundle: true, format: 'esm', write: false,
  });
  await page.route('**/assets/vendor/viewer.js', (route) => route.fulfill({
    contentType: 'text/javascript', body: bundle.outputFiles[0].text,
  }));
  await page.goto('/public/');
  await expect(page.locator('body')).toHaveAttribute('data-ready', 'true', { timeout: 45000 });
});

async function pose(page) {
  return page.evaluate(() => {
    const { robot, tool } = window.testViewer;
    robot.updateMatrixWorld(true);
    return {
      position: tool.getWorldPosition(tool.position.clone()).toArray(),
      orientation: tool.getWorldQuaternion(tool.quaternion.clone()).toArray(),
      joints: Array.from({ length: 7 }, (_, i) => robot.joints[`panda_joint${i + 1}`].angle),
      limits: Array.from({ length: 7 }, (_, i) => robot.joints[`panda_joint${i + 1}`].limit),
    };
  });
}

test('unit changes preserve the pose and radians support sliders and exact entry', async ({ page }) => {
  const initial = await pose(page);
  await page.getByLabel('Angle units').selectOption('radians');
  await expect(page.locator('output[for="panda_joint2"]')).toHaveText('-0.820 rad');
  expect((await pose(page)).joints).toEqual(initial.joints);
  await page.locator('#panda_joint5').fill('1.571');
  expect((await pose(page)).joints[4]).toBeCloseTo(1.571, 8);
  await page.locator('#panda_joint5-number').fill('0.785398');
  await page.locator('#panda_joint5-number').press('Enter');
  expect((await pose(page)).joints[4]).toBeCloseTo(0.785398, 5);
  await page.getByLabel('Angle units').selectOption('degrees');
  await expect(page.locator('output[for="panda_joint5"]')).toHaveText('45.0°');
  await page.locator('#panda_joint5-number').fill('1000');
  await page.locator('#panda_joint5-number').press('Enter');
  const clamped = await pose(page);
  expect(clamped.joints[4]).toBe(clamped.limits[4].upper);
});

test('XYZ targets and jog buttons drive the EE with orientation held; failure restores the pose', async ({ page }) => {
  const initial = await pose(page);
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  expect(Number(await page.locator('#target-x').inputValue())).toBeCloseTo(initial.position[0], 3);
  await page.getByRole('button', { name: 'Increase X', exact: true }).click();
  await expect(page.locator('#ik-status')).toContainText('Target reached');
  const moved = await pose(page);
  expect(moved.position[0]).toBeCloseTo(initial.position[0] + 0.01, 3);
  expect(moved.position[1]).toBeCloseTo(initial.position[1], 3);
  expect(moved.position[2]).toBeCloseTo(initial.position[2], 3);
  const dot = moved.orientation.reduce((sum, value, i) => sum + value * initial.orientation[i], 0);
  expect(2 * Math.acos(Math.min(1, Math.abs(dot)))).toBeLessThan(0.003);
  const target = [...moved.position];
  target[1] += 0.015;
  target[2] -= 0.01;
  for (const [i, axis] of ['x', 'y', 'z'].entries()) await page.locator(`#target-${axis}`).fill(String(target[i]));
  await page.getByRole('button', { name: 'Move to target' }).click();
  await expect(page.locator('#ik-status')).toContainText('Target reached');
  const achieved = await pose(page);
  expect(Math.hypot(...achieved.position.map((value, i) => value - target[i]))).toBeLessThan(0.0005);
  achieved.joints.forEach((value, i) => {
    expect(value).toBeGreaterThanOrEqual(achieved.limits[i].lower);
    expect(value).toBeLessThanOrEqual(achieved.limits[i].upper);
  });
  await page.locator('#target-x').fill('5');
  await page.getByRole('button', { name: 'Move to target' }).click();
  await expect(page.locator('#ik-status')).toHaveAttribute('data-error', 'true');
  expect((await pose(page)).joints).toEqual(achieved.joints);
  await page.getByRole('button', { name: 'Use current EE' }).click();
  expect(Number(await page.locator('#target-x').inputValue())).toBeCloseTo(achieved.position[0], 3);
  await page.locator('#target-x').fill('');
  await page.getByRole('button', { name: 'Move to target' }).click();
  await expect(page.locator('#ik-status')).toContainText('finite number');
  expect((await pose(page)).joints).toEqual(achieved.joints);
  await page.getByRole('button', { name: 'Use current EE' }).click();
  await page.screenshot({ path: 'test-results/cartesian-desktop.png', fullPage: true });
});

test('Cartesian controls fit on mobile and resync after a manual joint move', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('Angle units').selectOption('radians');
  await page.locator('#panda_joint2').fill('-0.7');
  const manual = await pose(page);
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  for (const [i, axis] of ['x', 'y', 'z'].entries()) {
    expect(Number(await page.locator(`#target-${axis}`).inputValue())).toBeCloseTo(manual.position[i], 3);
  }
  await page.getByLabel('Keep gripper orientation').uncheck();
  await page.getByRole('button', { name: 'Decrease Z', exact: true }).click();
  await expect(page.locator('#ik-status')).toContainText('Target reached');
  expect((await pose(page)).position[2]).toBeCloseTo(manual.position[2] - 0.01, 3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.screenshot({ path: 'test-results/cartesian-mobile.png', fullPage: true });
});

test('dragging the EE arrow moves the arm while camera orbit is suspended', async ({ page }) => {
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  await page.locator('canvas').scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const initial = await pose(page);
  const point = await page.evaluate(() => {
    const { scene, camera, robot } = window.testViewer;
    const drag = scene.children.find((child) => child.isTransformControlsRoot).controls;
    const handle = drag._gizmo.picker.translate.children.find((child) => child.name === 'Z');
    handle.geometry.computeBoundingBox();
    const p = handle.geometry.boundingBox.getCenter(robot.position.clone()).applyMatrix4(handle.matrixWorld).project(camera);
    const rect = document.querySelector('canvas').getBoundingClientRect();
    return { x: rect.x + (p.x + 1) * rect.width / 2, y: rect.y + (1 - p.y) * rect.height / 2 };
  });
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  expect(await page.evaluate(() => window.testViewer.controls.enabled)).toBe(false);
  await page.mouse.move(point.x, point.y + 10, { steps: 4 });
  await page.mouse.up();
  expect(await page.evaluate(() => window.testViewer.controls.enabled)).toBe(true);
  await expect(page.locator('#ik-status')).toContainText('Target reached');
  const moved = await pose(page);
  expect(moved.position[2]).toBeLessThan(initial.position[2] - 0.002);
  expect(moved.position[2]).toBeCloseTo(Number(await page.locator('#target-z').inputValue()), 3);
  await page.getByLabel('Show EE drag controls').uncheck();
  expect(await page.evaluate(() => {
    const drag = window.testViewer.scene.children.find((child) => child.isTransformControlsRoot).controls;
    return drag.object === undefined;
  })).toBe(true);
});

async function targetFields(page) {
  const names = ['x', 'y', 'z', 'roll', 'pitch', 'yaw'];
  const values = await Promise.all(names.map((name) => page.locator(`#target-${name}`).inputValue()));
  return Object.fromEntries(names.map((name, i) => [name, Number(values[i])]));
}

const turnBetween = (a, b) => 2 * Math.acos(Math.min(1, Math.abs(a.reduce((sum, v, i) => sum + v * b[i], 0))));

test('the Cartesian reference point sits at the finger tips, ahead of panda_grasptarget', async ({ page }) => {
  const measured = await page.evaluate(() => {
    const { robot, tool } = window.testViewer;
    robot.updateMatrixWorld(true);
    // Both finger meshes end 0.0539 m along their own +z; their midpoint is the tip centre.
    const tipOf = (name) => robot.links[name].localToWorld(tool.position.clone().set(0, 0, 0.0539));
    const left = tipOf('panda_leftfinger');
    const right = tipOf('panda_rightfinger');
    return {
      expected: left.clone().add(right).multiplyScalar(0.5).toArray(),
      actual: tool.getWorldPosition(tool.position.clone()).toArray(),
      behindGrasptarget: robot.links.panda_grasptarget
        .getWorldPosition(tool.position.clone())
        .distanceTo(tool.getWorldPosition(tool.position.clone())),
      parent: tool.parent.name,
    };
  });
  expect(measured.parent).toBe('panda_hand');
  expect(Math.hypot(...measured.actual.map((v, i) => v - measured.expected[i]))).toBeLessThan(1e-6);
  expect(measured.behindGrasptarget).toBeCloseTo(0.0073, 4);
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  const readout = await page.locator('#tool-position').textContent();
  expect(Number(readout.match(/X ([-\d.]+)/)[1])).toBeCloseTo(measured.actual[0], 3);
  expect(Number(await page.locator('#target-x').inputValue())).toBeCloseTo(measured.actual[0], 3);
});

// URDF roll/pitch/yaw as a quaternion, derived here rather than reusing the viewer's.
function quaternionFromRpy(roll, pitch, yaw) {
  const [r, p, y] = [roll, pitch, yaw].map((degrees) => degrees * Math.PI / 360);
  const [cr, sr, cp, sp, cy, sy] = [Math.cos(r), Math.sin(r), Math.cos(p), Math.sin(p), Math.cos(y), Math.sin(y)];
  return [
    sr * cp * cy - cr * sp * sy,
    cr * sp * cy + sr * cp * sy,
    cr * cp * sy - sr * sp * cy,
    cr * cp * cy + sr * sp * sy,
  ];
}

test('roll, pitch and yaw turn the gripper about the fingertip point', async ({ page }) => {
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  const entered = await targetFields(page);
  // The angles shown for the current pose describe that pose.
  expect(turnBetween((await pose(page)).orientation,
    quaternionFromRpy(entered.roll, entered.pitch, entered.yaw))).toBeLessThan(0.003);
  for (const [button, axis, step] of [['Increase yaw', 'yaw', 5], ['Decrease pitch', 'pitch', -5]]) {
    const start = await pose(page);
    await page.getByRole('button', { name: button, exact: true }).click();
    await expect(page.locator('#ik-status')).toContainText('Target reached');
    const moved = await pose(page);
    // The pivot stays put; the gripper turns by exactly the step, about that point.
    expect(Math.hypot(...moved.position.map((v, i) => v - start.position[i]))).toBeLessThan(0.0005);
    expect(turnBetween(moved.orientation, start.orientation)).toBeCloseTo(Math.abs(step) * Math.PI / 180, 2);
    const shown = await targetFields(page);
    expect(shown[axis]).toBeCloseTo(entered[axis] + step, 6);
    expect(turnBetween(moved.orientation, quaternionFromRpy(shown.roll, shown.pitch, shown.yaw))).toBeLessThan(0.003);
  }
  // Typed angles are applied in the same convention, still about the fingertip point.
  const held = await pose(page);
  await page.locator('#target-roll').fill(String(entered.roll + 12));
  await page.locator('#target-roll').press('Enter');
  await expect(page.locator('#ik-status')).toContainText('Target reached');
  const turned = await pose(page);
  expect(Math.hypot(...turned.position.map((v, i) => v - held.position[i]))).toBeLessThan(0.0005);
  expect(turnBetween(turned.orientation,
    quaternionFromRpy(entered.roll + 12, entered.pitch - 5, entered.yaw + 5))).toBeLessThan(0.003);
  await page.getByRole('button', { name: 'Use current EE' }).click();
  const resynced = await targetFields(page);
  expect(turnBetween(turned.orientation,
    quaternionFromRpy(resynced.roll, resynced.pitch, resynced.yaw))).toBeLessThan(0.003);
});

test('rotate is selectable by control and by key, and needs a held orientation', async ({ page }) => {
  const mode = () => page.evaluate(() =>
    window.testViewer.scene.children.find((child) => child.isTransformControlsRoot).controls.mode);
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  expect(await mode()).toBe('translate');
  await page.getByLabel('Drag mode').selectOption('rotate');
  expect(await mode()).toBe('rotate');
  await page.locator('canvas').click({ position: { x: 40, y: 40 } });
  await page.keyboard.press('g');
  expect(await mode()).toBe('translate');
  await page.keyboard.press('r');
  expect(await mode()).toBe('rotate');
  // Releasing the orientation leaves nothing to rotate, so the angles and the mode lock out.
  await page.getByLabel('Keep gripper orientation').uncheck();
  await expect(page.locator('#target-yaw')).toBeDisabled();
  expect(await mode()).toBe('translate');
  await page.keyboard.press('r');
  expect(await mode()).toBe('translate');
  await page.getByLabel('Keep gripper orientation').check();
  await expect(page.locator('#target-yaw')).toBeEnabled();
});

test('the angle unit applies to roll, pitch and yaw as well as the joints', async ({ page }) => {
  await page.getByRole('tab', { name: 'Cartesian EE' }).click();
  const degrees = await targetFields(page);
  await page.getByLabel('Angle units').selectOption('radians');
  await expect(page.locator('#cartesian-panel [data-angle-unit]').first()).toHaveText('rad');
  await expect(page.getByLabel('Rotate step')).toContainText('0.087 rad');
  const radians = await targetFields(page);
  expect(radians.yaw).toBeCloseTo(degrees.yaw * Math.PI / 180, 3);
  expect(radians.x).toBeCloseTo(degrees.x, 6);
  await page.locator('#target-yaw').fill(String(radians.yaw + 0.2));
  await page.locator('#target-yaw').press('Enter');
  await expect(page.locator('#ik-status')).toContainText('Target reached');
  await page.getByRole('button', { name: 'Use current EE' }).click();
  expect((await targetFields(page)).yaw).toBeCloseTo(radians.yaw + 0.2, 2);
  await page.getByLabel('Angle units').selectOption('degrees');
  await expect(page.locator('#cartesian-panel [data-angle-unit]').first()).toHaveText('°');
  expect((await targetFields(page)).yaw).toBeCloseTo(degrees.yaw + 0.2 * 180 / Math.PI, 1);
  await page.screenshot({ path: 'test-results/cartesian-rpy.png', fullPage: true });
});
