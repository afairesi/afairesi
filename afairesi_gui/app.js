/* oxlint-disable id-length -- SVG coordinates use x and y attributes. */
/* oxlint-disable func-style, no-use-before-define, one-var, sort-keys, sort-vars -- Keep named DOM renderers and state grouped by responsibility. */
/* oxlint-disable no-ternary, no-nested-ternary, no-undefined, unicorn/no-null, unicorn/no-nested-ternary -- Render optional JSON fields and conditional UI labels directly. */
/* oxlint-disable max-lines, max-lines-per-function, max-statements, complexity, no-magic-numbers, unicorn/max-nested-calls -- This buildless interface constructs its DOM in one module. */
/* oxlint-disable oxc/no-async-await, oxc/no-optional-chaining, oxc/no-rest-spread-properties -- Target modern browsers with native modules and fetch. */

import {
  changeKind,
  differences,
  equal,
  repositoryVersions,
  versionEntries,
} from "/differences.js";
const select = (selector) => document.querySelector(selector),
  state = {
    directory: "",
    diffs: new Map(),
    views: new Map(),
    diffLoading: new Set(),
    epoch: 0,
    reports: new Map(),
    sections: new Map(),
    refreshing: false,
    repositories: [],
    zoom: 1,
  };
const observer = new ResizeObserver(() => requestAnimationFrame(drawEdges));
const iconPaths = {
  refresh: ["M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"],
  fit: ["M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"],
  plus: ["M5 12h14M12 5v14"],
  minus: ["M5 12h14"],
  folder: ["M3 6h7l2 3h9v11H3Z"],
  repository: ["M5 3h14v18l-4-2-4 2V3M5 3v18"],
  package: ["m12 2 9 5v10l-9 5-9-5V7Zm-9 5 9 5 9-5M12 12v10M7 5l10 5"],
  python: [
    "M12 3H8q-3 0-3 3v3h9v3H6q-3 0-3 3v1q0 3 3 3h3v-5h9q3 0 3-3V8q0-3-3-3h-3v4M12 21h4q3 0 3-3v-3M9 6h.01M15 18h.01",
  ],
  nix: [
    "M12 2v20M3.34 7l17.32 10M3.34 17 20.66 7M8 4l4 4 4-4M4 11l5 1-1 5m8 3-4-4-4 4m12-7-5-1 1-5",
  ],
  html: ["m8 5-6 7 6 7m8-14 6 7-6 7M14 3l-4 18"],
  latex: ["M19 4H5l8 8-8 8h14"],
  lean4: ["m5 3 7 18L19 3M8 11h8"],
  host: ["M3 3h18v7H3Zm0 11h18v7H3ZM6 6.5h.01M6 17.5h.01M11 6.5h7M11 17.5h7"],
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    class: "icon",
    "aria-hidden": "true",
    focusable: "false",
  })) {
    svg.setAttribute(key, value);
  }
  for (const data of iconPaths[name] || iconPaths.package) {
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", data);
    svg.append(path);
  }
  return svg;
}
function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on")) {
      node.addEventListener(key.slice(2), value);
    } else if (key === "class") {
      node.className = value;
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

async function api(path) {
  const response = await fetch(path),
    result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || `Request failed (${response.status})`);
  }
  return result;
}

function query(path, repository) {
  return `${path}?${new URLSearchParams({ repository })}`;
}

function notice(message = "") {
  select("#repository-notice").textContent = message;
  select("#repository-notice").hidden = !message;
}

function empty(message) {
  return element("div", { class: "empty-state" }, element("p", {}, message));
}

function renderHeading() {
  const count = [...state.reports.values()].reduce(
    (total, report) => total + Object.keys(report.resources || {}).length,
    0,
  );
  document.title = `afairesi — ${state.directory || "home"}`;
  const directory = select("#current-directory");
  directory.textContent = `~/${state.directory}${state.directory ? "/" : ""}`;
  directory.title = directory.textContent;
  directory.hidden = !state.directory;
  select("#graph-status").textContent =
    `${state.repositories.length} registered repositories · ${count} resources${state.refreshing ? " · loading…" : ""}`;
  select("#home").title = select("#graph-status").textContent;
}

