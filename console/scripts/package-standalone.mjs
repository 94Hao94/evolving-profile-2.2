import { access, cp, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Only a server adjacent to this build's BUILD_ID is a deployable entry.
// Broad tracing can include previous releases with their own server.js.
export async function packageStandalone(root = process.cwd()) {
  root = path.resolve(root);
  const trace = path.join(root, '.next/standalone');
  const id = (await readFile(path.join(root, '.next/BUILD_ID'), 'utf8')).trim();
  const candidates = [trace, path.join(trace, path.basename(root))];
  for (const entry of await readdir(trace, { withFileTypes: true })) {
    if (entry.isDirectory() && entry.name !== 'node_modules') candidates.push(path.join(trace, entry.name));
  }
  let source;
  for (const candidate of new Set(candidates)) {
    try {
      await access(path.join(candidate, 'server.js'));
      if ((await readFile(path.join(candidate, '.next/BUILD_ID'), 'utf8')).trim() === id) { source = candidate; break; }
    } catch { /* Not a build entry. */ }
  }
  if (!source) throw new Error('No current standalone entry with matching BUILD_ID');
  const target = path.join(root, 'standalone');
  await rm(target, { recursive: true, force: true });
  await mkdir(target);
  for (const name of ['server.js', 'package.json', '.next']) await cp(path.join(source, name), path.join(target, name), { recursive: true });
  const dependencies = path.join(trace, 'node_modules');
  await cp(dependencies, path.join(target, 'node_modules'), { recursive: true });
  // Trace roots may have additional application-local dependencies.
  if (source !== trace) {
    try { await cp(path.join(source, 'node_modules'), path.join(target, 'node_modules'), { recursive: true }); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  await cp(path.join(root, '.next/static'), path.join(target, '.next/static'), { recursive: true });
  await cp(path.join(root, 'public'), path.join(target, 'public'), { recursive: true });
  if ((await readFile(path.join(target, '.next/BUILD_ID'), 'utf8')).trim() !== id) throw new Error('Standalone BUILD_ID mismatch');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await packageStandalone();
