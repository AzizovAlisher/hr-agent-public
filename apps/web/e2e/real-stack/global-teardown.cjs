const { spawn } = require("node:child_process");
const { resolve } = require("node:path");

module.exports = async function globalTeardown() {
  const stackManager = resolve(
    __dirname,
    "../fixtures/real-stack/stack.mjs",
  );

  await new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(process.execPath, [stackManager, "cleanup"], {
      cwd: __dirname,
      stdio: "inherit",
    });
    child.once("error", rejectPromise);
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolvePromise();
        return;
      }
      rejectPromise(
        new Error(
          `Real-stack cleanup failed with ${signal ?? `exit ${code}`}`,
        ),
      );
    });
  });
};