async function inBatches(items, operation) {
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(3, items.length) }, async () => {
      while (cursor < items.length) {
        const item = items[cursor];
        cursor += 1;
        // oxlint-disable-next-line no-await-in-loop -- Three workers cap concurrent repository requests.
        await operation(item);
      }
    }),
  );
}

async function loadWorkspace() {
  if (state.refreshing) {
    return;
  }
  state.refreshing = true;
  state.epoch += 1;
  const { epoch } = state;
  select("#refresh").disabled = true;
  notice();
  try {
    const workspace = await api("/api/workspace");
    state.repositories = workspace.repositories;
    for (const identifier of state.reports.keys()) {
      if (!state.repositories.some((repository) => repository.id === identifier)) {
        state.reports.delete(identifier);
        state.diffs.delete(identifier);
      }
    }
    renderResources();
    renderHeading();
    await inBatches(state.repositories, async (repository) => {
      await loadRepository(repository, epoch);
    });
    await inBatches(state.repositories, (repository) => loadDiff(repository.id, epoch));
  } catch (error) {
    notice(error.message);
  } finally {
    state.refreshing = false;
    select("#refresh").disabled = false;
    renderHeading();
    filterBoxes();
  }
}

async function loadRepository(repository, epoch) {
  if (!repository.initialized) {
    return;
  }
  try {
    const report = await api(query("/api/repository", repository.id));
    if (epoch !== state.epoch) {
      return;
    }
    state.reports.set(repository.id, report);
    renderRepository(repository);
    renderHeading();
  } catch (error) {
    if (epoch === state.epoch) {
      state.reports.set(repository.id, { resources: {}, inspection_error: error.message });
      renderRepository(repository);
    }
  }
}

function home() {
  select("#search").value = "";
  navigate("");
}

function navigate(directory) {
  state.directory = directory;
  renderResources();
  renderHeading();
  zoom(1);
  select("#viewport").scrollTo(0, 0);
}

function directoryName(name, directory) {
  return element(
    "button",
    {
      type: "button",
      class: "box-name",
      "data-directory": directory,
      title: `Open ~/${directory}/`,
      onclick: () => navigate(directory),
    },
    name,
  );
}

function kinds(resource) {
  return Array.isArray(resource.type) ? resource.type : [resource.type];
}
function targetKey(target) {
  return JSON.stringify([target.repository, target.name]);
}
function sectionState(target) {
  const key = targetKey(target);
  if (!state.sections.has(key)) {
    state.sections.set(key, {});
  }
  return state.sections.get(key);
}

function repositoryElement(identifier) {
  return [...select("#boxes").querySelectorAll(".repository-box")].find(
    (box) => box.dataset.repository === identifier,
  );
}

