import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const fixtureDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(fixtureDirectory, "../../../../..");
const baseCompose = join(repositoryRoot, "docker-compose.dev.yml");
const testCompose = join(fixtureDirectory, "compose.e2e.yml");
const project = process.env.REAL_STACK_PROJECT ?? "hr-agent-real-stack-e2e";

if (!/^hr-agent-real-stack-e2e(?:-[a-z0-9-]+)?$/.test(project)) {
  throw new Error(`Unsafe real-stack Compose project name: ${project}`);
}

const apiPort = process.env.REAL_STACK_API_PORT ?? "8300";
const webPort = process.env.REAL_STACK_WEB_PORT ?? "3300";
const httpsPort = process.env.REAL_STACK_HTTPS_PORT ?? "3643";

const fixtureEnvironment = {
  ...process.env,
  REAL_STACK_API_PORT: apiPort,
  REAL_STACK_WEB_PORT: webPort,
  REAL_STACK_HTTPS_PORT: httpsPort,
  POSTGRES_PASSWORD: "real-stack-postgres-operator-2026-7Vq2Jm9P",
  HR_DB_API_PASSWORD: "real-stack-api-db-2026-4Ks8Wn3T",
  HR_DB_WORKER_PASSWORD: "real-stack-worker-db-2026-6Px2Rc9M",
  JWT_SECRET: "Q7pV2mL9xR4kN8sT1wY5cD0fH6jB3uA9eG2iZ7oC4qX8vM1n",
  AUTH_MFA_ENCRYPTION_KEY:
    "B9rT4mK7xP2vN8cD1wF5hJ0sL6qY3uA9eG2iZ7oC4nX8bM1p",
  PUBLIC_RATE_LIMIT_HMAC_SECRET:
    "H3qW8nC1xV6mK9rT2pL5sD0fJ7uA4eY9iG2oZ6bN1cX8vM3k",
  MINIO_ROOT_USER: "realstackroot",
  MINIO_ROOT_PASSWORD: "real-stack-minio-root-2026-8Qp4Vm2N",
  S3_ACCESS_KEY: "realstackapp",
  S3_SECRET_KEY: "real-stack-minio-app-2026-5Jx9Tc3W7nL2",
  INTEGRATION_CREDENTIAL_ENCRYPTION_KEY:
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
  AUTH_SECRET: "V8mQ2pR7xT4nK9cL1wD5hF0sJ6uA3eY8iG2oZ7bN4vX9kM1q",
  AUTH_SESSION_SECRET:
    "N5xT9mC2qV7kR4pL1wD8hF0sJ6uA3eY9iG2oZ7bN4vX1cM8",
  ASSESSMENT_SESSION_SECRET:
    "C4vN8qT2mK7xR5pL1wD9hF0sJ6uA3eY8iG2oZ7bV4nX1cM5",
};

const composePrefix = [
  "compose",
  "--env-file",
  "/dev/null",
  "--project-name",
  project,
  "--file",
  baseCompose,
  "--file",
  testCompose,
];

let activeChild = null;
let shuttingDown = false;

function runDocker(arguments_, { allowFailure = false } = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn("docker", arguments_, {
      cwd: repositoryRoot,
      env: fixtureEnvironment,
      stdio: "inherit",
    });
    activeChild = child;
    child.once("error", rejectPromise);
    child.once("exit", (code, signal) => {
      if (activeChild === child) activeChild = null;
      if (code === 0 || allowFailure) {
        resolvePromise();
        return;
      }
      rejectPromise(
        new Error(
          `docker ${arguments_.join(" ")} failed with ${signal ?? `exit ${code}`}`,
        ),
      );
    });
  });
}

function compose(arguments_, options) {
  return runDocker([...composePrefix, ...arguments_], options);
}

async function waitForFullReadiness() {
  const deadline = Date.now() + 180_000;
  let lastStatus = "not requested";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${apiPort}/health/ready`);
      lastStatus = `${response.status} ${await response.text()}`;
      if (response.status === 200) return;
    } catch (error) {
      lastStatus = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 1_000));
  }
  throw new Error(`Real API did not become fully ready: ${lastStatus}`);
}

async function removeIsolatedStack() {
  await compose(
    ["down", "--volumes", "--remove-orphans", "--timeout", "10"],
    { allowFailure: true },
  );
}

async function shutdown(exitCode = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (activeChild && !activeChild.killed) activeChild.kill("SIGTERM");
  await removeIsolatedStack();
  process.exit(exitCode);
}

process.on("SIGINT", () => void shutdown(130));
process.on("SIGTERM", () => void shutdown(0));

if (process.argv[2] === "cleanup") {
  await removeIsolatedStack();
  process.exit(0);
}

try {
  // A fixed, tightly validated project name makes stale-run cleanup possible,
  // while the project-scoped volumes keep developer databases untouched.
  await removeIsolatedStack();
  await compose([
    "up",
    "--build",
    "--detach",
    "--wait",
    "--wait-timeout",
    "600",
  ]);

  // The upstream image launches freshclam and clamd concurrently. A new
  // daily database can land after clamd has opened the bundled one, so make
  // that hand-off explicit before enforcing the real readiness contract.
  await compose(["exec", "-T", "clamav", "clamdscan", "--reload"]);
  await waitForFullReadiness();

  const operatorDatabaseUrl =
    "postgresql+asyncpg://postgres:" +
    fixtureEnvironment.POSTGRES_PASSWORD +
    "@postgres:5432/recruitment_v2";
  await compose([
    "run",
    "--rm",
    "--no-deps",
    "--volume",
    `${fixtureDirectory}:/real-stack-fixture:ro`,
    "--env",
    `DATABASE_URL=${operatorDatabaseUrl}`,
    "--env",
    "PYTHONPATH=/app",
    "api",
    "python",
    "/real-stack-fixture/seed.py",
  ]);

  process.stdout.write(
    `Isolated real stack is seeded (project=${project}, api=${apiPort}, web=${webPort}).\n`,
  );
  setInterval(() => {}, 60_000);
  await new Promise(() => {});
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.stack : error}\n`);
  await compose(
    ["logs", "--no-color", "--tail", "200", "api", "worker", "web"],
    { allowFailure: true },
  );
  await shutdown(1);
}
