import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, extname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const generatedContract = join(webRoot, "lib/generated/api.ts");
const openApiDocument = resolve(webRoot, "../api/openapi.json");
const generator = join(webRoot, "node_modules/.bin/openapi-typescript");
const legacyTransportCalls = new Set(["apiGet", "apiGetAll", "apiGetAllPage"]);
const neutralTypeReferences = new Set(["Array", "Promise", "ReadonlyArray"]);
function posixPath(value) {
  return value.split(sep).join("/");
}

async function collectSourceFiles(directory, include) {
  const result = [];
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") return result;
    throw error;
  }
  for (const entry of entries) {
    if (["generated", ".next", "node_modules"].includes(entry.name)) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      result.push(...(await collectSourceFiles(path, include)));
    } else if (
      (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) &&
      !entry.name.endsWith(".d.ts") &&
      include(entry.name)
    ) {
      result.push(path);
    }
  }
  return result;
}

async function verifyGeneratedContract() {
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "hr-api-contract-"));
  const regenerated = join(temporaryDirectory, "api.ts");
  try {
    const result = spawnSync(generator, [openApiDocument, "-o", regenerated], {
      cwd: webRoot,
      encoding: "utf8",
    });
    if (result.status !== 0) {
      process.stderr.write(result.stdout ?? "");
      process.stderr.write(result.stderr ?? "");
      throw new Error("OpenAPI TypeScript generation failed");
    }
    const [checkedIn, fresh] = await Promise.all([
      readFile(generatedContract),
      readFile(regenerated),
    ]);
    if (!checkedIn.equals(fresh)) {
      throw new Error(
        "lib/generated/api.ts is stale; run `pnpm generate:api` and commit the result",
      );
    }
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
}

function declarationName(node) {
  return ts.isTypeAliasDeclaration(node) || ts.isInterfaceDeclaration(node)
    ? node.name.text
    : null;
}

function localOnlyTag(sourceFile, node) {
  return ts.getJSDocTags(node).find((tag) =>
    ["ui-only", "local-only"].includes(tag.tagName.getText(sourceFile)),
  );
}

function hasDocumentedLocalOnlyTag(sourceFile, node) {
  const tag = localOnlyTag(sourceFile, node);
  return Boolean(tag && typeof tag.comment === "string" && tag.comment.trim());
}

function referencedTypeNames(node) {
  const names = new Set();
  function visit(current) {
    if (ts.isTypeReferenceNode(current) && ts.isIdentifier(current.typeName)) {
      names.add(current.typeName.text);
    } else if (
      ts.isExpressionWithTypeArguments(current) &&
      ts.isIdentifier(current.expression)
    ) {
      names.add(current.expression.text);
    }
    ts.forEachChild(current, visit);
  }
  visit(node);
  return names;
}

function directlyGenerated(_sourceFile, node) {
  return [...referencedTypeNames(node)].some((name) =>
    /^(?:Api(?:Schema|ResponseSchema|Operation\w*)|components|operations|paths)$/.test(
      name,
    ),
  );
}

function generatedBackedNames(sourceFile, declarations) {
  const generated = new Set(
    declarations
      .filter((node) => directlyGenerated(sourceFile, node))
      .map((node) => declarationName(node)),
  );
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of declarations) {
      const name = declarationName(node);
      if (!name || generated.has(name)) continue;
      const dependencies = referencedTypeNames(node);
      if ([...generated].some((dependency) => dependencies.has(dependency))) {
        generated.add(name);
        changed = true;
      }
    }
  }
  return generated;
}

function declarationLabel(file, sourceFile, node, name) {
  const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
  return `${file}:${line} ${name}`;
}

function visibleTypeParameter(node, name) {
  for (let current = node.parent; current; current = current.parent) {
    if (
      current.typeParameters?.some(
        (parameter) => parameter.name.getText() === name,
      )
    ) {
      return true;
    }
  }
  return false;
}