function renderResources() {
  observer.disconnect();
  const container = select("#boxes"),
    folders = new Map([["", container]]),
    repositories = state.repositories.filter(
      (repository) =>
        !state.directory ||
        repository.id === state.directory ||
        repository.id.startsWith(`${state.directory}/`) ||
        state.directory.startsWith(`${repository.id}/`),
    );
  container.replaceChildren();
  container.className = "workspace-boxes";
  for (const repository of repositories) {
    const parts = repository.path.split("/"),
      depth = state.directory ? state.directory.split("/").length - 1 : 0,
      directories = parts.slice(depth, -1);
    let path = parts.slice(0, depth).join("/"),
      parent = container;
    for (const directory of directories) {
      path = path ? `${path}/${directory}` : directory;
      if (!folders.has(path)) {
        const children = element("div", { class: "directory-children" }),
          box = element(
            "section",
            { class: "directory-box", "data-path": path, "aria-label": `${path}/` },
            element(
              "h2",
              { class: "directory-heading" },
              icon("folder"),
              directoryName(`${directory}/`, path),
            ),
            children,
          );
        parent.append(box);
        folders.set(path, children);
      }
      parent = folders.get(path);
    }
    const box = element("section", {
      class: "repository-box",
      "data-repository": repository.id,
      "aria-label": repository.path,
    });
    parent.append(box);
    renderRepository(repository, box);
    if (state.directory.startsWith(`${repository.id}/`)) {
      const resource = [...box.querySelectorAll(".resource-box")].find(
        (node) => `${repository.id}/${node.dataset.resource}` === state.directory,
      );
      if (resource) {
        for (const error of box.querySelectorAll(".diff-error")) {
          resource.querySelector(".box-body").append(error);
        }
        container.replaceChildren(resource);
      } else {
        container.replaceChildren(empty("No resource at this path."));
      }
    }
  }
  if (container.children.length === 0) {
    container.append(
      empty(
        state.directory
          ? "No registered repositories at this path."
          : "No registered home submodules.",
      ),
    );
  }
  observer.observe(container);
  filterBoxes();
}

function renderRepository(repository, target = repositoryElement(repository.id)) {
  if (!target) {
    if (state.directory.startsWith(`${repository.id}/`)) {
      renderResources();
    }
    return;
  }
  const report = state.reports.get(repository.id),
    view = repositoryVersions(state.diffs.get(repository.id), report?.resources || {}),
    resources = Object.fromEntries(
      [view.head, view.index, view.working].flatMap((root) => Object.entries(root || {})),
    ),
    graph = element("div", { class: "graph-columns" }),
    header = element(
      "header",
      { class: "repository-heading" },
      element("h2", {}, icon("repository"), directoryName(`${repository.name}/`, repository.id)),
      element(
        "p",
        { class: "path" },
        report?.branch
          ? `${report.branch} · ${report.dirty ? "uncommitted changes" : "clean"} · ${Object.keys(resources).length} resources`
          : repository.path,
      ),
    );
  state.views.set(repository.id, view);
  target.replaceChildren(header, graph);
  const levels = new Map(),
    level = (name, ancestors = new Set()) => {
      if (levels.has(name)) {
        return levels.get(name);
      }
      if (ancestors.has(name)) {
        return 0;
      }
      const next = new Set([...ancestors, name]),
        dependencies = (resources[name].dependencies || []).filter((edge) =>
          Object.hasOwn(resources, edge.resource),
        ),
        result =
          dependencies.length > 0
            ? 1 + Math.max(...dependencies.map((edge) => level(edge.resource, next)))
            : 0;
      levels.set(name, result);
      return result;
    },
    columns = new Map();
  for (const [name, resource] of Object.entries(resources)) {
    const rank = level(name);
    if (!columns.has(rank)) {
      columns.set(rank, element("div", { class: "graph-column" }));
    }
    columns.get(rank).append(resourceBox({ repository: repository.id, name }, resource));
  }
  for (const [, column] of [...columns.entries()].toSorted(([left], [right]) => left - right)) {
    graph.append(column);
  }
  if (report?.inspection_error) {
    graph.append(empty(report.inspection_error));
  } else if (columns.size === 0) {
    graph.append(
      empty(
        repository.initialized
          ? report
            ? "No resources reported."
            : "Reading resources…"
          : "Submodule not initialized.",
      ),
    );
  }
  renderDiffError(repository.id);
  filterBoxes();
}

