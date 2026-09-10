import { build } from 'esbuild';
import { mkdir, copyFile, readFile, writeFile } from 'node:fs/promises';

await mkdir('assets/vendor', { recursive: true });
await build({
  entryPoints: ['src/viewer.js'],
  outfile: 'assets/vendor/viewer.js',
  bundle: true,
  minify: true,
  sourcemap: false,
  format: 'esm',
  target: ['es2022'],
  legalComments: 'linked',
});
await copyFile('node_modules/three/LICENSE', 'assets/vendor/THREE-LICENSE.txt');
// The npm package omits LICENSE; the repository includes the Apache-2.0 text.
await copyFile('../LICENSE', 'assets/vendor/URDF-LOADER-LICENSE.txt');
const loaderReadme = await readFile('node_modules/urdf-loader/README.md', 'utf8');
const attribution = loaderReadme.slice(loaderReadme.indexOf('# LICENSE'));
if (!attribution.includes('Copyright')) throw new Error('URDF loader attribution not found');
await writeFile('assets/vendor/URDF-LOADER-NOTICE.md', attribution.replace('../LICENSE', 'URDF-LOADER-LICENSE.txt'));
console.log('Built local browser bundle. Serve this folder with npm start or any static HTTP server.');
