import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { createServer } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";

const port = Number(process.env.REAL_STACK_HTTPS_PORT ?? "3643");
const target = new URL(
  process.env.REAL_STACK_PROXY_TARGET ?? "http://127.0.0.1:3300",
);
const certificateDirectory = mkdtempSync(
  join(tmpdir(), "hr-agent-real-stack-tls-"),
);
const keyPath = join(certificateDirectory, "localhost-key.pem");
const certificatePath = join(certificateDirectory, "localhost-cert.pem");

const certificate = spawnSync(
  "openssl",
  [
    "req",
    "-x509",
    "-newkey",
    "rsa:2048",
    "-sha256",
    "-nodes",
    "-keyout",
    keyPath,
    "-out",
    certificatePath,
    "-days",
    "1",
    "-subj",
    "/CN=localhost",
    "-addext",
    "subjectAltName=DNS:localhost,IP:127.0.0.1",
  ],
  { encoding: "utf8" },
);

if (certificate.status !== 0) {
  rmSync(certificateDirectory, { recursive: true, force: true });
  throw new Error(
    `Unable to create the real-stack TLS certificate: ${certificate.stderr}`,
  );
}

const server = createServer(
  {
    key: readFileSync(keyPath),
    cert: readFileSync(certificatePath),
  },
  (request, response) => {
    const upstream = httpRequest(
      {
        hostname: target.hostname,
        port: target.port,
        method: request.method,
        path: request.url,
        headers: {
          ...request.headers,
          "x-forwarded-host": request.headers.host,
          "x-forwarded-proto": "https",
        },
      },
      (upstreamResponse) => {
        const responseHeaders = { ...upstreamResponse.headers };
        const location = responseHeaders.location;
        if (typeof location === "string" && /^https?:\/\//.test(location)) {
          const redirect = new URL(location);
          const loopbackTarget =
            redirect.port === target.port &&
            ["localhost", "127.0.0.1", "[::1]"].includes(redirect.hostname);
          if (redirect.host === target.host || loopbackTarget) {
            redirect.protocol = "https:";
            redirect.host = request.headers.host ?? redirect.host;
            responseHeaders.location = redirect.toString();
          } else if (
            redirect.host === request.headers.host &&
            redirect.protocol === "http:"
          ) {
            redirect.protocol = "https:";
            responseHeaders.location = redirect.toString();
          }
        }
        response.writeHead(
          upstreamResponse.statusCode ?? 502,
          responseHeaders,
        );
        upstreamResponse.pipe(response);
      },
    );

    upstream.on("error", () => {
      if (!response.headersSent) {
        response.writeHead(502, {
          "content-type": "text/plain; charset=utf-8",
        });
      }
      response.end("The isolated real-stack web service is unavailable.");
    });
    request.pipe(upstream);
  },
);

function shutdown() {
  server.close(() => {
    rmSync(certificateDirectory, { recursive: true, force: true });
    process.exit(0);
  });
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
process.on("exit", () => {
  rmSync(certificateDirectory, { recursive: true, force: true });
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(
    `Real-stack HTTPS proxy listening on https://localhost:${port}\n`,
  );
});