function filterBoxes() {
  const filter = select("#search").value.toLowerCase(),
    diffOnly = select("#diff-only").checked;
  select("#viewport").classList.toggle("diff-only", diffOnly);
  const groups = [
    ...select("#boxes").querySelectorAll(".repository-box"),
    ...select("#boxes").querySelectorAll(":scope > .resource-box"),
  ];
  for (const repository of groups) {
    const standalone = repository.classList.contains("resource-box");
    const matches = repository.dataset.repository.toLowerCase().includes(filter),
      resources = standalone ? [repository] : [...repository.querySelectorAll(".resource-box")];
    for (const resource of resources) {
      resource.classList.remove("diff-anchor");
      resource.hidden =
        (!matches && !resource.dataset.search.includes(filter)) ||
        (diffOnly && !resource.dataset.change && !resource.querySelector(".diff-error"));
    }
    if (diffOnly) {
      showChangedDependencyEndpoints(repository.dataset.repository, resources);
    }
    for (const column of repository.querySelectorAll(".graph-column")) {
      column.hidden = [...column.children].every((box) => box.hidden);
    }
    if (!standalone) {
      repository.hidden = diffOnly
        ? !resources.some((box) => !box.hidden) && !repository.querySelector(".diff-error")
        : !matches && !resources.some((box) => !box.hidden);
    }
  }
  for (const directory of [...select("#boxes").querySelectorAll(".directory-box")].toReversed()) {
    directory.hidden = [...directory.querySelectorAll(".repository-box")].every(
      (box) => box.hidden,
    );
  }
  const status = select("#diff-status"),
    visible = groups.some((box) => !box.hidden);
  status.hidden = !diffOnly || visible;
  status.textContent =
    state.refreshing || state.diffLoading.size > 0
      ? "Reading interface differences…"
      : filter
        ? "No interface differences match this search."
        : "No interface differences.";
  requestAnimationFrame(drawEdges);
}

function showChangedDependencyEndpoints(repository, resources) {
  const targets = resources.filter((box) => !box.hidden),
    nodes = new Map(resources.map((box) => [box.dataset.resource, box]));
  for (const box of targets) {
    const dependencies = versionEntries(
      state.views.get(repository),
      (root) => root[box.dataset.resource]?.dependencies,
      (dependency) => JSON.stringify([dependency.resource, dependency.kind]),
    );
    for (const entry of dependencies.filter((item) => item.changes.length > 0)) {
      const source = nodes.get(entry.value.resource);
      if (source?.hidden) {
        source.hidden = false;
        source.classList.add("diff-anchor");
      }
    }
  }
}

function resourceSection(target, kind, title) {
  const saved = sectionState(target),
    section = element(
      "details",
      { class: `resource-section ${kind}-section`, "data-section": kind },
      element("summary", {}, title),
    );
  section.open = Boolean(saved[`${kind}Open`]);
  section.addEventListener("toggle", () => {
    saved[`${kind}Open`] = section.open;
    drawEdges();
  });
  return section;
}

function testLabel(identifier) {
  return identifier
    .split("::")
    .map((part) => {
      const words = part
        .replace(/^test(?:_|$)/u, "")
        .replaceAll("_", " ")
        .trim();
      return words ? words[0].toUpperCase() + words.slice(1) : "Test";
    })
    .join(": ");
}

function markChange(node, changes) {
  const kind = changeKind(changes);
  if (kind) {
    node.classList.add(`change-${kind}`);
    node.dataset.change = kind;
    node.dataset.stages = [...new Set(changes.map((change) => change.stage))].join(" / ");
    node.title = `${{ add: "Added", remove: "Removed", modify: "Changed" }[kind]} · ${node.dataset.stages}`;
  }
  return node;
}

function readableValue(value) {
  if (value === undefined) {
    return "none";
  }
  if (Array.isArray(value)) {
    return value.map((entry) => readableValue(entry)).join(", ") || "none";
  }
  if (value && typeof value === "object") {
    return Object.entries(value)
      .map(([key, entry]) => `${key}: ${readableValue(entry)}`)
      .join(", ");
  }
  return String(value);
}

function fieldValues(versions, read, format = readableValue) {
  const changes = differences(versions, read),
    node = element("span", { class: "field-values" });
  if (changes.length === 0) {
    node.append(format(read(versions.working)));
  } else {
    for (const change of changes) {
      node.append(
        element(
          "span",
          { class: "field-change", title: change.stage },
          element("small", { class: "change-stage" }, change.stage),
          change.before === undefined
            ? null
            : element("del", { class: "diff-remove" }, format(change.before)),
          change.after === undefined
            ? null
            : element("ins", { class: "diff-add" }, format(change.after)),
        ),
      );
    }
  }
  return node;
}

