/* oxlint-disable id-length -- SVG coordinates use x and y attributes. */
/* oxlint-disable func-style, no-use-before-define, one-var, sort-keys, sort-vars -- Keep named DOM renderers and state grouped by responsibility. */
/* oxlint-disable no-ternary, no-nested-ternary, no-undefined, unicorn/no-null, unicorn/no-nested-ternary -- Render optional JSON fields and conditional UI labels directly. */
/* oxlint-disable max-lines, max-lines-per-function, max-statements, complexity, no-magic-numbers, unicorn/max-nested-calls -- This buildless interface constructs its DOM in one module. */
/* oxlint-disable oxc/no-async-await, oxc/no-optional-chaining, oxc/no-rest-spread-properties -- Target modern browsers with native modules and fetch. */

import { argumentMode, argumentRequired, commandArguments } from "/arguments.js";
import {
  changeKind,
  differences,
  equal,
  repositoryVersions,
  versionEntries,
} from "/differences.js";
const select = (selector) => document.querySelector(selector),
  state = {
    actions: new Map(),
    activity: new Map(),
    diffs: new Map(),
    views: new Map(),
    diffLoading: new Set(),
    epoch: 0,
    home: "",
    reports: new Map(),
    watching: new Set(),
    agent: {},
    chat: {},
    connecting: false,
    forms: new Map(),
    jobs: [],
    messageSignature: "",
    mentionIndex: 0,
    mentions: [],
    mentionRange: null,
    refreshing: false,
    report: null,
    repositories: [],
    repository: null,
    requestSignature: "",
    resource: null,
    submitting: false,
    zoom: 1,
  };
const observer = new ResizeObserver(() => requestAnimationFrame(drawEdges));
const iconPaths = {
  build: ["m14 3 7 7-3 3-3-3-8 10-3-3 10-8-3-3Z"],
  run: ["m8 4 12 8-12 8Z"],
  test: ["M9 3h6m-5 0v7L4 19q-1 2 2 2h12q3 0 2-2l-6-9V3M8 15h8"],
  refresh: ["M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"],
  fit: ["M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"],
  plus: ["M5 12h14M12 5v14"],
  minus: ["M5 12h14"],
  diff: ["M5 7h8M9 3v8m2 6h8"],
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
  chat: ["M3 4h18v13H9l-6 4Z"],
  link: [
    "m10 14 4-4M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 10a4 4 0 0 0 6 0l4-4a4 4 0 0 0-6-6l-1 1",
  ],
  stop: ["M5 5h14v14H5Z"],
  send: ["M12 20V4m-7 7 7-7 7 7"],
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
    } else if (key === "disabled" || key === "hidden") {
      node[key] = value;
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

async function api(path, body) {
  const response = await fetch(
      path,
      body === undefined
        ? {}
        : {
            body: JSON.stringify(body),
            headers: { "Content-Type": "application/json" },
            method: "POST",
          },
    ),
    result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || `Request failed (${response.status})`);
  }
  return result;
}

function query(path, extra = {}, repository = state.repository) {
  return `${path}?${new URLSearchParams({ repository: repository || "", ...extra })}`;
}

let toastTimer = 0;
function toast(message) {
  select("#toast").textContent = message;
  select("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    select("#toast").hidden = true;
  }, 6500);
}

function notice(message = "") {
  select("#repository-notice").textContent = message;
  select("#repository-notice").hidden = !message;
}

function empty(message) {
  return element(
    "div",
    { class: "empty-state" },
    element("span", { class: "empty-symbol" }, "−"),
    element("p", {}, message),
  );
}

function renderHeading() {
  const count = [...state.reports.values()].reduce(
    (total, report) => total + Object.keys(report.resources || {}).length,
    0,
  );
  document.title = "afairesi — home";
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
    state.home = workspace.home;
    state.actions.clear();
    for (const identifier of state.reports.keys()) {
      if (!state.repositories.some((repository) => repository.id === identifier)) {
        state.reports.delete(identifier);
        state.activity.delete(identifier);
        state.diffs.delete(identifier);
      }
    }
    if (!state.repositories.some((repository) => repository.id === state.repository)) {
      selectContext(null);
    }
    renderResources();
    renderHeading();
    await inBatches(state.repositories, async (repository) => {
      await loadRepository(repository, epoch);
    });
    if (!state.repository) {
      let requested = "";
      try {
        requested = decodeURIComponent(location.hash.slice(1));
      } catch {
        requested = "";
      }
      const preferred = state.repositories.find(
        (repository) => repository.id === requested && repository.initialized,
      );
      if (preferred) {
        selectContext(preferred.id);
      }
    }
    await Promise.all([
      inBatches(state.repositories, (repository) => loadActions(repository.id, epoch)),
      inBatches(state.repositories, (repository) => loadDiff(repository.id, epoch)),
    ]);
    await pollActivity();
  } catch (error) {
    notice(error.message);
  } finally {
    state.refreshing = false;
    select("#refresh").disabled = false;
    renderHeading();
  }
}