function typeReferences(typeNode) {
  const references = [];
  function visit(node) {
    if (ts.isTypeReferenceNode(node)) {
      if (ts.isIdentifier(node.typeName)) {
        references.push({ name: node.typeName.text, importedName: null, node });
      } else if (
        ts.isQualifiedName(node.typeName) &&
        ts.isIdentifier(node.typeName.left) &&
        ts.isIdentifier(node.typeName.right)
      ) {
        references.push({
          name: node.typeName.left.text,
          importedName: node.typeName.right.text,
          node,
        });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(typeNode);
  return references;
}

function importedTransportNames(sourceFile) {
  const names = new Map(
    [...legacyTransportCalls].map((name) => [name, name === "apiGetAllPage" ? [0, 1] : [0]]),
  );
  for (const [localName, binding] of importBindings(sourceFile)) {
    if (
      (binding.module === "@/lib/api-client" ||
        binding.module.endsWith("/lib/api-client")) &&
      legacyTransportCalls.has(binding.importedName)
    ) {
      names.set(
        localName,
        binding.importedName === "apiGetAllPage" ? [0, 1] : [0],
      );
    }
  }
  return names;
}

function topLevelGenericFunctions(sourceFile) {
  const functions = new Map();
  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) {
      functions.set(statement.name.text, statement);
      continue;
    }
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (
        ts.isIdentifier(declaration.name) &&
        declaration.initializer &&
        (ts.isArrowFunction(declaration.initializer) ||
          ts.isFunctionExpression(declaration.initializer))
      ) {
        functions.set(declaration.name.text, declaration.initializer);
      }
    }
  }
  return functions;
}

function genericTransportWrappers(sourceFile) {
  const contracts = importedTransportNames(sourceFile);
  const functions = topLevelGenericFunctions(sourceFile);
  let changed = true;
  while (changed) {
    changed = false;
    for (const [name, fn] of functions) {
      const parameters = new Map(
        (fn.typeParameters ?? []).map((parameter, index) => [
          parameter.name.text,
          index,
        ]),
      );
      if (!parameters.size || !fn.body) continue;
      const responsePositions = new Set(contracts.get(name) ?? []);
      function visit(node) {
        if (
          node !== fn &&
          (ts.isFunctionDeclaration(node) ||
            ts.isFunctionExpression(node) ||
            ts.isArrowFunction(node))
        ) {
          return;
        }
        if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
          const calledPositions = contracts.get(node.expression.text) ?? [];
          for (const calledPosition of calledPositions) {
            const typeArgument = node.typeArguments?.[calledPosition];
            if (
              typeArgument &&
              ts.isTypeReferenceNode(typeArgument) &&
              ts.isIdentifier(typeArgument.typeName)
            ) {
              const wrapperPosition = parameters.get(typeArgument.typeName.text);
              if (wrapperPosition !== undefined) responsePositions.add(wrapperPosition);
            }
          }
        }
        ts.forEachChild(node, visit);
      }
      visit(fn.body);
      const previous = contracts.get(name) ?? [];
      const next = [...responsePositions].sort((left, right) => left - right);
      if (
        next.length !== previous.length ||
        next.some((position, index) => position !== previous[index])
      ) {
        contracts.set(name, next);
        changed = true;
      }
    }
  }
  return contracts;
}