function resourceBox(target, resource) {
  const { name, repository } = target,
    view = state.views.get(repository),
    changes = differences(view, (root) => root[name]),
    current = Object.hasOwn(view.working, name),
    box = markChange(
      element("article", {
        class: "resource-box box",
        tabindex: "0",
        "data-resource": name,
        "data-repository": repository,
        "data-search": `${name} ${resource.description || ""}`.toLowerCase(),
      }),
      changes,
    ),
    title = element("h3", {}, directoryName(name, `${repository}/${name}`)),
    header = element(
      "header",
      { class: "resource-heading" },
      element(
        "div",
        { class: "box-title" },
        title,
        element(
          "span",
          { class: "kinds" },
          versionEntries(
            view,
            (root) => (root[name] ? kinds(root[name]) : []),
            (kind) => kind,
          ).map((entry) =>
            markChange(
              element(
                "span",
                { class: "kind", title: entry.value },
                icon(entry.value),
                entry.value,
              ),
              entry.changes,
            ),
          ),
        ),
      ),
      element(
        "p",
        { class: "resource-description" },
        fieldValues(
          view,
          (root) => root[name]?.description,
          (value) => value || "",
        ),
      ),
    ),
    body = element("div", { class: "box-body" });
  if (changes.length > 0) {
    title.append(
      element(
        "span",
        { class: "change-badge", title: box.title },
        { add: "+", remove: "−", modify: "+/−" }[changeKind(changes)],
      ),
    );
  }
  box.dataset.removed = String(!current);
  box.append(header, body);
  if (
    [view.working, view.index, view.head].some((root) => {
      const cli = root?.[name]?.cli;
      return (
        cli && ((cli.arguments || []).length > 0 || Object.keys(cli.commands || {}).length > 0)
      );
    })
  ) {
    body.append(argumentDetails(target));
  }
  const entries = versionEntries(
    view,
    (root) => root[name]?.tests,
    (identifier) => identifier,
  );
  if (entries.length > 0) {
    const tests = resourceSection(
      target,
      "tests",
      `Tests (${(view.working[name]?.tests || []).length})`,
    );
    markChange(
      tests.querySelector("summary"),
      differences(view, (root) => root[name]?.tests),
    );
    tests.append(
      element(
        "ul",
        { class: "test-names" },
        entries.map((entry) =>
          markChange(element("li", { title: entry.value }, testLabel(entry.value)), entry.changes),
        ),
      ),
    );
    body.append(tests);
  }
  return box;
}

function metadataFields(view, read, excluded = []) {
  const fields = element("dl", { class: "metadata" }),
    properties = new Set(
      [view.working, view.index, view.head].flatMap((root) => Object.keys(read(root) || {})),
    );
  for (const property of properties) {
    if (!excluded.includes(property)) {
      fields.append(
        markChange(
          element(
            "div",
            { class: "metadata-field", "data-property": property },
            element("dt", {}, property.replaceAll("_", " ")),
            element(
              "dd",
              {},
              fieldValues(view, (root) => read(root)?.[property]),
            ),
          ),
          differences(view, (root) => read(root)?.[property]),
        ),
      );
    }
  }
  return fields;
}

function argumentIdentity(argument) {
  return JSON.stringify(argument.names);
}