async function loadRepository(repository, epoch) {
  if (!repository.initialized) {
    return;
  }
  try {
    const report = await api(query("/api/repository", {}, repository.id));
    if (epoch !== state.epoch) {
      return;
    }
    state.reports.set(repository.id, report);
    renderRepository(repository);
    if (state.repository === repository.id) {
      state.report = report;
      renderContext();
    }
    renderHeading();
    await refreshActivity(repository.id);
  } catch (error) {
    if (epoch === state.epoch) {
      state.reports.set(repository.id, { resources: {}, inspection_error: error.message });
      renderRepository(repository);
    }
  }
}

function selectContext(repository, resource = null) {
  if (state.repository !== repository) {
    state.repository = repository;
    state.chat = state.activity.get(repository)?.chat || {};
    state.messageSignature = "";
    state.requestSignature = "";
  }
  state.report = state.reports.get(repository) || null;
  state.resource = resource;
  history.replaceState(
    null,
    "",
    repository ? `#${encodeURIComponent(repository)}` : location.pathname,
  );
  renderHeading();
  renderContext();
  renderChat();
}

function home() {
  select("#search").value = "";
  renderResources();
  zoom(1);
  select("#viewport").scrollTo(0, 0);
}

function kinds(resource) {
  return Array.isArray(resource.type) ? resource.type : [resource.type];
}
function targetKey(target) {
  return JSON.stringify([target.repository, target.name]);
}
function formState(target) {
  const key = targetKey(target);
  if (!state.forms.has(key)) {
    state.forms.set(key, { commands: {} });
  }
  return state.forms.get(key);
}

function repositoryElement(identifier) {
  return [...select("#boxes").querySelectorAll(".repository-box")].find(
    (box) => box.dataset.repository === identifier,
  );
}

