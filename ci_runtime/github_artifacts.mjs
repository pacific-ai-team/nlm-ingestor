// GitHub's supported artifact SDK supplies the durable upload protocol.
// Stdout may contain SDK progress; only the final prefixed JSON is consumed.
import { createHash, randomUUID } from 'node:crypto';
import { copyFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [operation, key, filename] = process.argv.slice(2);
const repository = process.env.GITHUB_REPOSITORY;
const token = process.env.CI_RUNTIME_GITHUB_TOKEN;
const hash = value => createHash('sha256').update(value).digest('hex').slice(0, 32);
const namespace = value => value.startsWith('history/') ? value.split('/').slice(0, -2).join('/') : value;
const prefix = value => `ci-runtime-${value.startsWith('history/') ? 'h' : 'c'}-${hash(namespace(value))}-`;
const [repositoryOwner, repositoryName] = repository.split('/');
const sdk = await import(pathToFileURL(process.env.CI_RUNTIME_ARTIFACT_SDK).href);
const client = new sdk.DefaultArtifactClient();
const scratch = await mkdtemp(path.join(tmpdir(), 'ci-artifact-'));

async function api(relative) {
  const response = await fetch(`${process.env.GITHUB_API_URL || 'https://api.github.com'}/repos/${repository}/${relative}`, {
    headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`Artifact list HTTP ${response.status}`);
  return response.json();
}

async function download(id, run) {
  const result = await client.downloadArtifact(Number(id), {
    path: scratch,
    findBy: { token, workflowRunId: Number(run), repositoryOwner, repositoryName },
  });
  if (result.digestMismatch) throw new Error('Artifact digest mismatch');
  // Runner scratch and checkout can reside on different volumes.
  await copyFile(path.join(scratch, 'payload'), filename);
  return true;
}

try {
  let result;
  if (operation === 'upload') {
    await copyFile(filename, path.join(scratch, 'payload'));
    const name = `${prefix(key)}${hash(key)}-${Date.now()}-${randomUUID().slice(0, 8)}`;
    await client.uploadArtifact(name, [path.join(scratch, 'payload')], scratch, {
      retentionDays: key.startsWith('history/') ? 7 : 1, compressionLevel: 0,
    });
    result = true;
  } else if (operation === 'objects') {
    // History is read ONCE by the plan job, then distributed to every shard.
    // No independent REST listings may decide matrix shard memberships.
    const expected = `ci-runtime-h-${hash(key.replace(/\/$/, ''))}-`;
    result = [];
    for (let page = 1; ; page++) {
      const response = await api(`actions/artifacts?per_page=100&page=${page}`);
      for (const artifact of response.artifacts) {
        if (!artifact.expired && artifact.name.startsWith(expected)) {
          result.push({ key: `artifact:${artifact.id}:${artifact.workflow_run.id}`, modified: Date.parse(artifact.created_at) / 1000 });
        }
      }
      if (response.artifacts.length < 100) break;
    }
  } else if (operation === 'download') {
    if (key.startsWith('artifact:')) {
      const [, id, run] = key.split(':');
      result = await download(id, run);
    } else {
      const expected = `${prefix(key)}${hash(key)}-`;
      // Remote checkpoints are append-only. An interrupted upload is never
      // finalized, and can never replace the previous usable checkpoint.
      const found = [];
      for (let page = 1; ; page++) {
        const response = await api(`actions/runs/${process.env.GITHUB_RUN_ID}/artifacts?per_page=100&page=${page}`);
        found.push(...response.artifacts.filter(a => !a.expired && a.name.startsWith(expected)));
        if (response.artifacts.length < 100) break;
      }
      found.sort((a, b) => b.id - a.id);
      result = found.length ? await download(found[0].id, process.env.GITHUB_RUN_ID) : false;
    }
  } else {
    throw new Error('Unknown CI artifact operation');
  }
  process.stdout.write(`\nCI_RUNTIME_JSON=${JSON.stringify(result)}\n`);
} catch {
  // SDK errors may contain signed download URLs. Do not echo them or its
  // captured progress, which can also contain masking commands with tokens.
  process.stderr.write('CI artifact operation failed\n');
  process.exitCode = 1;
} finally {
  await rm(scratch, { recursive: true, force: true });
}