function transportTypeViolations(
  file,
  sourceFile,
  declarations,
  generatedNames,
  resolveImportedReference,
  resolveTransportCall,
) {
  const byName = new Map(
    declarations.map((declaration) => [declarationName(declaration), declaration]),
  );
  const transportContracts = genericTransportWrappers(sourceFile);
  const violations = [];
  const taintedNamesByScope = new Map();
  const rawUntypedNamesByScope = new Map();

  function scopeRoot(node) {
    for (let current = node; current; current = current.parent) {
      if (
        ts.isFunctionDeclaration(current) ||
        ts.isFunctionExpression(current) ||
        ts.isArrowFunction(current) ||
        ts.isMethodDeclaration(current)
      ) {
        return current;
      }
    }
    return sourceFile;
  }

  function taintedNames(scope) {
    const existing = taintedNamesByScope.get(scope);
    if (existing) return existing;
    const names = collectTaintedNames(scope, (call) => {
      const localPositions = ts.isIdentifier(call.expression)
        ? transportContracts.get(call.expression.text) ?? []
        : [];
      const resolvedPositions = resolveTransportCall?.(call)?.positions ?? [];
      return localPositions.length > 0 || resolvedPositions.length > 0;
    });
    taintedNamesByScope.set(scope, names);
    return names;
  }

  function isTransportResponse(node) {
    const scope = scopeRoot(node);
    return isTransportResponseExpression(
      node,
      taintedNames(scope),
      (call) => {
        const localPositions = ts.isIdentifier(call.expression)
          ? transportContracts.get(call.expression.text) ?? []
          : [];
        const resolvedPositions = resolveTransportCall?.(call)?.positions ?? [];
        return localPositions.length > 0 || resolvedPositions.length > 0;
      },
    );
  }

  function resolvedPositions(call) {
    const localPositions = ts.isIdentifier(call.expression)
      ? transportContracts.get(call.expression.text) ?? []
      : [];
    const importedPositions = resolveTransportCall?.(call)?.positions ?? [];
    return [...new Set([...localPositions, ...importedPositions])];
  }

  function hasMissingResponseArgument(call) {
    const positions = resolvedPositions(call);
    return (
      positions.length > 0 &&
      positions.some((position) => !call.typeArguments?.[position])
    );
  }

  function rawUntypedNames(scope) {
    const existing = rawUntypedNamesByScope.get(scope);
    if (existing) return existing;
    const names = collectTaintedNames(scope, hasMissingResponseArgument);
    rawUntypedNamesByScope.set(scope, names);
    return names;
  }

  function needsReturnContract(node) {
    const scope = scopeRoot(node);
    return isTransportResponseExpression(
      node,
      rawUntypedNames(scope),
      hasMissingResponseArgument,
    );
  }
  function inspectResponseType(typeNode, context) {
    const line =
      sourceFile.getLineAndCharacterOfPosition(typeNode.getStart(sourceFile)).line + 1;
    if (typeNode.getText(sourceFile).includes("{")) {
      violations.push(`${file}:${line} inline response type used by ${context}`);
    }
    const forbiddenKeywords = new Set();
    function findForbiddenKeywords(node) {
      if (node.kind === ts.SyntaxKind.AnyKeyword) forbiddenKeywords.add("any");
      if (node.kind === ts.SyntaxKind.UnknownKeyword) forbiddenKeywords.add("unknown");
      if (node.kind === ts.SyntaxKind.ObjectKeyword) forbiddenKeywords.add("object");
      ts.forEachChild(node, findForbiddenKeywords);
    }
    findForbiddenKeywords(typeNode);
    for (const keyword of forbiddenKeywords) {
      violations.push(`${file}:${line} ${keyword} response type used by ${context}`);
    }
    for (const reference of typeReferences(typeNode)) {
      if (visibleTypeParameter(reference.node, reference.name)) continue;
      if (!reference.importedName && neutralTypeReferences.has(reference.name)) {
        continue;
      }
      const declaration = byName.get(reference.name);
      if (declaration) {
        if (generatedNames.has(reference.name)) continue;
        violations.push(
          `${declarationLabel(file, sourceFile, declaration, reference.name)} ` +
            `(used as ${context} response type)`,
        );
        continue;
      }
      const imported = resolveImportedReference?.(
        reference.name,
        reference.importedName,
      );
      if (imported === undefined || imported === "generated") continue;
      violations.push(
        `${file}:${line} ${reference.name}${
          reference.importedName ? `.${reference.importedName}` : ""
        } (used as ${context} response type; import provenance is ${imported})`,
      );
    }
  }
  function visit(node) {
    if (ts.isCallExpression(node)) {
      const localPositions = ts.isIdentifier(node.expression)
        ? transportContracts.get(node.expression.text) ?? []
        : [];
      const resolvedCall = resolveTransportCall?.(node);
      const positions = [
        ...new Set([...localPositions, ...(resolvedCall?.positions ?? [])]),
      ];
      const callLabel =
        resolvedCall?.label ?? node.expression.getText(sourceFile);
      for (const position of positions) {
        const typeNode = node.typeArguments?.[position];
        if (typeNode) inspectResponseType(typeNode, callLabel);
      }
    }
    if (
      (ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) &&
      isTransportResponse(node.expression)
    ) {
      inspectResponseType(node.type, "response.json()");
    }
    if (
      ts.isSatisfiesExpression(node) &&
      isTransportResponse(node.expression)
    ) {
      inspectResponseType(node.type, "response.json() satisfies");
    }
    if (
      ts.isVariableDeclaration(node) &&
      node.type &&
      node.initializer &&
      isTransportResponse(node.initializer)
    ) {
      inspectResponseType(node.type, "response.json() assignment");
    }
    if (
      ts.isReturnStatement(node) &&
      node.expression &&
      needsReturnContract(node.expression)
    ) {
      for (let current = node.parent; current; current = current.parent) {
        if (
          (ts.isFunctionDeclaration(current) ||
            ts.isFunctionExpression(current) ||
            ts.isArrowFunction(current) ||
            ts.isMethodDeclaration(current)) &&
          current.type
        ) {
          inspectResponseType(current.type, "response.json() return annotation");
          break;
        }
      }
    }
    if (
      (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) &&
      node.type &&
      !ts.isBlock(node.body) &&
      needsReturnContract(node.body)
    ) {
      inspectResponseType(node.type, "response.json() return annotation");
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return violations;
}

function isJsonResponseExpression(node) {
  let expression = node;
  while (
    ts.isParenthesizedExpression(expression) ||
    ts.isAwaitExpression(expression) ||
    ts.isAsExpression(expression) ||
    ts.isTypeAssertionExpression(expression) ||
    ts.isSatisfiesExpression(expression)
  ) {
    expression = expression.expression;
  }
  return (
    ts.isCallExpression(expression) &&
    expression.arguments.length === 0 &&
    ts.isPropertyAccessExpression(expression.expression) &&
    expression.expression.name.text === "json"
  );
}

function isFunctionLike(node) {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node)
  );
}

