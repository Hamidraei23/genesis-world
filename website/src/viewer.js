import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { MTLLoader } from 'three/addons/loaders/MTLLoader.js';
import URDFLoader from 'urdf-loader';
import { createCartesianControls } from './cartesian-controls.js';
import { createToolFrame } from './kinematics.js';

const $ = (id) => document.getElementById(id);
const status = $('load-status');
const home = [0, -0.82, 0, -2.18, 0, 2.9, 0.78];
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

function fail(error) {
  console.error(error);
  status.hidden = false;
  status.dataset.error = 'true';
  status.textContent = `Could not load the viewer: ${error.message}. Check that the page is served over HTTP and reload.`;
  $('motion-state').textContent = '● Viewer unavailable';
  $('robot-controls').disabled = true;
}

async function start() {
  const host = $('canvas-host');
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#171e29');
  scene.fog = new THREE.Fog('#171e29', 4, 12);
  // Keep the robot's native Z-up frame; rotate only the floor geometry.
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 50);
  camera.up.set(0, 0, 1);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.35;
  host.appendChild(renderer.domElement);
  renderer.domElement.setAttribute('aria-label', 'Franka Panda 3D view. Drag to rotate, scroll to zoom, right-drag to pan.');
  renderer.domElement.tabIndex = 0;
  renderer.domElement.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();
    renderer.setAnimationLoop(null);
    fail(new Error('The graphics context was lost'));
  });
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = !reducedMotion;
  controls.minDistance = 0.35;
  controls.maxDistance = 6;
  controls.maxPolarAngle = Math.PI / 2 - 0.02;
  controls.autoRotateSpeed = 0.65;
  controls.listenToKeyEvents(renderer.domElement);
  const views = {
    perspective: [1.7, 1.8, 1.25],
    front: [2.5, 0, 0.6],
    side: [0.25, 2.5, 0.6],
    top: [0.25, 0.001, 3],
  };
  function setView(view) {
    controls.target.set(0.22, 0, 0.46);
    camera.position.fromArray(views[view]);
    controls.update();
    document.querySelectorAll('[data-view]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.view === view));
    });
  }
  setView('perspective');
  document.querySelectorAll('[data-view]').forEach((button) => {
    button.addEventListener('click', () => setView(button.dataset.view));
  });
  controls.addEventListener('start', () => {
    document.querySelectorAll('[data-view]').forEach((button) => button.setAttribute('aria-pressed', 'false'));
  });

  scene.add(new THREE.HemisphereLight(0xdcecff, 0x677587, 2.7));
  const key = new THREE.DirectionalLight(0xfff3dc, 3.4);
  key.position.set(2, 1, 4);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, { left: -1.5, right: 1.5, top: 1.5, bottom: -1.5, near: 0.1, far: 10 });
  key.shadow.bias = -0.0002;
  key.shadow.normalBias = 0.008;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x8acfff, 2);
  rim.position.set(-1, -2, 2);
  scene.add(rim);
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(200, 200),
    new THREE.MeshStandardMaterial({ color: 0x202a37, roughness: 0.95 }),
  );
  floor.position.z = -0.004;
  floor.receiveShadow = true;
  scene.add(floor);
  const grid = new THREE.GridHelper(6, 30, 0x8098a9, 0x526b80);
  grid.rotation.x = Math.PI / 2;
  grid.position.z = -0.002;
  grid.material.transparent = true;
  grid.material.opacity = 0.6;
  grid.material.fog = false;
  scene.add(grid);
  const axes = new THREE.AxesHelper(0.35);
  axes.visible = false;
  scene.add(axes);
  $('grid').addEventListener('change', (event) => { grid.visible = event.target.checked; });
  $('axes').addEventListener('change', (event) => { axes.visible = event.target.checked; });
  $('rotate').addEventListener('change', (event) => { controls.autoRotate = event.target.checked; });
  if (!document.fullscreenEnabled) $('fullscreen').hidden = true;
  $('fullscreen').addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.querySelector('.viewport').requestFullscreen();
    } catch {
      $('fullscreen').textContent = 'Expand unavailable';
    }
  });
  document.addEventListener('fullscreenchange', () => {
    $('fullscreen').textContent = document.fullscreenElement ? '⛶ Restore' : '⛶ Expand';
  });

  const resize = new ResizeObserver(() => {
    const { width, height } = host.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  });
  resize.observe(host);
  let robot;
  let tool;
  let cartesian = null;
  let playing = false;
  let elapsed = 0;
  let lastTime = 0;
  const toolPosition = new THREE.Vector3();
  let visible = true;
  new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; }).observe(host);
  renderer.setAnimationLoop((time) => {
    const delta = Math.min((time - lastTime) / 1000, 0.05);
    lastTime = time;
    if (document.hidden || !visible) return;
    if (robot && playing) {
      elapsed += delta;
      const amplitude = [0.32, 0.15, 0.24, 0.18, 0.24, 0.15, 0.3];
      home.forEach((angle, index) => {
        robot.setJointValue(`panda_joint${index + 1}`, angle + Math.sin(elapsed * 0.65) * amplitude[index]);
      });
      updateSliders();
      cartesian?.sync();
    }
    controls.update(delta);
    if (tool) {
      tool.getWorldPosition(toolPosition);
      $('tool-position').textContent = `X ${toolPosition.x.toFixed(3)}   Y ${toolPosition.y.toFixed(3)}   Z ${toolPosition.z.toFixed(3)} m`;
    }
    renderer.render(scene, camera);
  });

  // Track the complete asset graph, including OBJ -> MTL -> texture dependencies.
  const manager = new THREE.LoadingManager();
  const errors = [];
  manager.onError = (url) => errors.push(url);
  manager.onProgress = (_url, loaded, total) => {
    if (!status.dataset.error) status.textContent = `Loading robot assets… ${loaded}/${total}`;
  };
  const finished = new Promise((resolve) => { manager.onLoad = resolve; });
  const loader = new URDFLoader(manager);
  const displayMaterials = new Map();
  function displayMaterial(source) {
    if (!displayMaterials.has(source)) {
      // Upstream MTLs contain negative shininess. Use stable PBR shading while
      // retaining the authored colors, textures, and transparency.
      displayMaterials.set(source, new THREE.MeshStandardMaterial({
        color: source.color,
        map: source.map,
        roughness: 0.52,
        metalness: 0.08,
        opacity: source.opacity,
        transparent: source.transparent,
        side: source.side,
      }));
    }
    return displayMaterials.get(source);
  }
  loader.parseCollision = false;
  loader.loadMeshCb = (url, loadingManager, material, done) => {
    // A sentinel keeps the manager open across asynchronous OBJ/MTL parsing.
    const token = `${url}#complete`;
    loadingManager.itemStart(token);
    (async () => {
      const text = await new THREE.FileLoader(loadingManager).loadAsync(url);
      const objLoader = new OBJLoader(loadingManager);
      const mtl = /^mtllib\s+(.+)$/m.exec(text);
      if (mtl) {
        const materials = await new MTLLoader(loadingManager).loadAsync(new URL(mtl[1].trim(), url).href);
        materials.preload();
        objLoader.setMaterials(materials);
      }
      const object = objLoader.parse(text);
      object.traverse((child) => {
        if (!child.isMesh) return;
        if (!mtl) child.material = material;
        child.material = Array.isArray(child.material)
          ? child.material.map(displayMaterial) : displayMaterial(child.material);
        child.castShadow = true;
        child.receiveShadow = true;
      });
      done(object);
    })().catch((error) => {
      errors.push(url);
      done(null, error);
    }).finally(() => loadingManager.itemEnd(token));
  };
  const url = new URL('assets/panda/panda.urdf', document.baseURI).href;
  robot = await loader.loadAsync(url);
  await finished;
  if (errors.length) throw new Error(`Missing model assets: ${[...new Set(errors)].join(', ')}`);
  if (!robot.links.panda_hand) throw new Error('The model is missing the Panda hand');
  scene.add(robot);
  tool = createToolFrame(robot);
  const sliders = [];
  const unit = () => $('angle-unit').value;
  const toDisplay = (radians) => unit() === 'radians' ? radians : THREE.MathUtils.radToDeg(radians);
  const toRadians = (value) => unit() === 'radians' ? value : THREE.MathUtils.degToRad(value);
  for (let index = 0; index < 7; index++) {
    const name = `panda_joint${index + 1}`;
    const joint = robot.joints[name];
    if (!joint) throw new Error(`The model is missing ${name}`);
    const row = document.createElement('div');
    row.className = 'joint';
    row.innerHTML = `<label for="${name}">Joint ${index + 1}<output for="${name}"></output></label><div class="joint-entry"><input id="${name}" type="range"><input id="${name}-number" type="number" step="any" aria-label="Joint ${index + 1} value"></div>`;
    const input = row.querySelector('input[type="range"]');
    const number = row.querySelector('input[type="number"]');
    sliders.push({ input, number, output: row.querySelector('output'), joint });
    const apply = (value) => {
      if (!Number.isFinite(value)) {
        number.setCustomValidity('Enter a finite joint angle.');
        number.reportValidity();
        return;
      }
      setPlaying(false);
      robot.setJointValue(name, toRadians(value));
      updateSliders();
      cartesian?.sync();
    };
    input.addEventListener('input', () => apply(input.valueAsNumber));
    number.addEventListener('change', () => apply(number.valueAsNumber));
    number.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); apply(number.valueAsNumber); }
    });
    $('joints').appendChild(row);
  }
  function updateSliders() {
    sliders.forEach(({ input, number, output, joint }, index) => {
      const value = toDisplay(joint.angle);
      const radians = unit() === 'radians';
      const factor = radians ? 1000 : 10;
      input.min = Math.ceil(toDisplay(joint.limit.lower) * factor) / factor;
      input.max = Math.floor(toDisplay(joint.limit.upper) * factor) / factor;
      input.step = 1 / factor;
      input.value = value;
      input.setAttribute('aria-valuetext', `${value.toFixed(radians ? 3 : 1)} ${unit()}`);
      output.value = `${value.toFixed(radians ? 3 : 1)}${radians ? ' rad' : '°'}`;
      number.min = toDisplay(joint.limit.lower);
      number.max = toDisplay(joint.limit.upper);
      number.value = value.toFixed(5);
      number.setCustomValidity('');
      number.setAttribute('aria-label', `Joint ${index + 1} value in ${unit()}`);
    });
  }
  $('angle-unit').addEventListener('change', () => { updateSliders(); cartesian?.refresh(); });
  function setPlaying(value) {
    playing = value;
    $('demo').setAttribute('aria-pressed', String(value));
    $('demo').textContent = value ? 'Ⅱ Pause demo' : '▷ Play demo';
    $('motion-state').textContent = value ? '● Motion demo' : '● Ready to explore';
  }
  function setHome() {
    home.forEach((angle, index) => robot.setJointValue(`panda_joint${index + 1}`, angle));
    robot.setJointValue('panda_finger_joint1', 0.00809);
    $('gripper').value = '16.18';
    $('gripper-value').value = '16.2 mm';
    elapsed = 0;
    updateSliders();
    cartesian?.sync();
  }
  $('home').addEventListener('click', () => { setPlaying(false); setHome(); });
  $('demo').addEventListener('click', () => {
    if (!playing) setHome();
    setPlaying(!playing);
  });
  $('gripper').addEventListener('input', (event) => {
    const width = Number(event.target.value);
    robot.setJointValue('panda_finger_joint1', width / 2000);
    $('gripper-value').value = `${width.toFixed(1)} mm`;
  });
  $('wireframe').addEventListener('change', (event) => {
    robot.traverse((child) => {
      if (child.isMesh) {
        const materials = Array.isArray(child.material) ? child.material : [child.material];
        materials.forEach((material) => { material.wireframe = event.target.checked; });
      }
    });
  });
  cartesian = createCartesianControls({
    robot, tool, scene, camera, renderer, orbit: controls,
    stopDemo: () => setPlaying(false), onPoseChanged: updateSliders,
    angle: { unit, toDisplay, toRadians },
  });
  setHome();
  setPlaying(false);
  $('robot-controls').disabled = false;
  status.hidden = true;
  document.body.dataset.ready = 'true';
}

start().catch(fail);