function renderResources() {
  observer.disconnect();
  const container = select("#boxes"),
    folders = new Map([["", container]]);
  container.replaceChildren();
  container.className = "workspace-boxes";
  for (const repository of state.repositories) {
    const parts = repository.path.split("/"),
      directories = parts.slice(0, -1);
    let path = "",
      parent = container;
    for (const directory of directories) {
      path = path ? `${path}/${directory}` : directory;
      if (!folders.has(path)) {
        const children = element("div", { class: "directory-children" }),
          box = element(
            "section",
            { class: "directory-box", "data-path": path, "aria-label": `${path}/` },
            element("h2", { class: "directory-heading" }, icon("folder"), `${directory}/`),
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
  }
  if (state.repositories.length === 0) {
    container.append(empty("No registered home submodules."));
  }
  observer.observe(container);
  filterBoxes();
  renderJobs();
}

function renderRepository(repository, target = repositoryElement(repository.id)) {
  if (!target) {
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
      element(
        "div",
        {},
        element("h2", {}, icon("repository"), `${repository.name}/`),
        element(
          "p",
          { class: "path" },
          report?.branch
            ? `${report.branch} · ${report.dirty ? "uncommitted changes" : "clean"} · ${Object.keys(resources).length} resources`
            : repository.path,
        ),
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
  renderActionNotice(repository.id);
  renderDiffError(repository.id);
  filterBoxes();
  renderJobs();
}

function filterBoxes() {
  const filter = select("#search").value.toLowerCase();
  for (const repository of select("#boxes").querySelectorAll(".repository-box")) {
    const matches = repository.dataset.repository.toLowerCase().includes(filter),
      resources = [...repository.querySelectorAll(".resource-box")];
    for (const resource of resources) {
      resource.hidden = !matches && !resource.dataset.search.includes(filter);
    }
    for (const column of repository.querySelectorAll(".graph-column")) {
      column.hidden = [...column.children].every((box) => box.hidden);
    }
    repository.hidden = !matches && !resources.some((box) => !box.hidden);
  }
  for (const directory of [...select("#boxes").querySelectorAll(".directory-box")].toReversed()) {
    directory.hidden = [...directory.querySelectorAll(".repository-box")].every(
      (box) => box.hidden,
    );
  }
  requestAnimationFrame(drawEdges);
}

function resourceSection(target, kind, title) {
  const saved = formState(target),
    section = element(
      "details",
      { class: `resource-section ${kind}-section` },
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
    title = element("h3", {}, name),
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
        {},
        fieldValues(
          view,
          (root) => root[name]?.description,
          (value) => value || "",
        ),
      ),
    ),
    body = element("div", { class: "box-body" }),
    actions = element("div", { class: "action-bar" }),
    jobs = element("div", { class: "box-jobs" });
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
  body.append(actions);
  if (
    [view.working, view.index, view.head].some((root) => {
      const cli = root?.[name]?.cli;
      return (
        cli && ((cli.arguments || []).length > 0 || Object.keys(cli.commands || {}).length > 0)
      );
    })
  ) {
    body.append(argumentForm(target));
  }
  body.append(jobs);
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
    tests.classList.add("test-identifiers");
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
  renderActions(box);
  return box;
}

async function loadActions(repository, epoch) {
  if (Object.keys(state.reports.get(repository)?.resources || {}).length === 0) {
    return;
  }
  let result = {};
  try {
    result = { values: await api(query("/api/actions", {}, repository)) };
  } catch (error) {
    result = {
      error: error.message.includes("timed out")
        ? "Action checks timed out. Refresh to retry."
        : error.message,
    };
  }
  if (epoch !== state.epoch) {
    return;
  }
  state.actions.set(repository, result);
  renderActionNotice(repository);
  for (const box of repositoryElement(repository)?.querySelectorAll(".resource-box") || []) {
    renderActions(box);
  }
}

function renderActionNotice(repository) {
  const box = repositoryElement(repository),
    error = state.actions.get(repository)?.error;
  box?.querySelector(".action-error")?.remove();
  if (box && error) {
    box.querySelector(".repository-heading").append(element("p", { class: "action-error" }, error));
  }
}

function renderActions(box) {
  const target = { repository: box.dataset.repository, name: box.dataset.resource },
    result = state.actions.get(target.repository),
    actions = result?.values?.[target.name],
    container = box.querySelector(".action-bar");
  container.replaceChildren(
    ...["build", "run", "test"].map((action) =>
      element(
        "button",
        {
          class: "button icon-button",
          "aria-label": `${action[0].toUpperCase() + action.slice(1)} ${target.name}`,
          disabled: box.dataset.removed === "true" || !actions?.[action],
          title: result
            ? result.error ||
              (actions?.[action] ? `${action} ${target.name}` : "Unavailable on this system")
            : "Checking available actions…",
          onclick: (event) => startJob(target, action, event.currentTarget),
        },
        icon(action),
      ),
    ),
  );
}

function argumentIdentity(argument) {
  return JSON.stringify(argument.names);
}

function argumentForm(target) {
  const data = formState(target),
    view = state.views.get(target.repository),
    form = resourceSection(target, "arguments", "Arguments"),
    readCommand = (root, path) => {
      let command = root[target.name]?.cli;
      for (const name of path) {
        command = command?.commands?.[name];
      }
      return command;
    },
    update = () => {
      for (const box of form.querySelectorAll(".command-box")) {
        box.classList.toggle("active-command", box.querySelector("legend input").checked);
      }
    },
    renderCommand = (path, activate) => {
      const key = path.join("/"),
        command = readCommand(view.working, path) || {},
        saved = data.commands[key] || { values: {}, selected: "", arguments: [] },
        fields = element("div", { class: "command-fields" }),
        entries = versionEntries(
          view,
          (root) => readCommand(root, path)?.arguments,
          argumentIdentity,
        ),
        previous = new Map(
          (saved.arguments || []).map((argument, index) => [
            argumentIdentity(argument),
            saved.values[index],
          ]),
        );
      saved.values = Object.fromEntries(
        (command.arguments || []).map((argument, index) => [
          index,
          previous.get(argumentIdentity(argument)),
        ]),
      );
      saved.arguments = command.arguments || [];
      if (!Object.hasOwn(command.commands || {}, saved.selected)) {
        saved.selected = "";
      }
      data.commands[key] = saved;
      for (const entry of entries) {
        const index = (command.arguments || []).findIndex(
            (argument) => argumentIdentity(argument) === entry.key,
          ),
          row = argumentControl(entry.value, index, entry.current ? saved.values : {}, () => {
            activate?.();
            update();
          });
        markChange(row, entry.changes);
        if (!entry.current) {
          for (const control of row.querySelectorAll("input, select, textarea")) {
            control.disabled = true;
          }
        } else if (
          entry.changes.some((change) => change.before !== undefined && change.after !== undefined)
        ) {
          const readArgument = (root) =>
              (readCommand(root, path)?.arguments || []).find(
                (argument) => argumentIdentity(argument) === entry.key,
              ),
            properties = new Set(
              entry.changes.flatMap((change) =>
                [change.before, change.after].flatMap((value) => Object.keys(value || {})),
              ),
            );
          for (const property of properties) {
            if (differences(view, (root) => readArgument(root)?.[property]).length > 0) {
              row.append(
                element(
                  "div",
                  { class: "argument-change" },
                  element(
                    "span",
                    { class: "argument-property" },
                    `${property.replaceAll("_", " ")}: `,
                  ),
                  fieldValues(view, (root) => readArgument(root)?.[property]),
                ),
              );
            }
          }
        }
        fields.append(row);
      }
      const commands = versionEntries(
        view,
        (root) => Object.keys(readCommand(root, path)?.commands || {}),
        (name) => name,
      );
      if (commands.length > 0) {
        const group = `${targetKey(target)}:${key}`,
          rootChoice = element("input", { type: "radio", name: group, value: "" });
        rootChoice.checked = saved.selected === "";
        rootChoice.addEventListener("change", () => {
          saved.selected = "";
          activate?.();
          update();
        });
        fields.append(
          element("label", { class: "argument-label" }, rootChoice, "Run without a subcommand"),
        );
        for (const entry of commands) {
          const choice = element("input", { type: "radio", name: group, value: entry.key }),
            choose = () => {
              saved.selected = entry.key;
              choice.checked = true;
              activate?.();
            },
            box = markChange(
              element(
                "fieldset",
                {
                  class: "command-box",
                  "data-command": [...path, entry.key].join("/"),
                  disabled: !entry.current,
                },
                element("legend", {}, element("label", {}, choice, ` ${entry.key}`)),
              ),
              differences(view, (root) => readCommand(root, [...path, entry.key])),
            );
          choice.disabled = !entry.current;
          choice.checked = saved.selected === entry.key;
          choice.addEventListener("change", () => {
            choose();
            update();
          });
          box.append(renderCommand([...path, entry.key], choose));
          fields.append(box);
        }
      }
      return fields;
    };
  form.classList.add("argument-form");
  markChange(
    form.querySelector("summary"),
    differences(view, (root) => root[target.name]?.cli),
  );
  form.append(renderCommand([], null));
  update();
  return form;
}

// oxlint-disable-next-line max-params -- Metadata, saved state, and change callback belong to one control.
function argumentControl(argument, index, values, update) {
  const mode = argumentMode(argument),
    required = argumentRequired(argument),
    label = argument.names.join(" / "),
    saved = values[index] || {
      enabled: required,
      text:
        mode === "count"
          ? "1"
          : mode === "boolean"
            ? "true"
            : mode === "choice"
              ? String(argument.choices[0])
              : "",
    };
  values[index] = saved;
  if (mode === "unsupported") {
    return element("p", { class: "muted" }, `${label}: custom action; use the CLI.`);
  }
  const row = element("div", { class: "argument" }),
    enabled = element("input", { type: "checkbox", "aria-label": `Include ${label}` }),
    heading = element(
      "label",
      { class: "argument-label" },
      enabled,
      element("code", {}, label),
      required ? element("span", { class: "required" }, "required") : null,
    );
  enabled.checked = saved.enabled;
  enabled.disabled = required;
  row.append(heading);
  let input = null;
  if (mode !== "flag") {
    if (mode === "choice" || mode === "boolean") {
      const choices = mode === "boolean" ? ["true", "false"] : argument.choices;
      input = element(
        "select",
        { "aria-label": label },
        choices.map((choice) => element("option", { value: String(choice) }, String(choice))),
      );
    } else {
      input = element(mode === "multiple" ? "textarea" : "input", {
        "aria-label": label,
        ...(mode === "multiple"
          ? { rows: "2" }
          : {
              type: ["number", "count"].includes(mode) ? "number" : "text",
              step: argument.type === "int" || mode === "count" ? "1" : "any",
            }),
      });
      if (mode === "multiple") {
        input.placeholder = "One value per line";
      } else if (Object.hasOwn(argument, "default")) {
        input.placeholder =
          typeof argument.default === "object"
            ? "Use package default"
            : `Default: ${String(argument.default)}`;
      }
    }
    input.value = saved.text;
    input.addEventListener("input", () => {
      saved.text = input.value;
      saved.enabled = true;
      enabled.checked = true;
      update();
    });
    row.append(input);
  }
  enabled.addEventListener("change", () => {
    saved.enabled = enabled.checked;
    update();
  });
  if (argument.nargs === "?" && argument.names[0].startsWith("-")) {
    const constant = element("input", { type: "checkbox" });
    constant.checked = saved.constant || false;
    constant.addEventListener("change", () => {
      saved.constant = constant.checked;
      saved.enabled = true;
      enabled.checked = true;
      update();
    });
    row.append(element("label", { class: "muted" }, constant, " Use without a value"));
  }
  if (argument.help && typeof argument.help === "string") {
    row.append(element("p", { class: "argument-help" }, argument.help));
  }
  return row;
}

function collectArguments(cli, data, path = "") {
  const saved = data.commands[path] || { values: {}, selected: "" },
    tokens = commandArguments(cli, saved.values);
  if (saved.selected && cli.commands?.[saved.selected]) {
    tokens.push(
      saved.selected,
      ...collectArguments(
        cli.commands[saved.selected],
        data,
        [path, saved.selected].filter(Boolean).join("/"),
      ),
    );
  }
  return tokens;
}

async function startJob(target, action, button) {
  const { repository, name } = target;
  button.disabled = true;
  try {
    const resource = state.reports.get(repository).resources[name],
      args = action === "run" ? collectArguments(resource.cli || {}, formState(target)) : [];
    await api("/api/jobs", { action, arguments: args, repository, resource: name });
    state.watching.add(repository);
    await refreshActivity(repository);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function stripAnsi(value) {
  // oxlint-disable-next-line no-control-regex -- Remove terminal ANSI sequences from process output.
  return value.replaceAll(/\u001B\[[0-?]*[ -/]*[@-~]/gu, "");
}

function renderJobs() {
  for (const box of select("#boxes").querySelectorAll(".resource-box")) {
    const container = box.querySelector(".box-jobs"),
      jobs = state.jobs.filter(
        (job) => job.repository === box.dataset.repository && job.resource === box.dataset.resource,
      ),
      signature = JSON.stringify(jobs);
    if (container.dataset.signature !== signature) {
      const previous = new Map(
        [...container.querySelectorAll(".job")].map((node) => [
          node.dataset.job,
          { scroll: node.querySelector("pre").scrollTop },
        ]),
      );
      container.dataset.signature = signature;
      container.replaceChildren(
        ...jobs.map((job) => {
          const detail = element("section", { class: "job", "data-job": job.id }),
            output = element(
              "pre",
              { class: "log-output", tabindex: "0" },
              stripAnsi(job.output) || "Waiting for output…",
            ),
            actions = element(
              "div",
              { class: "action-bar" },
              element(
                "button",
                {
                  class: "button small",
                  onclick: () => askAboutJob(job),
                },
                icon("chat"),
                "Ask Codex",
              ),
            );
          if (job.status === "running") {
            actions.append(
              element(
                "button",
                {
                  class: "button small",
                  onclick: async () => {
                    try {
                      await api("/api/jobs/cancel", { repository: job.repository, id: job.id });
                      await pollActivity();
                    } catch (error) {
                      toast(error.message);
                    }
                  },
                },
                "Stop",
              ),
            );
          }
          detail.append(
            element(
              "h4",
              { class: job.status },
              `${job.action} · ${job.status}${job.exit_code === null ? "" : ` · exit ${job.exit_code}`}`,
            ),
            element("code", { class: "job-command" }, job.command),
            output,
            actions,
          );
          output.scrollTop = previous.get(job.id)?.scroll || 0;
          return detail;
        }),
      );
    }
  }
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
    for (const entry of entries) {
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
    changed = !equal(report.resources, view.working),
    active = document.activeElement,
    target = active.closest(".resource-box"),
    focus =
      target?.dataset.repository === repository
        ? {
            resource: target.dataset.resource,
            label: active.getAttribute("aria-label"),
            start: active.selectionStart,
            end: active.selectionEnd,
          }
        : null;
  report.resources = view.working;
  renderRepository(item);
  if (focus?.label) {
    const box = [...repositoryElement(repository).querySelectorAll(".resource-box")].find(
        (node) => node.dataset.resource === focus.resource,
      ),
      control = [...(box?.querySelectorAll("[aria-label]") || [])].find(
        (node) => node.getAttribute("aria-label") === focus.label,
      );
    if (control && !control.disabled) {
      control.focus({ preventScroll: true });
      if (focus.start !== null && focus.start !== undefined) {
        control.setSelectionRange(focus.start, focus.end);
      }
    }
  }
  if (changed) {
    loadActions(repository, state.epoch);
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
    const result = await api(query("/api/diff", {}, repository));
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
  }
}

function renderContext() {
  const repository = state.repositories.find((item) => item.id === state.repository);
  select("#chat-context").textContent = repository ? `@${repository.path}` : "";
  select("#chat-context").title = repository?.path || "";
  renderAgent();
}

function renderAgent() {
  const { connected, login } = state.agent;
  select("#agent-status").textContent = connected
    ? `Connected${state.agent.plan ? ` · ${state.agent.plan}` : ""}`
    : state.agent.error ||
      (state.agent.available === false
        ? "Codex executable is unavailable"
        : "Your coding companion");
  select("#agent-status").hidden = Boolean(
    connected || (!state.agent.error && state.agent.available !== false && !login),
  );
  if (!connected && login) {
    select("#agent-status").replaceChildren(
      element(
        "a",
        {
          href: login.verificationUrl || login.authUrl,
          target: "_blank",
          rel: "noopener noreferrer",
        },
        "Sign in to Codex ↗",
      ),
    );
    if (login.userCode) {
      select("#agent-status").append(
        element(
          "span",
          {},
          " Enter code ",
          element("code", { class: "login-code", "aria-label": "Sign-in code" }, login.userCode),
        ),
      );
    }
  }
  select("#connect").replaceChildren(
    icon("link"),
    state.connecting
      ? "Connecting…"
      : connected
        ? "Codex ✓"
        : login
          ? "Waiting for sign-in…"
          : "Connect Codex",
  );
  select("#connect").title = select("#agent-status").textContent;
  select("#connect").disabled = Boolean(
    state.connecting || connected || login || state.agent.available === false,
  );
  select("#stop-chat").hidden = !state.chat.busy;
  select("#message").disabled = !connected || state.submitting;
  select("#send").disabled = !connected || state.submitting || !select("#message").value.trim();
}

function hideMentions() {
  state.mentions = [];
  state.mentionRange = null;
  select("#mentions").hidden = true;
  select("#message").setAttribute("aria-expanded", "false");
  select("#message").removeAttribute("aria-activedescendant");
}

function chooseMention(repository) {
  const range = state.mentionRange;
  if (!range) {
    return;
  }
  const input = select("#message"),
    path = /\s/u.test(repository.id) ? `"${repository.id}"` : repository.id;
  input.setRangeText(`@${path} `, range.start, range.end, "end");
  hideMentions();
  selectContext(repository.id);
  input.focus();
}

function renderMentions() {
  const target = select("#mentions");
  target.replaceChildren(
    ...state.mentions.map((repository, index) =>
      element(
        "button",
        {
          type: "button",
          role: "option",
          tabindex: "-1",
          id: `mention-${index}`,
          "aria-selected": String(index === state.mentionIndex),
          onpointerdown: (event) => event.preventDefault(),
          onclick: () => chooseMention(repository),
        },
        icon("repository"),
        repository.path,
      ),
    ),
  );
  target.hidden = state.mentions.length === 0;
  select("#message").setAttribute("aria-expanded", String(!target.hidden));
  if (target.hidden) {
    select("#message").removeAttribute("aria-activedescendant");
  } else {
    select("#message").setAttribute("aria-activedescendant", `mention-${state.mentionIndex}`);
    target.children[state.mentionIndex].scrollIntoView({ block: "nearest" });
  }
}

function updateMentions() {
  const input = select("#message"),
    prefix = input.value.slice(0, input.selectionStart),
    match = /(?:^|\s)@(?<query>[^@\s]*)$/u.exec(prefix);
  if (!match) {
    hideMentions();
    return;
  }
  const fragment = match.groups.query.toLowerCase();
  let end = input.selectionEnd;
  while (end < input.value.length && !/\s/u.test(input.value[end])) {
    end += 1;
  }
  state.mentionRange = { start: prefix.length - match.groups.query.length - 1, end };
  state.mentions = state.repositories.filter(
    (repository) => repository.initialized && repository.path.toLowerCase().includes(fragment),
  );
  state.mentionIndex = 0;
  renderMentions();
}

function messageRepository(message) {
  const selected = new Set();
  for (const match of message.matchAll(/(?:^|\s)@(?:"(?<quoted>[^"]+)"|(?<bare>[^\s]+))/gu)) {
    const name = (match.groups.quoted || match.groups.bare).replace(/[),;:!?]+$/u, ""),
      matches = state.repositories.filter(
        (repository) =>
          repository.initialized && (repository.id === name || repository.name === name),
      );
    if (matches.length > 1) {
      throw new Error(
        `@${name} matches multiple repositories. Choose its full path from @ suggestions.`,
      );
    }
    if (matches.length === 1) {
      selected.add(matches[0].id);
    }
  }
  if (selected.size > 1) {
    throw new Error("Mention one repository to choose the conversation for this message.");
  }
  return [...selected][0] || state.repository;
}

async function sendMessage(event) {
  event.preventDefault();
  const input = select("#message"),
    message = input.value.trim();
  if (!message) {
    return;
  }
  try {
    const repository = messageRepository(message);
    if (!repository) {
      toast("Type @ to choose a repository for this conversation.");
      input.focus();
      return;
    }
    if (await submitChat({ repository, message, resource: "" })) {
      input.value = "";
      hideMentions();
      renderAgent();
      input.focus();
    }
  } catch (error) {
    toast(error.message);
  }
}

function richText(text) {
  const node = element("div", { class: "message-text" });
  for (const block of text.split(/(?<code>```[\s\S]*?```)/gu)) {
    if (block.startsWith("```")) {
      node.append(
        element("pre", { class: "diff" }, block.replace(/^```[^\n]*\n?/u, "").replace(/```$/u, "")),
      );
    } else {
      for (const part of block.split(/(?<inline>`[^`\n]+`|\*\*[^*]+\*\*)/gu)) {
        if (part.startsWith("`")) {
          node.append(element("code", {}, part.slice(1, -1)));
        } else if (part.startsWith("**")) {
          node.append(element("strong", {}, part.slice(2, -2)));
        } else {
          node.append(document.createTextNode(part));
        }
      }
    }
  }
  return node;
}

function renderChat() {
  const messages = state.chat.messages || [],
    signature = JSON.stringify([messages, state.chat.busy]);
  if (signature !== state.messageSignature) {
    state.messageSignature = signature;
    const container = select("#messages"),
      follow = container.scrollTop + container.clientHeight >= container.scrollHeight - 60,
      scroll = container.scrollTop;
    container.replaceChildren();
    for (const message of messages) {
      if (message.role === "tool") {
        container.append(
          element(
            "details",
            { class: "message tool" },
            element("summary", {}, message.text),
            element("pre", {}, stripAnsi(message.output || "")),
          ),
        );
      } else {
        container.append(
          element(
            "article",
            { class: `message ${message.role}` },
            element(
              "span",
              { class: "message-role" },
              { user: "You", assistant: "Codex", error: "Something went wrong" }[message.role] ||
                message.role,
            ),
            richText(message.text),
          ),
        );
      }
    }
    if (state.chat.busy) {
      container.append(element("p", { class: "thinking" }, "◌ Codex is working…"));
    }
    container.scrollTop = follow ? container.scrollHeight : scroll;
  }
  renderRequests();
  renderAgent();
}

function renderRequests() {
  const requests = state.chat.requests || [],
    signature = JSON.stringify(requests);
  if (state.requestSignature === signature) {
    return;
  }
  state.requestSignature = signature;
  select("#chat-requests").replaceChildren(
    ...requests.map((request) => {
      const { params } = request;
      const card = element(
          "div",
          { class: "request" },
          element("p", {}, params.reason || params.message || "Codex needs your input."),
        ),
        reply = async (answer) => {
          try {
            await api("/api/chat/reply", {
              id: request.id,
              repository: state.repository,
              ...answer,
            });
            await pollActivity();
          } catch (error) {
            toast(error.message);
          }
        };
      if (request.method === "item/tool/requestUserInput") {
        const inputs = {};
        for (const question of params.questions || []) {
          const input = element("input", {
            "aria-label": question.question,
            placeholder:
              question.options?.map((option) => option.label).join(" / ") || "Your answer",
            type: "text",
          });
          inputs[question.id] = input;
          card.append(element("label", {}, question.question, input));
        }
        card.append(
          element(
            "button",
            {
              class: "button small",
              onclick: () =>
                reply({
                  answers: Object.fromEntries(
                    Object.entries(inputs).map(([id, input]) => [id, { answers: [input.value] }]),
                  ),
                }),
            },
            "Reply",
          ),
        );
      } else {
        card.append(
          element(
            "pre",
            {},
            params.command ||
              (params.permissions
                ? JSON.stringify(params.permissions, null, 2)
                : params.grantRoot || request.method),
          ),
        );
        const actions = element("div", { class: "request-actions" });
        if (request.method.endsWith("/requestApproval")) {
          actions.append(
            element(
              "button",
              { class: "button small primary", onclick: () => reply({ decision: "accept" }) },
              "Allow once",
            ),
          );
        }
        actions.append(
          element(
            "button",
            { class: "button small", onclick: () => reply({ decision: "decline" }) },
            "Decline",
          ),
        );
        card.append(actions);
      }
      return card;
    }),
  );
}

async function connect() {
  state.connecting = true;
  renderAgent();
  try {
    const result = await api("/api/agent/connect", {});
    state.agent = result;
    if (result.login?.authUrl) {
      window.open(result.login.authUrl, "_blank", "noopener,noreferrer");
      toast("Complete sign-in using the link above the chat.");
    }
    renderAgent();
  } catch (error) {
    state.agent.error = error.message;
    renderAgent();
    toast(error.message);
  } finally {
    state.connecting = false;
    renderAgent();
  }
}

async function askAboutJob(job) {
  await submitChat({
    repository: job.repository,
    resource: job.resource,
    job: job.id,
    message: `Explain the ${job.action} output for ${job.resource} and suggest next steps.`,
  });
}

async function submitChat(body) {
  if (!state.agent.connected) {
    toast("Connect Codex to send a message.");
    select("#connect").focus();
    return false;
  }
  if (state.submitting || state.activity.get(body.repository)?.chat.busy) {
    toast("Wait for the current Codex response or stop it first.");
    return false;
  }
  const { repository } = body;
  selectContext(repository, body.resource || null);
  state.submitting = true;
  renderAgent();
  try {
    await api("/api/chat", body);
    state.watching.add(repository);
    await refreshActivity(repository);
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  } finally {
    state.submitting = false;
    renderAgent();
  }
}

async function refreshActivity(repository) {
  try {
    const result = await api(query("/api/activity", {}, repository));
    if (!state.repositories.some((item) => item.id === repository)) {
      return;
    }
    const finished = state.activity.get(repository)?.chat.busy && !result.chat.busy;
    state.activity.set(repository, result);
    state.watching.delete(repository);
    state.jobs = [...state.activity.values()].flatMap((activity) => activity.jobs);
    state.agent = result.agent;
    renderJobs();
    if (repository === state.repository) {
      state.chat = result.chat;
      renderChat();
    }
    if (finished) {
      const report = await api(query("/api/repository", {}, repository));
      state.reports.set(repository, report);
      const item = state.repositories.find((entry) => entry.id === repository);
      if (item) {
        renderRepository(item);
        await Promise.all([loadActions(repository, state.epoch), loadDiff(repository)]);
      }
      if (repository === state.repository) {
        state.report = report;
        renderHeading();
        renderContext();
      }
    }
  } catch (error) {
    if (repository === state.repository) {
      select("#graph-status").textContent = `Connection interrupted: ${error.message}`;
    }
  }
}
let polling = false;
async function pollActivity() {
  if (polling) {
    return;
  }
  polling = true;
  const repositories = new Set(state.watching);
  if (state.report) {
    repositories.add(state.repository);
  }
  for (const [repository, activity] of state.activity) {
    if (activity.chat.busy || activity.jobs.some((job) => job.status === "running")) {
      repositories.add(repository);
    }
  }
  try {
    state.agent = await api("/api/agent");
    renderAgent();
    await inBatches([...repositories], refreshActivity);
  } catch (error) {
    select("#agent-status").textContent = `Connection interrupted: ${error.message}`;
    select("#agent-status").hidden = false;
  } finally {
    polling = false;
  }
}
for (const [selector, name, label] of [
  ["#refresh", "refresh", "Refresh workspace"],
  ["#fit", "fit", "Fit all repositories"],
  ["#zoom-in", "plus", "Zoom in"],
  ["#zoom-out", "minus", "Zoom out"],
  ["#stop-chat", "stop", "Stop Codex"],
  ["#send", "send", "Send message"],
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
select("#fit").addEventListener("click", fit);
select("#zoom-in").addEventListener("click", () => zoom(state.zoom + 0.1));
select("#zoom-out").addEventListener("click", () => zoom(state.zoom - 0.1));
select("#connect").addEventListener("click", connect);
select("#chat-form").addEventListener("submit", sendMessage);
select("#message").addEventListener("input", () => {
  updateMentions();
  renderAgent();
});
select("#message").addEventListener("click", updateMentions);
select("#message").addEventListener("blur", hideMentions);
select("#message").addEventListener("keydown", (event) => {
  if (event.isComposing) {
    return;
  }
  if (state.mentions.length > 0) {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      state.mentionIndex =
        (state.mentionIndex + (event.key === "ArrowDown" ? 1 : -1) + state.mentions.length) %
        state.mentions.length;
      renderMentions();
      return;
    }
    if (["Enter", "Tab"].includes(event.key) && !event.shiftKey) {
      event.preventDefault();
      chooseMention(state.mentions[state.mentionIndex]);
      return;
    }
  }
  if (event.key === "Escape") {
    hideMentions();
  } else if (event.key === "Enter" && !event.shiftKey) {
    sendMessage(event);
  }
});
select("#stop-chat").addEventListener("click", async () => {
  try {
    await api("/api/chat/stop", { repository: state.repository });
  } catch (error) {
    toast(error.message);
  }
});
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
  if (
    event.button === 0 &&
    !event.target.closest("input, textarea, select, [contenteditable='true']")
  ) {
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
document.addEventListener("keydown", (event) => {
  if (
    event.key === "/" &&
    !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)
  ) {
    event.preventDefault();
    select("#search").focus();
  }
});
window.addEventListener("resize", drawEdges);
try {
  state.agent = await api("/api/agent");
  renderAgent();
} catch (error) {
  toast(error.message);
}
await loadWorkspace();
setInterval(pollActivity, 1800);
setInterval(() => {
  if (!state.refreshing) {
    inBatches(state.repositories, (repository) => loadDiff(repository.id));
  }
}, 10_000);