function argumentDetails(target) {
  const view = state.views.get(target.repository),
    section = resourceSection(target, "arguments", "Arguments"),
    readCommand = (root, path) => {
      let command = root?.[target.name]?.cli;
      for (const name of path) {
        command = command?.commands?.[name];
      }
      return command;
    },
    renderCommand = (path) => {
      const fields = element("div", { class: "command-fields" }),
        argumentsList = versionEntries(
          view,
          (root) => readCommand(root, path)?.arguments,
          argumentIdentity,
        );
      fields.append(
        metadataFields(view, (root) => readCommand(root, path), ["arguments", "commands"]),
      );
      for (const entry of argumentsList) {
        fields.append(
          markChange(
            element(
              "section",
              { class: "argument", "data-argument": entry.value.names.join(" / ") },
              element(
                "h4",
                { class: "argument-name" },
                element("code", {}, entry.value.names.join(" / ")),
              ),
              metadataFields(
                view,
                (root) =>
                  (readCommand(root, path)?.arguments || []).find(
                    (argument) => argumentIdentity(argument) === entry.key,
                  ),
                ["names"],
              ),
            ),
            entry.changes,
          ),
        );
      }
      const commands = versionEntries(
        view,
        (root) => Object.keys(readCommand(root, path)?.commands || {}),
        (name) => name,
      );
      for (const entry of commands) {
        const commandPath = [...path, entry.key];
        fields.append(
          markChange(
            element(
              "section",
              { class: "command-box", "data-command": commandPath.join("/") },
              element("h4", {}, entry.key),
              renderCommand(commandPath),
            ),
            differences(view, (root) => readCommand(root, commandPath)),
          ),
        );
      }
      return fields;
    };
  markChange(
    section.querySelector("summary"),
    differences(view, (root) => root[target.name]?.cli),
  );
  section.append(renderCommand([]));
  return section;
}

function drawEdges() {
  const svg = select("#edges"),
    canvas = select("#canvas"),
    ns = svg.namespaceURI,
    shape = (tag, attributes) => {
      const node = document.createElementNS(ns, tag);
      for (const [key, value] of Object.entries(attributes)) {
        node.setAttribute(key, String(value));
      }
      return node;
    },
    marker = shape("marker", {
      id: "arrow",
      viewBox: "0 0 10 10",
      refX: "9",
      refY: "5",
      markerWidth: "7",
      markerHeight: "7",
      orient: "auto-start-reverse",
    }),
    defs = shape("defs", {});
  marker.append(shape("path", { d: "M 0 0 L 10 5 L 0 10 z" }));
  defs.append(marker);
  svg.replaceChildren(defs);
  svg.setAttribute("width", canvas.offsetWidth);
  svg.setAttribute("height", canvas.offsetHeight);
  const bounds = canvas.getBoundingClientRect(),
    nodes = new Map(
      [...select("#boxes").querySelectorAll(".resource-box")]
        .filter((box) => box.getClientRects().length > 0)
        .map((box) => [
          targetKey({ repository: box.dataset.repository, name: box.dataset.resource }),
          box,
        ]),
    );
  for (const box of nodes.values()) {
    const { repository, resource: name } = box.dataset,
      grouped = new Map();
    const entries = versionEntries(
      state.views.get(repository),
      (root) => root[name]?.dependencies,
      (dependency) => JSON.stringify([dependency.resource, dependency.kind]),
    );
    for (const entry of entries.filter(
      (item) => !select("#diff-only").checked || item.changes.length > 0,
    )) {
      const dependency = entry.value;
      const key = targetKey({ repository, name: dependency.resource });
      if (nodes.has(key)) {
        grouped.set(key, [...(grouped.get(key) || []), entry]);
      }
    }
    for (const [source, dependencies] of grouped) {
      const changes = dependencies.flatMap((entry) => entry.changes),
        kind = dependencies.every((entry) => entry.changes.length > 0) ? changeKind(changes) : "";
      const from = nodes.get(source).getBoundingClientRect(),
        to = box.getBoundingClientRect(),
        x1 = (from.right - bounds.left) / state.zoom,
        y1 = (from.top - bounds.top + 36 * state.zoom) / state.zoom,
        x2 = (to.left - bounds.left) / state.zoom,
        y2 = (to.top - bounds.top + 36 * state.zoom) / state.zoom,
        bend = Math.max(40, Math.abs(x2 - x1) / 2),
        path = shape("path", {
          d: `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`,
          class: `dependency-edge${kind ? ` change-${kind}` : ""}`,
          "marker-end": "url(#arrow)",
        }),
        title = shape("title", {}),
        label = shape("text", {
          x: (x1 + x2) / 2,
          y: (y1 + y2) / 2 - 8,
          "text-anchor": "middle",
          class: "edge-label",
        });
      title.textContent = `${repository}: ${nodes.get(source).dataset.resource} → ${name}: ${dependencies.map((entry) => entry.value.kind).join(", ")}${changes.length > 0 ? ` · ${[...new Set(changes.map((change) => change.stage))].join(" / ")}` : ""}`;
      for (const [index, entry] of dependencies.entries()) {
        const span = shape("tspan", {
          class: entry.changes.length > 0 ? `change-${changeKind(entry.changes)}` : "",
        });
        span.textContent = `${index ? ", " : ""}${{ add: "+ ", remove: "− " }[changeKind(entry.changes)] || ""}${entry.value.kind}`;
        label.append(span);
      }
      path.append(title);
      svg.append(path, label);
    }
  }
}