function isTransportResponseExpression(node, taintedNames, isTransportCall) {
  let expression = node;
  while (
    ts.isParenthesizedExpression(expression) ||
    ts.isAwaitExpression(expression) ||
    ts.isAsExpression(expression) ||
    ts.isTypeAssertionExpression(expression) ||
    ts.isSatisfiesExpression(expression)
  ) {
    expression = expression.expression;
  }
  if (isJsonResponseExpression(expression)) return true;
  if (ts.isIdentifier(expression)) return taintedNames.has(expression.text);
  if (ts.isCallExpression(expression)) return isTransportCall(expression);
  if (ts.isConditionalExpression(expression)) {
    return (
      isTransportResponseExpression(
        expression.whenTrue,
        taintedNames,
        isTransportCall,
      ) ||
      isTransportResponseExpression(
        expression.whenFalse,
        taintedNames,
        isTransportCall,
      )
    );
  }
  return false;
}

function collectTaintedNames(scope, isTransportCall) {
  const names = new Set();
  const root = ts.isSourceFile(scope) ? scope : scope.body;
  if (!root) return names;
  let changed = true;
  while (changed) {
    changed = false;
    function visit(node) {
      if (node !== root && isFunctionLike(node)) return;
      if (
        ts.isVariableDeclaration(node) &&
        ts.isIdentifier(node.name) &&
        node.initializer &&
        isTransportResponseExpression(node.initializer, names, isTransportCall) &&
        !names.has(node.name.text)
      ) {
        names.add(node.name.text);
        changed = true;
      }
      if (
        ts.isBinaryExpression(node) &&
        node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
        ts.isIdentifier(node.left) &&
        isTransportResponseExpression(node.right, names, isTransportCall) &&
        !names.has(node.left.text)
      ) {
        names.add(node.left.text);
        changed = true;
      }
      ts.forEachChild(node, visit);
    }
    visit(root);
  }
  return names;
}

function parseBoundarySource(file, text) {
  const scriptKind = extname(file) === ".tsx" ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(
    file,
    text,
    ts.ScriptTarget.Latest,
    true,
    scriptKind,
  );
  const declarations = sourceFile.statements.filter(
    (node) => declarationName(node) !== null,
  );
  const generatedNames = generatedBackedNames(sourceFile, declarations);
  return { sourceFile, declarations, generatedNames };
}

export function analyzeBoundarySource(
  file,
  text,
  resolveImportedReference,
  resolveTransportCall,
  enforceDeclarations = true,
) {
  const { sourceFile, declarations, generatedNames } = parseBoundarySource(file, text);
  const violations = [];
  let generatedCount = 0;
  let viewModelCount = 0;
  for (const node of declarations) {
    const name = declarationName(node);
    if (!name) continue;
    if (generatedNames.has(name)) {
      generatedCount += 1;
    } else if (hasDocumentedLocalOnlyTag(sourceFile, node)) {
      viewModelCount += 1;
    } else if (enforceDeclarations) {
      violations.push(declarationLabel(file, sourceFile, node, name));
    }
  }
  violations.push(
    ...transportTypeViolations(
      file,
      sourceFile,
      declarations,
      generatedNames,
      resolveImportedReference,
      resolveTransportCall,
    ),
  );
  return { violations, generatedCount, viewModelCount };
}

