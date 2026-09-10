import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';

test('Panda CAD edges cannot turn link5 and link6 surfaces into line segments', async () => {
  for (const name of ['link5', 'link6']) {
    const original = await readFile(`../genesis/assets/urdf/panda_bullet/meshes/visual/${name}.obj`, 'utf8');
    const prepared = await readFile(`public/assets/panda/meshes/visual/${name}.obj`, 'utf8');
    const faces = (text) => text.split('\n').filter((line) => line.startsWith('f '));
    expect(faces(prepared)).toEqual(faces(original));
    const object = new OBJLoader().parse(prepared);
    let triangles = 0;
    object.traverse((child) => {
      expect(Boolean(child.isLineSegments)).toBe(false);
      if (child.isMesh) triangles += child.geometry.attributes.position.count / 3;
    });
    expect(triangles).toBe(faces(original).length);
  }
});

test('joint5 renders solid surfaces, moves its descendants, and casts a floor shadow', async ({ page }) => {
  // Inspect the real viewer in a test-only bundle; no debugging globals ship publicly.
  const source = (await readFile('src/viewer.js', 'utf8')).replace(
    'scene.add(robot);',
    'scene.add(robot); window.testViewer = { robot, scene, camera, renderer, key };',
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
  const before = await page.evaluate(() => {
    const { robot } = window.testViewer;
    robot.updateMatrixWorld(true);
    const meshes = [];
    for (const visual of robot.links.panda_link5.children.filter((child) => child.isURDFVisual)) {
      visual.traverse((child) => {
        if (child.isMesh || child.isLineSegments) meshes.push({
          type: child.type, casts: child.castShadow, receives: child.receiveShadow,
        });
      });
    }
    return {
      meshes,
      parent: robot.links.panda_link4.matrixWorld.toArray(),
      link: robot.links.panda_link5.matrixWorld.toArray(),
      tool: robot.links.panda_grasptarget.matrixWorld.toArray(),
    };
  });
  expect(before.meshes).toEqual([
    { type: 'Mesh', casts: true, receives: true },
    { type: 'Mesh', casts: true, receives: true },
  ]);
  await page.locator('#panda_joint5').fill('90');
  const after = await page.evaluate(() => {
    const { robot } = window.testViewer;
    robot.updateMatrixWorld(true);
    return {
      parent: robot.links.panda_link4.matrixWorld.toArray(),
      link: robot.links.panda_link5.matrixWorld.toArray(),
      tool: robot.links.panda_grasptarget.matrixWorld.toArray(),
    };
  });
  expect(after.parent).toEqual(before.parent);
  expect(after.link).not.toEqual(before.link);
  expect(after.tool).not.toEqual(before.tool);
  await page.locator('canvas').scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.locator('.viewport').screenshot({ path: 'test-results/joint5-90-degrees.png' });
  await page.getByRole('button', { name: 'Home pose' }).click();
  await page.locator('canvas').scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.locator('.viewport').screenshot({ path: 'test-results/joint5-corrected.png' });

  const changedShadowPixels = await page.evaluate(() => {
    const { robot, renderer, camera, scene } = window.testViewer;
    // Disable robot self-shadow reception so any difference is on the floor.
    robot.traverse((child) => { if (child.isMesh) child.receiveShadow = false; });
    const linkMeshes = [];
    for (const visual of robot.links.panda_link5.children.filter((child) => child.isURDFVisual)) {
      visual.traverse((child) => { if (child.isMesh) linkMeshes.push(child); });
    }
    const gl = renderer.getContext();
    const sample = () => {
      renderer.render(scene, camera);
      const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
      gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      return pixels;
    };
    const withShadow = sample();
    linkMeshes.forEach((mesh) => { mesh.castShadow = false; });
    const withoutShadow = sample();
    linkMeshes.forEach((mesh) => { mesh.castShadow = true; });
    robot.traverse((child) => { if (child.isMesh) child.receiveShadow = true; });
    let difference = 0;
    for (let i = 0; i < withShadow.length; i += 4) {
      if (Math.abs(withShadow[i] - withoutShadow[i]) > 3) difference++;
    }
    return difference;
  });
  expect(changedShadowPixels).toBeGreaterThan(50);
});