function zoom(value, anchor) {
  const viewport = select("#viewport"),
    canvas = select("#canvas"),
    bounds = viewport.getBoundingClientRect(),
    point = anchor || {
      x: bounds.left + bounds.width / 2,
      y: bounds.top + bounds.height / 2,
    },
    before = canvas.getBoundingClientRect(),
    local = {
      x: (point.x - before.left) / state.zoom,
      y: (point.y - before.top) / state.zoom,
    };
  state.zoom = Math.max(0.02, Math.min(1.6, value));
  canvas.style.zoom = state.zoom;
  const after = canvas.getBoundingClientRect();
  viewport.scrollBy(
    after.left + local.x * state.zoom - point.x,
    after.top + local.y * state.zoom - point.y,
  );
  requestAnimationFrame(drawEdges);
}
function fit() {
  zoom(1);
  requestAnimationFrame(() => {
    const viewport = select("#viewport"),
      canvas = select("#canvas");
    zoom(
      Math.min(
        1,
        viewport.clientWidth / canvas.offsetWidth,
        viewport.clientHeight / canvas.offsetHeight,
      ),
    );
    viewport.scrollTo(0, 0);
  });
}
function renderDiffError(repository) {
  const box = repositoryElement(repository),
    interfaces = state.diffs.get(repository);
  box?.querySelector(".diff-error")?.remove();
  for (const stage of ["working", "staged"]) {
    if (interfaces?.[stage]?.error) {
      box
        ?.querySelector(".repository-heading")
        .append(
          element(
            "p",
            { class: "diff-error", role: "status" },
            `afairesi diff${stage === "staged" ? " --cached" : ""}: ${interfaces[stage].error}`,
          ),
        );
    }
  }
}

function refreshDiffView(repository) {
  const item = state.repositories.find((entry) => entry.id === repository),
    report = state.reports.get(repository);
  if (!item || !report) {
    return;
  }
  const view = repositoryVersions(state.diffs.get(repository), report.resources),
    active = document.activeElement,
    target = active.closest(".resource-box"),
    focused = target?.dataset.repository === repository ? target.dataset.resource : null,
    section = active.closest(".resource-section")?.dataset.section;
  report.resources = view.working;
  renderRepository(item);
  if (focused) {
    const box = [...select("#boxes").querySelectorAll(".resource-box")].find(
      (node) => node.dataset.repository === repository && node.dataset.resource === focused,
    );
    const disclosure = [...(box?.querySelectorAll(".resource-section") || [])].find(
      (node) => node.dataset.section === section,
    );
    (disclosure?.querySelector("summary") || box)?.focus({ preventScroll: true });
  }
  renderHeading();
}