function declarationBoundary(file) {
  if (file.startsWith("app/")) {
    return (
      file.endsWith(".tsx") ||
      file.endsWith("actions.ts") ||
      file.endsWith("/types.ts") ||
      file.endsWith("/route.ts")
    );
  }
  if (file.startsWith("components/")) return file.endsWith(".tsx");
  return file.startsWith("lib/") && file.endsWith(".ts");
}

function importBindings(sourceFile) {
  const bindings = new Map();
  for (const statement of sourceFile.statements) {
    if (
      !ts.isImportDeclaration(statement) ||
      !ts.isStringLiteral(statement.moduleSpecifier) ||
      !statement.importClause
    ) {
      continue;
    }
    const module = statement.moduleSpecifier.text;
    if (statement.importClause.name) {
      bindings.set(statement.importClause.name.text, {
        module,
        importedName: "default",
      });
    }
    const named = statement.importClause.namedBindings;
    if (named && ts.isNamespaceImport(named)) {
      bindings.set(named.name.text, { module, importedName: "*" });
    } else if (named && ts.isNamedImports(named)) {
      for (const element of named.elements) {
        bindings.set(element.name.text, {
          module,
          importedName: element.propertyName?.text ?? element.name.text,
        });
      }
    }
  }
  return bindings;
}

function resolveSourceModule(root, importer, module, records) {
  if (!module.startsWith(".") && !module.startsWith("@/")) return null;
  const base = module.startsWith("@/")
    ? resolve(root, module.slice(2))
    : resolve(dirname(importer), module);
  const candidates = [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    join(base, "index.ts"),
    join(base, "index.tsx"),
  ];
  for (const candidate of candidates) {
    const record = records.get(resolve(candidate));
    if (record) return record;
  }
  return null;
}

function generatedModule(root, importer, module) {
  if (module === "@/lib/generated/api" || module === "@/lib/generated/contracts") {
    return true;
  }
  if (!module.startsWith(".") && !module.startsWith("@/")) return false;
  const base = module.startsWith("@/")
    ? resolve(root, module.slice(2))
    : resolve(dirname(importer), module);
  const generatedRoot = resolve(root, "lib/generated");
  return base === generatedRoot || base.startsWith(`${generatedRoot}${sep}`);
}

function declarationForName(record, name) {
  return record.declarations.find((node) => declarationName(node) === name) ?? null;
}

function classifyImportedSymbol(
  record,
  localName,
  requestedImportedName,
  root,
  records,
  seen = new Set(),
) {
  const binding = record.imports.get(localName);
  if (!binding) return "unresolved";
  if (generatedModule(root, record.absoluteFile, binding.module)) return "generated";
  const target = resolveSourceModule(
    root,
    record.absoluteFile,
    binding.module,
    records,
  );
  if (!target) return "unresolved";
  const importedName =
    binding.importedName === "*" ? requestedImportedName : binding.importedName;
  if (!importedName) return "unresolved";
  const key = `${target.absoluteFile}#${importedName}`;
  if (seen.has(key)) return "unresolved";
  seen.add(key);
  const declaration = declarationForName(target, importedName);
  if (declaration) {
    if (target.generatedNames.has(importedName)) return "generated";
    if (hasDocumentedLocalOnlyTag(target.sourceFile, declaration)) {
      return "documented local/UI-only";
    }
    return "handwritten";
  }
  return classifyImportedSymbol(
    target,
    importedName,
    null,
    root,
    records,
    seen,
  );
}

function apiClientModule(module) {
  return module === "@/lib/api-client" || module.endsWith("/lib/api-client");
}

function mergeContract(contracts, name, positions) {
  const previous = contracts.get(name) ?? [];
  const next = [...new Set([...previous, ...positions])].sort(
    (left, right) => left - right,
  );
  if (
    next.length === previous.length &&
    next.every((position, index) => position === previous[index])
  ) {
    return false;
  }
  contracts.set(name, next);
  return true;
}

function allGenericFunctions(sourceFile) {
  const result = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name) {
      result.push({ name: node.name.text, node });
    } else if (
      (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) &&
      ts.isVariableDeclaration(node.parent) &&
      ts.isIdentifier(node.parent.name)
    ) {
      result.push({ name: node.parent.name.text, node });
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return result;
}

