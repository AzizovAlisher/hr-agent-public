import { execFileSync } from "node:child_process";

const report = JSON.parse(
  execFileSync("pnpm", ["licenses", "list", "--prod", "--json"], {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  }),
);

const prohibited = [
  /(^|[\s(])AGPL(?:-|$)/i,
  /(^|[\s(])GPL(?:-|$)/i,
  /(^|[\s(])SSPL(?:-|$)/i,
  /\bBUSL(?:-|$)/i,
  /\bCommons Clause\b/i,
  /\bElastic License\b/i,
  /\bPolyForm\b/i,
  /\bUNLICENSED\b/i,
  /^UNKNOWN$/i,
];

const violations = [];
let packageCount = 0;
for (const [license, packages] of Object.entries(report)) {
  if (!Array.isArray(packages)) {
    violations.push(`${license}: license inventory is not an array`);
    continue;
  }
  for (const dependency of packages) {
    packageCount += 1;
    const declared = String(dependency.license ?? license).trim();
    if (!declared || prohibited.some((pattern) => pattern.test(declared))) {
      const versions = Array.isArray(dependency.versions)
        ? dependency.versions.join(",")
        : "unknown";
      violations.push(`${dependency.name}@${versions}: ${declared || "missing"}`);
    }
  }
}

if (violations.length > 0) {
  console.error("Production dependency license policy failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log(`License policy passed for ${packageCount} production dependencies.`);