async function loadDiff(repository, epoch = state.epoch) {
  if (
    state.diffLoading.has(repository) ||
    !state.repositories.some((item) => item.id === repository && item.initialized)
  ) {
    return;
  }
  state.diffLoading.add(repository);
  try {
    const result = await api(query("/api/diff", repository));
    if (epoch === state.epoch && !equal(state.diffs.get(repository), result)) {
      state.diffs.set(repository, result);
      refreshDiffView(repository);
    }
  } catch (error) {
    if (epoch === state.epoch) {
      state.diffs.set(repository, { working: { error: error.message } });
      refreshDiffView(repository);
    }
  } finally {
    state.diffLoading.delete(repository);
    filterBoxes();
  }
}

for (const [selector, name, label] of [
  ["#refresh", "refresh", "Refresh workspace"],
  ["#fit", "fit", "Fit all repositories"],
  ["#zoom-in", "plus", "Zoom in"],
  ["#zoom-out", "minus", "Zoom out"],
]) {
  const button = select(selector);
  button.replaceChildren(icon(name));
  button.title = label;
  button.setAttribute("aria-label", label);
  button.classList.add("icon-button");
}
select("#home").addEventListener("click", home);
select("#refresh").addEventListener("click", loadWorkspace);
select("#search").addEventListener("input", filterBoxes);
select("#diff-only").addEventListener("change", filterBoxes);
select("#fit").addEventListener("click", fit);
select("#zoom-in").addEventListener("click", () => zoom(state.zoom + 0.1));
select("#zoom-out").addEventListener("click", () => zoom(state.zoom - 0.1));
select("#viewport").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 800 : 1,
      delta = Math.max(-300, Math.min(300, (event.deltaY || event.deltaX) * unit));
    zoom(state.zoom * Math.exp(-delta * 0.002), { x: event.clientX, y: event.clientY });
  },
  { passive: false },
);
let pan = null,
  suppressCanvasClick = false;
select("#viewport").addEventListener("pointerdown", (event) => {
  suppressCanvasClick = false;
  if (event.button === 0) {
    const viewport = select("#viewport");
    pan = {
      x: event.clientX,
      y: event.clientY,
      left: viewport.scrollLeft,
      top: viewport.scrollTop,
      dragging: false,
    };
  }
});
select("#viewport").addEventListener("pointermove", (event) => {
  if (pan) {
    const viewport = select("#viewport"),
      dx = pan.x - event.clientX,
      dy = pan.y - event.clientY;
    if (!pan.dragging && Math.hypot(dx, dy) > 4) {
      pan.dragging = true;
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("panning");
      globalThis.getSelection()?.removeAllRanges();
    }
    if (pan.dragging) {
      event.preventDefault();
      viewport.scrollTo(pan.left + dx, pan.top + dy);
    }
  }
});
function endPan(event) {
  if (!pan) {
    return;
  }
  suppressCanvasClick = event.type === "pointerup" && pan.dragging;
  pan = null;
  select("#viewport").classList.remove("panning");
}
for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
  globalThis.addEventListener(name, endPan);
}
select("#viewport").addEventListener("dragstart", (event) => event.preventDefault());
select("#viewport").addEventListener(
  "click",
  (event) => {
    if (suppressCanvasClick) {
      event.preventDefault();
      event.stopPropagation();
      suppressCanvasClick = false;
    }
  },
  true,
);
select("#viewport").addEventListener("click", (event) => {
  if (state.directory && !event.target.closest(".directory-box, .repository-box, .resource-box")) {
    navigate(state.directory.split("/").slice(0, -1).join("/"));
  }
});
document.addEventListener("keydown", (event) => {
  if (
    !event.defaultPrevented &&
    !event.isComposing &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    event.key === "/" &&
    !document.activeElement.matches("input, textarea, select") &&
    !document.activeElement.isContentEditable
  ) {
    event.preventDefault();
    select("#search").focus();
    select("#search").select();
  }
});
window.addEventListener("resize", drawEdges);
await loadWorkspace();
setInterval(() => {
  if (!state.refreshing) {
    inBatches(state.repositories, (repository) => loadDiff(repository.id));
  }
}, 10_000);