function allCallableAliases(sourceFile) {
  const result = [];
  function visit(node) {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      !ts.isArrowFunction(node.initializer) &&
      !ts.isFunctionExpression(node.initializer)
    ) {
      let initializer = node.initializer;
      while (ts.isParenthesizedExpression(initializer)) {
        initializer = initializer.expression;
      }
      if (ts.isIdentifier(initializer) || ts.isPropertyAccessExpression(initializer)) {
        result.push({ name: node.name.text, initializer });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return result;
}

function resolveCallableExpression(
  record,
  expression,
  contractsByFile,
  root,
  records,
) {
  const localContracts = contractsByFile.get(record.absoluteFile) ?? new Map();
  if (ts.isIdentifier(expression)) {
    const local = localContracts.get(expression.text);
    if (local) return local;
    const binding = record.imports.get(expression.text);
    if (!binding) return null;
    if (
      apiClientModule(binding.module) &&
      legacyTransportCalls.has(binding.importedName)
    ) {
      return binding.importedName === "apiGetAllPage" ? [0, 1] : [0];
    }
    const target = resolveSourceModule(
      root,
      record.absoluteFile,
      binding.module,
      records,
    );
    if (!target || binding.importedName === "*") return null;
    return (
      contractsByFile.get(target.absoluteFile)?.get(binding.importedName) ?? null
    );
  }
  if (
    ts.isPropertyAccessExpression(expression) &&
    ts.isIdentifier(expression.expression)
  ) {
    const binding = record.imports.get(expression.expression.text);
    if (!binding || binding.importedName !== "*") return null;
    if (
      apiClientModule(binding.module) &&
      legacyTransportCalls.has(expression.name.text)
    ) {
      return expression.name.text === "apiGetAllPage" ? [0, 1] : [0];
    }
    const target = resolveSourceModule(
      root,
      record.absoluteFile,
      binding.module,
      records,
    );
    if (!target) return null;
    return (
      contractsByFile.get(target.absoluteFile)?.get(expression.name.text) ?? null
    );
  }
  return null;
}

function buildTransportContracts(records, root) {
  const contractsByFile = new Map(
    [...records.values()].map((record) => [record.absoluteFile, new Map()]),
  );
  const functionsByFile = new Map(
    [...records.values()].map((record) => [
      record.absoluteFile,
      allGenericFunctions(record.sourceFile),
    ]),
  );
  const aliasesByFile = new Map(
    [...records.values()].map((record) => [
      record.absoluteFile,
      allCallableAliases(record.sourceFile),
    ]),
  );

  for (let iteration = 0; iteration < 500; iteration += 1) {
    let changed = false;
    for (const record of records.values()) {
      const contracts = contractsByFile.get(record.absoluteFile);
      for (const [localName, binding] of record.imports) {
        if (
          apiClientModule(binding.module) &&
          legacyTransportCalls.has(binding.importedName)
        ) {
          changed =
            mergeContract(
              contracts,
              localName,
              binding.importedName === "apiGetAllPage" ? [0, 1] : [0],
            ) || changed;
        }
      }
      for (const alias of aliasesByFile.get(record.absoluteFile)) {
        const positions = resolveCallableExpression(
          record,
          alias.initializer,
          contractsByFile,
          root,
          records,
        );
        if (positions) {
          changed = mergeContract(contracts, alias.name, positions) || changed;
        }
      }
      for (const fn of functionsByFile.get(record.absoluteFile)) {
        if (!fn.node.body || !fn.node.typeParameters?.length) continue;
        const parameters = new Map(
          fn.node.typeParameters.map((parameter, index) => [
            parameter.name.text,
            index,
          ]),
        );
        const positions = new Set(contracts.get(fn.name) ?? []);
        function markTypeParameters(typeNode) {
          if (!typeNode) return;
          for (const reference of typeReferences(typeNode)) {
            if (reference.importedName) continue;
            const position = parameters.get(reference.name);
            if (position !== undefined) positions.add(position);
          }
        }
        const isResolvedTransportCall = (call) =>
          Boolean(
            resolveCallableExpression(
              record,
              call.expression,
              contractsByFile,
              root,
              records,
            )?.length,
          );
        const taintedNames = collectTaintedNames(
          fn.node,
          isResolvedTransportCall,
        );
        const isTransportResponse = (expression) =>
          isTransportResponseExpression(
            expression,
            taintedNames,
            isResolvedTransportCall,
          );
        function visit(node) {
          if (
            node !== fn.node.body &&
            (ts.isFunctionDeclaration(node) ||
              ts.isFunctionExpression(node) ||
              ts.isArrowFunction(node))
          ) {
            return;
          }
          if (ts.isCallExpression(node)) {
            const calledPositions = resolveCallableExpression(
              record,
              node.expression,
              contractsByFile,
              root,
              records,
            ) ?? [];
            for (const calledPosition of calledPositions) {
              const argument = node.typeArguments?.[calledPosition];
              if (!argument) continue;
              for (const reference of typeReferences(argument)) {
                if (reference.importedName) continue;
                const position = parameters.get(reference.name);
                if (position !== undefined) positions.add(position);
              }
            }
          }
          if (
            (ts.isAsExpression(node) ||
              ts.isTypeAssertionExpression(node) ||
              ts.isSatisfiesExpression(node)) &&
            isTransportResponse(node.expression)
          ) {
            markTypeParameters(node.type);
          }
          if (
            ts.isVariableDeclaration(node) &&
            node.type &&
            node.initializer &&
            isTransportResponse(node.initializer)
          ) {
            markTypeParameters(node.type);
          }
          if (
            ts.isReturnStatement(node) &&
            node.expression &&
            isTransportResponse(node.expression)
          ) {
            markTypeParameters(fn.node.type);
          }
          ts.forEachChild(node, visit);
        }
        visit(fn.node.body);
        if (!ts.isBlock(fn.node.body) && isTransportResponse(fn.node.body)) {
          markTypeParameters(fn.node.type);
        }
        changed = mergeContract(contracts, fn.name, positions) || changed;
      }
    }
    if (!changed) {
      return {
        resolveCall(record, call) {
          const positions = resolveCallableExpression(
            record,
            call.expression,
            contractsByFile,
            root,
            records,
          );
          return positions
            ? { label: call.expression.getText(), positions }
            : null;
        },
      };
    }
  }
  throw new Error("Transport wrapper provenance did not converge");
}

export async function verifyBoundaryDeclarations(root = webRoot) {
  const files = await discoverBoundaryFiles(root);
  const violations = [];
  let generatedCount = 0;
  let viewModelCount = 0;
  const records = new Map();

  for (const absoluteFile of [...new Set(files)].sort()) {
    const file = posixPath(relative(root, absoluteFile));
    const text = await readFile(absoluteFile, "utf8");
    const parsed = parseBoundarySource(file, text);
    records.set(resolve(absoluteFile), {
      absoluteFile: resolve(absoluteFile),
      file,
      text,
      ...parsed,
      imports: importBindings(parsed.sourceFile),
    });
  }

  const transportContracts = buildTransportContracts(records, root);

  for (const record of records.values()) {
    const result = analyzeBoundarySource(
      record.file,
      record.text,
      (name, importedName) =>
        classifyImportedSymbol(record, name, importedName, root, records),
      (call) => transportContracts.resolveCall(record, call),
      declarationBoundary(record.file),
    );
    generatedCount += result.generatedCount;
    viewModelCount += result.viewModelCount;
    violations.push(...result.violations);
  }

  if (violations.length) {
    throw new Error(
      [
        "Handwritten API-boundary declarations found.",
        "Alias wire payloads from lib/generated/contracts.ts or document genuine local/view state with a direct @local-only or @ui-only tag and a reason:",
        ...violations.map((item) => `  - ${item}`),
      ].join("\n"),
    );
  }
  return { generatedCount, viewModelCount };
}

export async function discoverBoundaryFiles(root = webRoot) {
  const sourceFile = (name) => name.endsWith(".ts") || name.endsWith(".tsx");
  return [
    ...(await collectSourceFiles(join(root, "app"), sourceFile)),
    ...(await collectSourceFiles(join(root, "components"), sourceFile)),
    ...(await collectSourceFiles(join(root, "lib"), sourceFile)),
  ];
}

export async function main() {
  await verifyGeneratedContract();
  const counts = await verifyBoundaryDeclarations();
  console.log("OpenAPI artifact is current; handwritten API-wire DTO residual: 0.");
  console.log(
    `Reviewed boundary declarations: ${counts.generatedCount} generated-backed, ${counts.viewModelCount} explicitly normalized/UI-only.`,
  );
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  try {
    await main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}
