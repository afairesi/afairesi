/* oxlint-disable no-magic-numbers -- JSON Pointer offsets and single-element array edits use zero and one. */
/* oxlint-disable func-style, no-use-before-define, one-var, sort-keys, sort-vars -- Keep structural comparison helpers together. */
/* oxlint-disable no-ternary, no-undefined, unicorn/no-null -- Null means an unavailable comparison; undefined means a missing field. */
/* oxlint-disable oxc/no-optional-chaining, oxc/no-rest-spread-properties -- Native modules target modern browsers. */

export function equal(left, right) {
  if (left === right) {
    return true;
  }
  if (!left || !right || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  const keys = Object.keys(left);
  return (
    Array.isArray(left) === Array.isArray(right) &&
    keys.length === Object.keys(right).length &&
    keys.every((key) => Object.hasOwn(right, key) && equal(left[key], right[key]))
  );
}

function patchParent(root, path) {
  let parent = root;
  for (const part of path) {
    if (!parent || !Object.hasOwn(parent, part)) {
      throw new Error("Interface changed while its diff was being read. Refresh to retry.");
    }
    parent = parent[part];
  }
  if (!parent || typeof parent !== "object") {
    throw new Error("Invalid interface patch path.");
  }
  return parent;
}

// oxlint-disable-next-line max-statements -- Validate and undo one native jd JSON Patch operation.
function undoOperation(result, operation) {
  if (operation.op === "test") {
    return result;
  }
  if (!["add", "remove"].includes(operation.op)) {
    throw new Error("Unsupported interface patch operation.");
  }
  if (operation.path === "") {
    return operation.op === "add" ? undefined : structuredClone(operation.value);
  }
  const path = operation.path
      .slice(1)
      .split("/")
      .map((key) => key.replaceAll("~1", "/").replaceAll("~0", "~")),
    key = path.pop(),
    parent = patchParent(result, path);
  if (operation.op === "add" && !equal(parent[key], operation.value)) {
    throw new Error("Interface changed while its diff was being read. Refresh to retry.");
  }
  if (Array.isArray(parent)) {
    const index = Number(key);
    if (!Number.isInteger(index) || index < 0 || index > parent.length) {
      throw new Error("Invalid interface patch index.");
    }
    if (operation.op === "add") {
      parent.splice(index, 1);
    } else {
      parent.splice(index, 0, structuredClone(operation.value));
    }
  } else if (operation.op === "add") {
    Reflect.deleteProperty(parent, key);
  } else {
    Object.defineProperty(parent, key, {
      value: structuredClone(operation.value),
      configurable: true,
      enumerable: true,
      writable: true,
    });
  }
  return result;
}

export function reversePatch(after, patch) {
  let result = structuredClone(after);
  for (const operation of patch.toReversed()) {
    result = undoOperation(result, operation);
  }
  return result;
}

export function repositoryVersions(interfaces, fallback = {}) {
  const working = interfaces?.working?.resources || fallback,
    index = interfaces?.working?.patch ? reversePatch(working, interfaces.working.patch) : null,
    head = index && interfaces?.staged?.patch ? reversePatch(index, interfaces.staged.patch) : null;
  return { working, index, head };
}

export function differences(versions, read) {
  const changes = [];
  for (const [before, after, stage] of [
    ["head", "index", "staged"],
    ["index", "working", "unstaged"],
  ]) {
    if (versions[before] !== null && versions[after] !== null) {
      const previous = read(versions[before]),
        current = read(versions[after]);
      if (!equal(previous, current)) {
        changes.push({ before: previous, after: current, stage });
      }
    }
  }
  return changes;
}

export function versionEntries(versions, read, identify) {
  const entries = new Map();
  for (const version of [versions.working, versions.index, versions.head]) {
    if (version !== null) {
      for (const value of read(version) || []) {
        const key = identify(value);
        if (!entries.has(key)) {
          entries.set(key, {
            key,
            value,
            changes: differences(versions, (root) =>
              (read(root) || []).find((item) => identify(item) === key),
            ),
          });
        }
      }
    }
  }
  return [...entries.values()];
}

export function changeKind(changes) {
  if (changes.length === 0) {
    return "";
  }
  if (changes.at(-1).after === undefined) {
    return "remove";
  }
  return changes[0].before === undefined ? "add" : "modify";
}
