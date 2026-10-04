"use strict";

const $ = (id) => document.getElementById(id);
const CARD_WIDTH = 260;
const GROUP_PADDING = [52, 12, 12, 12];
const LAYOUT_SPACING = { node: 8, combo: 12, rank: 24 };
const expanded = new Map();
const searchCollapsed = new Set();
const blocks = new Map();
const directorySnapshots = new Map();
const directoryViews = new Map();
const actionStates = new Map();
const actionViews = new Map();
const actionTimers = new Map();
const runArguments = new Map();
let restoringFocus = false;
let keyboardFocus = false;
let pendingView = null;
let pendingFocus = null;
let data = null;
let directory = new URLSearchParams(location.search).get("directory");
let query = "";
let changesOnly = false;
let selected = null;
let currentNodes = [];
let hoveredResource = null;
let hoveredEdge = null;
let graph = null;
let model = null;
let observer = null;
let requestGeneration = 0;
let generation = 0;
let pendingFrame = null;
let work = Promise.resolve();
let drag = null;
let suppressClick = false;

class DirectoryCombo extends G6.RectCombo {
  getLabelStyle(attributes) {
    const label = super.getLabelStyle(attributes);
    if (!label) return label;
    const [width, height] = this.getKeySize(attributes);
    return {
      ...label,
      x: -width / 2 + 36,
      y: -height / 2 + 8,
      textAlign: "left",
      textBaseline: "top",
      cursor: "pointer",
      wordWrap: true,
      wordWrapWidth: width - 52,
      maxLines: 3,
      textOverflow: "ellipsis",
    };
  }
  getIconStyle(attributes) {
    const [width, height] = this.getKeySize(attributes);
    return {
      src: attributes.iconSrc,
      cursor: "pointer",
      width: 16,
      height: 16,
      x: -width / 2 + 22,
      y: -height / 2 + 16,
    };
  }
}
G6.register(G6.ExtensionCategory.COMBO, "canonical-directory", DirectoryCombo);

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

const iconTemplates = new Map(
  Object.entries(window.canonicalIcons).map(([name, svg]) => [
    name,
    new DOMParser().parseFromString(svg, "image/svg+xml").documentElement,
  ]),
);

function icon(name, className, label) {
  const item = iconTemplates.get(name).cloneNode(true);
  item.setAttribute("class", className);
  if (label) {
    item.setAttribute("role", "img");
    item.setAttribute("aria-label", label);
  } else item.setAttribute("aria-hidden", "true");
  return item;
}

function kindIcon(kind) {
  const names = {
    directory: "Directory",
    package: "Package",
    "package-reference": "Package",
    host: "Computer",
    machine: "Computer",
  };
  const icons = {
    directory: "folder",
    package: "package",
    "package-reference": "package",
    host: "monitor",
    machine: "monitor",
  };
  return icon(icons[kind] || "monitor", "kind-icon", names[kind] || kind);
}

function actionIcon(action) {
  return icon({ check: "check", run: "play", stop: "square" }[action], "action-icon");
}

function languageIcon(kind) {
  const names = { python: "Python", html: "HTML", nix: "Nix", nixos: "NixOS", latex: "LaTeX" };
  const icons = { python: "python", html: "html5", nix: "nixos", nixos: "nixos", latex: "latex" };
  return names[kind] ? icon(icons[kind], "language-icon", names[kind]) : null;
}

function hasChange(tree) {
  return Boolean(tree.change) || (tree.children || []).some(hasChange);
}

function countLeaves(tree) {
  if (tree.warning && !tree.children?.length) return 0;
  return tree.children?.length
    ? tree.children.reduce((sum, child) => sum + countLeaves(child), 0)
    : 1;
}

function flattenText(tree) {
  return [tree.title, ...(tree.children || []).map(flattenText)].join(" ");
}

function changeTally(tree) {
  const counts = { added: 0, removed: 0, modified: 0 };
  function count(node) {
    if (node.change) counts[node.change] += 1;
    for (const child of node.children || []) count(child);
  }
  count(tree);
  const tally = element("span", "diff-tally");
  for (const [kind, symbol] of [
    ["added", "+"],
    ["removed", "−"],
    ["modified", "~"],
  ]) {
    if (!counts[kind]) continue;
    const value = element("span", kind, `${symbol}${counts[kind]}`);
    value.title = `${counts[kind]} ${kind}`;
    tally.append(value);
  }
  return tally;
}

function fieldComparison(children, field) {
  const label = { name: "Name", description: "Description", help: "Help" }[field];
  const comparison = element("section", "field-comparison");
  comparison.append(element("div", "comparison-heading", label));
  for (const [kind, label] of [
    ["removed", "HEAD"],
    ["added", "Working tree"],
  ]) {
    const declaration = children.find((child) => child.field === field && child.change === kind);
    const side = element("div", `comparison-side ${kind}`);
    side.append(
      element("span", "comparison-label", label),
      element("div", "", declaration?.value ?? "(not declared)"),
    );
    comparison.append(side);
  }
  return comparison;
}

function prepareData(snapshot) {
  const nodes = snapshot.nodes.map((node) => ({ ...node }));
  for (const node of nodes) {
    node.changed = node.tree ? hasChange(node.tree) : false;
    node.searchText = [
      node.name,
      node.path,
      node.repository,
      node.description,
      node.overview,
      node.expression,
      node.tree ? flattenText(node.tree) : "",
    ]
      .join(" ")
      .toLowerCase();
  }
  return { ...snapshot, nodes };
}

function relationships() {
  return data.edges.filter((edge) => !["contains", "submodule", "checked-by"].includes(edge.kind));
}

function visibleNodes() {
  const resources = data.nodes.filter(
    (node) => !["repository", "directory", "check", "machine"].includes(node.kind),
  );
  const matches = new Set(
    resources
      .filter(
        (node) => (!changesOnly || node.changed) && (!query || node.searchText.includes(query)),
      )
      .map((node) => node.id),
  );
  const included = new Set(matches);
  if (query || changesOnly) {
    for (const edge of relationships()) {
      if (matches.has(edge.source) || matches.has(edge.target)) {
        included.add(edge.source);
        included.add(edge.target);
      }
    }
  }
  return resources
    .filter((node) => included.has(node.id))
    .map((node) => ({ ...node, context: !matches.has(node.id) }));
}

function visibleEdges() {
  const ids = new Set(currentNodes.map((node) => node.id));
  return relationships().filter((edge) => ids.has(edge.source) && ids.has(edge.target));
}

function updateCollapseState() {
  const canCollapse = Boolean(
    document.querySelector("#canvas details[open]") || [...expanded.values()].some(Boolean),
  );
  const button = $("collapse");
  const label = canCollapse ? "Collapse all" : "Expand all";
  button.disabled = !canCollapse && !document.querySelector("#canvas details");
  button.textContent = canCollapse ? "⊟" : "⊞";
  button.title = label;
  button.setAttribute("aria-label", label);
}

function bindExpansion(details, key, matchesSearch = false, changed = false) {
  details.dataset.expansionKey = key;
  const automatic = (query && matchesSearch) || (changesOnly && changed);
  details.open = automatic ? !searchCollapsed.has(key) : Boolean(expanded.get(key));
  let previous = details.open;
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (previous === details.open) return;
    expanded.set(key, details.open);
    if (automatic) {
      if (details.open) searchCollapsed.delete(key);
      else searchCollapsed.add(key);
    }
    previous = details.open;
    updateCollapseState();
    if ($("canvas").contains(details)) scheduleLayout();
  });
}

function behaviorTree(tree, key, filterChanges) {
  const children = tree.children || [];
  function appendLines(row) {
    if (tree.lines == null) return;
    const count = tree.lines;
    row.append(
      element(
        "span",
        "source-lines",
        `${count.toLocaleString()} ${count === 1 ? "line" : "lines"}`,
      ),
    );
  }
  function appendDiffoscope(row) {
    if (!tree.output_diff) return;
    const link = element("a", "diffoscope-button", "Diffoscope");
    link.href = tree.output_diff;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = `Compare ${tree.title} with the previous capture`;
    link.addEventListener("click", (event) => event.stopPropagation());
    row.append(link);
  }
  if (!children.length && !tree.expandable && !tree.text_diff) {
    const matches = query && tree.title.toLowerCase().includes(query);
    const row = element("div", "output-row");
    const leaf = element(
      "div",
      `detail-leaf ${tree.change || ""}${tree.warning ? " warning" : ""}${matches ? " match" : ""}`,
      tree.title,
    );
    if (tree.source_file) leaf.classList.add("source-row");
    appendLines(leaf);
    if (!tree.output_diff) return leaf;
    row.append(leaf);
    appendDiffoscope(row);
    return row;
  }
  const details = element("details", `behavior-group${hasChange(tree) ? " changed" : ""}`);
  const visible = children.filter((child) => !filterChanges || hasChange(child));
  bindExpansion(details, key, flattenText(tree).toLowerCase().includes(query), hasChange(tree));
  const summary = element("summary", "", tree.title);
  appendLines(summary);
  if (!tree.source_file && (tree.expandable || children.length))
    summary.append(
      element("span", "count", String(visible.reduce((sum, child) => sum + countLeaves(child), 0))),
    );
  if (hasChange(tree)) summary.append(changeTally(tree));
  appendDiffoscope(summary);
  details.append(summary);
  const content = element("div", "behavior-content");
  if (tree.text_diff) content.append(element("pre", "output-text-diff", tree.text_diff));
  children.forEach((child, index) => {
    if (!filterChanges || hasChange(child))
      content.append(behaviorTree(child, `${key}/${index}`, filterChanges));
  });
  details.append(content);
  return details;
}

async function packageAction(packagePath, action = null) {
  clearTimeout(actionTimers.get(packagePath));
  try {
    const response = await fetch(
      action ? "/api/action" : `/api/action?directory=${encodeURIComponent(packagePath)}`,
      action
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              directory: packagePath,
              action,
              args: runArguments.get(packagePath) || "",
            }),
          }
        : {},
    );
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || `HTTP ${response.status}`);
    const previous = actionStates.get(packagePath);
    actionStates.set(packagePath, state);
    actionViews.get(packagePath)?.(state);
    if (state.state === "running")
      actionTimers.set(
        packagePath,
        setTimeout(() => packageAction(packagePath), 1000),
      );
    else if (action || previous?.state === "running") {
      directorySnapshots.clear();
      refresh(directory, false);
    }
  } catch (error) {
    actionViews.get(packagePath)?.({ state: "failed", output: error.message });
  }
}

function packageControls(node, meta, body, details) {
  const packagePath = node.actions.directory;
  const controls = element("div", "package-controls");
  const argumentsInput = element("input", "run-arguments");
  argumentsInput.placeholder = "Run arguments (optional)";
  argumentsInput.setAttribute("aria-label", `Run arguments for ${node.name}`);
  argumentsInput.value = runArguments.get(packagePath) || "";
  argumentsInput.addEventListener("input", () =>
    runArguments.set(packagePath, argumentsInput.value),
  );
  const buttons = {};
  for (const action of ["check", "run", "stop"]) {
    const label = action[0].toUpperCase() + action.slice(1);
    const button = element("button", "package-action");
    button.type = "button";
    button.setAttribute("aria-label", `${label} ${node.name}`);
    button.title = action === "check" && !node.actions.check ? "No declared check" : label;
    button.append(actionIcon(action));
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      details.open = true;
      update({ state: "running", action, output: "Starting…" });
      packageAction(packagePath, action);
    });
    buttons[action] = button;
    controls.append(button);
  }
  const result = element("details", "behavior-group action-result");
  const status = element("summary", "");
  const output = element("pre", "output-text-diff action-output");
  result.append(status, output);
  bindExpansion(result, `${node.id}/action-log`);
  result.setAttribute("aria-live", "polite");
  function update(state) {
    const running = state.state === "running";
    buttons.check.disabled = running || !node.actions.check;
    buttons.run.disabled = running;
    buttons.stop.hidden = !running;
    argumentsInput.disabled = running;
    result.hidden = state.state === "idle";
    status.textContent = `${state.action || "Command"}: ${state.state}${state.exit_code != null ? ` (exit ${state.exit_code})` : ""}`;
    output.textContent = state.output || "No output.";
  }
  actionViews.set(packagePath, update);
  update(actionStates.get(packagePath) || { state: "idle" });
  meta.append(controls);
  body.append(argumentsInput, result);
  if (!actionStates.has(packagePath)) packageAction(packagePath);
}

function resourceBlock(node) {
  const details = element(
    "details",
    `resource-block ${node.kind}${node.changed ? " changed" : ""}${node.id === selected ? " selected" : ""}`,
  );
  details.dataset.nodeId = node.id;
  bindExpansion(
    details,
    node.id,
    Boolean(node.tree && flattenText(node.tree).toLowerCase().includes(query)),
    node.changed,
  );
  const summary = element("summary", "resource-header");
  const title = element("div", "resource-title");
  const icon = languageIcon(node.icon || node.package_type);
  title.append(
    kindIcon(node.kind),
    element("strong", "", node.name),
    element("span", "chevron", "›"),
  );
  if (icon) title.insertBefore(icon, title.querySelector(".chevron"));
  summary.append(title);
  if (node.description) summary.append(element("p", "resource-description", node.description));
  if (!["package", "package-reference"].includes(node.kind))
    summary.append(element("div", "resource-path", node.path));
  const meta = element("div", "resource-meta");
  if (node.context) details.classList.add("context");
  if (node.changed)
    meta.append(
      element("span", "change-badge", node.removed ? "− Removed" : "◉ Changed"),
      changeTally(node.tree),
    );
  if (meta.childNodes.length) summary.append(meta);
  summary.addEventListener("click", () => {
    selected = node.id;
    for (const [id, other] of blocks) other.classList.toggle("selected", id === node.id);
    highlightEdges();
  });
  details.append(summary);
  const body = element("div", "resource-body");
  if (node.actions) {
    packageControls(node, meta, body, details);
    if (!summary.contains(meta)) summary.append(meta);
  }
  if (node.tree) {
    if (node.tree.children.some((child) => child.field !== "tmp" && hasChange(child)))
      body.append(element("div", "diff-baseline", "HEAD → Working tree"));
    const compared = new Set();
    node.tree.children.forEach((child, index) => {
      if (["name", "description", "help"].includes(child.field) && child.change) {
        if (!compared.has(child.field)) {
          compared.add(child.field);
          body.append(fieldComparison(node.tree.children, child.field));
        }
        return;
      }
      if (["name", "description", "dependencies", "connections"].includes(child.field)) return;
      if (!changesOnly || node.context || hasChange(child))
        body.append(behaviorTree(child, `${node.id}/tree/${index}`, changesOnly && !node.context));
    });
  }
  if (node.expression) body.append(element("div", "detail-leaf", node.expression));
  details.append(body);
  blocks.set(node.id, details);
  return details;
}

function viewport() {
  const rect = $("canvas").getBoundingClientRect();
  const top =
    Math.max(
      $("cwd").getBoundingClientRect().bottom,
      document.querySelector(".graph-controls").getBoundingClientRect().bottom,
    ) -
    rect.top +
    16;
  return { width: rect.width - 32, height: rect.height - top - 16, top };
}

function directoryCounts(relative) {
  const nodes = data.nodes.filter(
    (node) =>
      !node.removed &&
      (relative === "." ||
        node.repository === relative ||
        node.repository.startsWith(`${relative}/`)),
  );
  const count = (kind) =>
    nodes.filter((node) => node.kind === kind && !["directory", "home"].includes(node.profile))
      .length;
  if (
    !nodes.some(
      (node) =>
        node.kind === "repository" && node.profile === "flake" && node.repository === relative,
    )
  ) {
    if (nodes.some((node) => node.kind === "directory")) {
      const directories = count("directory");
      return `${directories.toLocaleString()} ${directories === 1 ? "directory" : "directories"}`;
    }
    const repositories = count("repository");
    return `${repositories.toLocaleString()} ${repositories === 1 ? "repository" : "repositories"}`;
  }
  const lines = nodes
    .filter((node) => ["package", "host"].includes(node.kind))
    .reduce(
      (sum, node) =>
        sum + Object.values(node.source_metrics?.lines || {}).reduce((a, b) => a + b, 0),
      0,
    );
  return [
    [count("package"), "package", "packages"],
    [count("host"), "host", "hosts"],
    [lines, "line", "lines"],
  ]
    .map(
      ([count, singular, plural]) => `${count.toLocaleString()} ${count === 1 ? singular : plural}`,
    )
    .join(" · ");
}

function buildModel() {
  blocks.clear();
  $("measure").replaceChildren();
  $("directories").replaceChildren();
  currentNodes = visibleNodes();
  if (
    !currentNodes.length &&
    (query ||
      changesOnly ||
      !data.nodes.some((node) => ["repository", "directory"].includes(node.kind)))
  )
    return null;
  const combos = new Map();
  const rootId = `directory:${data.root}`;
  const machineId = data.root === data.machine?.home ? `machine:${data.machine.name}` : null;
  const navigation = $("directories");
  navigation.replaceChildren();
  function combo(id, name, parent, destination, relative) {
    const icon = kindIcon(destination ? "directory" : "machine");
    icon.setAttribute("stroke", "#346747");
    const label = destination ? `${name} · ${directoryCounts(relative)}` : name;
    const result = {
      id,
      combo: parent,
      data: { directory: destination },
      style: {
        padding: GROUP_PADDING,
        collapsedSize: [280, 40],
        labelText: label,
        labelFontSize: 12,
        labelFill: "#346747",
        iconSrc: `data:image/svg+xml,${encodeURIComponent(new XMLSerializer().serializeToString(icon))}`,
        fill: destination ? "#f1f5ec" : "#edf2f8",
        fillOpacity: 0.6,
        stroke: "#cfdcca",
        radius: 12,
      },
    };
    combos.set(id, result);
    if (destination) {
      const button = element("button", "", destination);
      button.addEventListener("click", () => navigateDirectory(destination));
      button.addEventListener("focus", () => graph?.focusElement(id, false));
      navigation.append(button);
    }
    return id;
  }
  if (machineId)
    combo(machineId, `${data.machine.description} · ${data.machine.name}`, undefined, null);
  combo(rootId, data.root.split("/").pop() || "/", machineId || undefined, data.root, ".");
  function directoryCombo(relative) {
    let parent = rootId;
    let path = "";
    for (const name of relative === "." ? [] : relative.split("/")) {
      path = path ? `${path}/${name}` : name;
      const destination = `${data.root}/${path}`;
      const id = `directory:${destination}`;
      if (!combos.has(id)) combo(id, name, parent, destination, path);
      parent = id;
    }
    return parent;
  }
  if (!query && !changesOnly)
    for (const node of data.nodes)
      if (["repository", "directory"].includes(node.kind)) directoryCombo(node.repository);
  const measure = $("measure");
  measure.replaceChildren();
  const nodes = currentNodes.map((node) => {
    const parent = directoryCombo(node.repository);
    const block = resourceBlock(node);
    measure.append(block);
    const height = block.offsetHeight;
    return {
      id: node.id,
      combo: parent,
      style: { size: [CARD_WIDTH, height], dx: -CARD_WIDTH / 2, dy: -height / 2, innerHTML: block },
    };
  });
  if (machineId) {
    const info = element("details", "resource-block machine-info");
    info.dataset.nodeId = "machine-info";
    bindExpansion(info, "machine-info");
    info.append(element("summary", "resource-header", "System details"));
    const body = element("div", "resource-body");
    for (const fact of data.machine.details) body.append(element("div", "detail-leaf", fact));
    info.append(body);
    measure.append(info);
    const height = info.offsetHeight;
    nodes.push({
      id: "machine-info",
      combo: machineId,
      style: { size: [CARD_WIDTH, height], dx: -CARD_WIDTH / 2, dy: -height / 2, innerHTML: info },
    });
  }
  const edges = visibleEdges().map((edge, index) => ({
    ...edge,
    id: `relationship:${index}`,
    style: {
      labelText: `${edge.change === "removed" ? "− " : edge.change === "added" ? "+ " : ""}${edge.kind}`,
      stroke: edge.change === "removed" ? "#b95656" : "#81a589",
    },
  }));
  // Invisible column groups let G6 stack unequal directory heights compactly.
  const area = viewport();
  if (area.width >= area.height) {
    for (const parent of [...combos.values()]) {
      const children = [...combos.values()]
        .filter((child) => child.combo === parent.id && !child.data.layoutColumn)
        .sort((a, b) => a.id.localeCompare(b.id));
      if (children.length < 2) continue;
      const columns = [[], []];
      const heights = [0, 0];
      const contentHeight = (child) =>
        GROUP_PADDING[0] +
        GROUP_PADDING[2] +
        nodes
          .filter((node) => node.combo === child.id || node.combo.startsWith(`${child.id}/`))
          .reduce((sum, node) => sum + node.style.size[1] + LAYOUT_SPACING.node, 0);
      for (const child of children.sort(
        (a, b) => contentHeight(b) - contentHeight(a) || a.id.localeCompare(b.id),
      )) {
        const column = heights[0] <= heights[1] ? 0 : 1;
        columns[column].push(child);
        heights[column] += contentHeight(child);
      }
      columns.forEach((children, column) => {
        const id = `layout:${parent.id}:${column}`;
        combos.set(id, {
          id,
          combo: parent.id,
          data: { layoutColumn: true },
          style: { padding: 0, fillOpacity: 0, lineWidth: 0, pointerEvents: "none" },
        });
        children.forEach((child) => {
          child.combo = id;
        });
      });
    }
  }
  return { nodes, edges, combos: [...combos.values()] };
}

function graphOptions() {
  const area = viewport();
  const sizes = new Map(model.nodes.map((node) => [node.id, node.style.size]));
  const endpoints = new Set(model.edges.flatMap((edge) => [edge.source, edge.target]));
  const parentGroups = new Set(model.combos.map((combo) => combo.combo || null));
  const dependencyGroups = new Set(
    model.nodes.filter((node) => endpoints.has(node.id)).map((node) => node.combo),
  );
  const columnParents = new Set(
    model.combos.filter((combo) => combo.data.layoutColumn).map((combo) => combo.combo),
  );
  return {
    width: $("canvas").clientWidth,
    height: $("canvas").clientHeight,
    padding: [area.top, 16, 16, 16],
    layout: {
      type: "combo-combined",
      nodeSize: (node) => sizes.get(node.id) || [320, 80],
      nodeSpacing: LAYOUT_SPACING.node,
      comboSpacing: LAYOUT_SPACING.combo,
      comboPadding: (combo) => (combo?.id?.startsWith("layout:") ? 0 : GROUP_PADDING[0]),
      layout: (group) =>
        group?.startsWith("machine:") || parentGroups.has(group)
          ? {
              type: "dagre",
              rankdir: columnParents.has(group) ? "TB" : "LR",
              nodesep: LAYOUT_SPACING.node,
              ranksep: LAYOUT_SPACING.rank,
            }
          : dependencyGroups.has(group)
            ? {
                type: "dagre",
                rankdir: area.width >= area.height ? "LR" : "TB",
                nodesep: LAYOUT_SPACING.node,
                ranksep: LAYOUT_SPACING.rank,
              }
            : {
                type: "grid",
                width: area.width,
                height: area.height,
                sortBy: "id",
                condense: true,
                preventOverlap: true,
                nodeSpacing: LAYOUT_SPACING.node,
              },
    },
  };
}

function createGraph() {
  graph = new G6.Graph({
    container: $("canvas"),
    ...graphOptions(),
    data: model,
    animation: false,
    zoomRange: [0.001, 4],
    node: { type: "html" },
    combo: {
      type: (combo) => (combo.data.layoutColumn ? "rect" : "canonical-directory"),
      state: { hovered: { fill: "#e6eddf" } },
    },
    edge: {
      type: "polyline",
      style: {
        endArrow: true,
        radius: 5,
        lineWidth: 1.3,
        labelFontSize: 9,
        labelFill: "#66836c",
        router: { type: "orth" },
      },
      state: { related: { stroke: "#426d49", lineWidth: 2.5, labelFill: "#346747" } },
    },
    behaviors: [
      { type: "drag-canvas", enable: true },
      { type: "zoom-canvas", preventDefault: true },
    ],
  });
  graph.on("combo:click", (event) => {
    if (suppressClick || drag?.moved) return;
    const destination = graph.getComboData(event.target.id).data.directory;
    const title = event.target.getShape("label");
    const icon = event.target.getShape("icon");
    if (!destination) return;
    for (let target = event.originalTarget; target; target = target.parentNode) {
      if (target === title || target === icon) {
        navigateDirectory(destination);
        return;
      }
    }
  });
  for (const [event, active] of [
    ["combo:pointerenter", true],
    ["combo:pointerleave", false],
  ])
    graph.on(event, (event) => {
      if (!graph.getComboData(event.target.id).data.layoutColumn)
        graph.setElementState({ [event.target.id]: active ? ["hovered"] : [] }, false);
    });
  for (const [event, active] of [
    ["edge:pointerenter", true],
    ["edge:pointerleave", false],
  ])
    graph.on(event, (event) => {
      hoveredEdge = active ? event.target.id : null;
      highlightEdges();
    });
}

function captureFocus() {
  const element = document.activeElement;
  if (!$("canvas").contains(element) || element === $("canvas")) return null;
  const details = element.closest("details[data-expansion-key]");
  return {
    element,
    key: details?.dataset.expansionKey,
    summary: element.tagName === "SUMMARY",
    point: [element.getBoundingClientRect().left, element.getBoundingClientRect().top],
  };
}

async function restoreFocus(focus, anchor) {
  if (!focus) return;
  const element = focus.element.isConnected
    ? focus.element
    : focus.summary
      ? [...$("canvas").querySelectorAll("details[data-expansion-key]")]
          .find((details) => details.dataset.expansionKey === focus.key)
          ?.querySelector(":scope > summary")
      : null;
  if (!element) return;
  restoringFocus = true;
  element.focus({ preventScroll: true });
  restoringFocus = false;
  if (anchor) {
    const rect = element.getBoundingClientRect();
    await graph.translateBy([focus.point[0] - rect.left, focus.point[1] - rect.top], false);
    await new Promise(requestAnimationFrame);
  }
}

function renderBreadcrumbs() {
  const home = data.machine?.home;
  const parts = directory.split("/").filter(Boolean);
  const crumbs = [];
  let path = "";
  for (const [index, name] of parts.entries()) {
    crumbs.push(element("span", "breadcrumb-separator", "/"));
    path += `/${name}`;
    const current = index === parts.length - 1;
    const allowed =
      !home ||
      !(directory === home || directory.startsWith(`${home}/`)) ||
      path === home ||
      path.startsWith(`${home}/`);
    const item = element(!current && allowed ? "a" : "span", "breadcrumb", name);
    if (current) {
      item.setAttribute("aria-current", "page");
      item.tabIndex = -1;
    }
    if (!current && allowed) {
      const destination = path;
      const url = new URL(location.href);
      url.searchParams.set("directory", destination);
      item.href = url;
      item.title = `Open ${destination}`;
      item.addEventListener("click", (event) => {
        if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)
          return;
        event.preventDefault();
        navigateDirectory(destination);
      });
    }
    crumbs.push(item);
  }
  if (!parts.length) {
    const root = element("span", "breadcrumb", "/");
    root.setAttribute("aria-current", "page");
    root.tabIndex = -1;
    crumbs.push(root);
  }
  $("cwd").replaceChildren(...crumbs);
  $("cwd").title = directory;
  $("cwd").hidden = false;
}

let rebuildPending = false;
let fitPending = false;
let outputPoll = null;
function scheduleLayout(rebuild = false, fit = false) {
  const focus = captureFocus();
  if (focus) {
    if (!pendingFocus || pendingFocus.element !== focus.element) pendingFocus = focus;
  } else if (document.activeElement !== document.body) pendingFocus = null;
  rebuildPending ||= rebuild;
  fitPending ||= fit;
  const current = ++generation;
  $("canvas").dataset.layout = "pending";
  if (pendingFrame !== null) cancelAnimationFrame(pendingFrame);
  pendingFrame = requestAnimationFrame(() => {
    pendingFrame = null;
    work = work
      .catch(() => {})
      .then(async () => {
        if (current !== generation || !data) return;
        const focus = pendingFocus;
        if (rebuildPending) {
          observer?.disconnect();
          observer = null;
          graph?.destroy();
          graph = null;
          model = buildModel();
          rebuildPending = false;
          if (!model) {
            graph?.destroy();
            graph = null;
            $("canvas").replaceChildren(
              element(
                "p",
                "empty",
                changesOnly
                  ? "No semantic changes match this filter."
                  : "No resources match your search.",
              ),
            );
            if (pendingView)
              $("cwd").querySelector('[aria-current="page"]').focus({ preventScroll: true });
            pendingView = pendingFocus = null;
            fitPending = false;
            $("canvas").dataset.layout = "ready";
            updateCollapseState();
            return;
          }
        } else if (model) {
          for (const node of model.nodes) {
            const height = node.style.innerHTML.offsetHeight;
            if (height) node.style.size = [CARD_WIDTH, height];
            node.style.dy = -node.style.size[1] / 2;
          }
        }
        if (!graph) {
          $("canvas").replaceChildren();
          createGraph();
        } else {
          graph.setOptions(graphOptions());
          graph.setData(model);
        }
        await graph.render();
        await new Promise(requestAnimationFrame);
        if (current !== generation) return;
        if (!observer) {
          const heights = new WeakMap();
          observer = new ResizeObserver((entries) => {
            let changed = false;
            for (const { target } of entries) {
              const height = target.offsetHeight;
              if (heights.get(target) !== height) changed = true;
              heights.set(target, height);
            }
            if (changed) scheduleLayout();
          });
          for (const node of model.nodes) {
            heights.set(node.style.innerHTML, node.style.innerHTML.offsetHeight);
            observer.observe(node.style.innerHTML);
          }
        }
        if (pendingView) {
          const view = pendingView;
          pendingView = null;
          if (
            view.viewport &&
            view.viewport.width === innerWidth &&
            view.viewport.height === innerHeight
          ) {
            await graph.zoomTo(view.viewport.zoom, false);
            const point = graph.getViewportByCanvas(view.viewport.center);
            await graph.translateBy([innerWidth / 2 - point[0], innerHeight / 2 - point[1]], false);
          } else await fitGraph();
          await new Promise(requestAnimationFrame);
          fitPending = false;
          $("cwd").querySelector('[aria-current="page"]').focus({ preventScroll: true });
        } else if (fitPending) {
          await fitGraph();
          fitPending = false;
          await restoreFocus(focus, false);
        } else await restoreFocus(focus, true);
        if (current !== generation) return;
        pendingFocus = null;
        $("canvas").dataset.layout = "ready";
        updateCollapseState();
        highlightEdges();
      })
      .catch((error) => showError(`Could not arrange the diagram: ${error.message}`));
  });
}

async function fitGraph() {
  if (!graph) return;
  await graph.fitView({ when: "always" }, false);
  if (graph.getZoom() > 1) await graph.zoomTo(1, false);
  await new Promise(requestAnimationFrame);
}

function highlightEdges() {
  if (!graph || !model || $("canvas").dataset.layout !== "ready") return;
  const states = {};
  const related = new Set();
  for (const edge of model.edges) {
    const hovered = [edge.source, edge.target].includes(hoveredResource) || edge.id === hoveredEdge;
    states[edge.id] = hovered || [edge.source, edge.target].includes(selected) ? ["related"] : [];
    if (hovered) {
      related.add(edge.source);
      related.add(edge.target);
    }
  }
  graph.setElementState(states, false);
  for (const [id, block] of blocks) block.classList.toggle("edge-related", related.has(id));
}

function showError(message) {
  $("message").hidden = false;
  $("message").textContent = message;
}

async function refresh(
  nextDirectory = directory,
  force = true,
  historyMode = "replace",
  view = null,
) {
  const current = ++requestGeneration;
  clearTimeout(outputPoll);
  $("refresh").disabled = true;
  $("canvas").dataset.layout = "pending";
  try {
    let snapshot = force ? null : directorySnapshots.get(nextDirectory);
    if (snapshot?.output_pending) snapshot = null;
    if (!snapshot) {
      const parameters = new URLSearchParams();
      if (nextDirectory) parameters.set("directory", nextDirectory);
      if (force) parameters.set("refresh", "1");
      const response = await fetch(`/api/overview?${parameters}`);
      snapshot = await response.json();
      if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
    }
    if (current !== requestGeneration) return;
    if (force) {
      directorySnapshots.clear();
      directoryViews.clear();
    }
    directorySnapshots.set(snapshot.root, snapshot);
    pendingView = view;
    data = prepareData(snapshot);
    directory = data.root;
    renderBreadcrumbs();
    const url = new URL(location.href);
    url.searchParams.delete("renderer");
    url.searchParams.set("directory", directory);
    if (historyMode !== "none")
      history[historyMode === "push" ? "pushState" : "replaceState"]({ directory }, "", url);
    document.title = `Canonical — ${directory}`;
    $("message").hidden = !data.warning;
    $("message").textContent = data.warning || "";
    const focused = data.nodes.find((node) => `${node.directory}/${node.path}` === directory);
    if (!view?.viewport && focused && ["package", "host"].includes(focused.kind))
      expanded.set(focused.id, true);
    scheduleLayout(true, true);
    if (snapshot.output_pending) outputPoll = setTimeout(() => refresh(directory, false), 2000);
  } catch (error) {
    if (current === requestGeneration)
      showError(`Could not read the directory: ${error.message}. Use Refresh to try again.`);
  } finally {
    if (current === requestGeneration) $("refresh").disabled = false;
  }
}

function navigateDirectory(nextDirectory, historyMode = "push") {
  if (!nextDirectory || nextDirectory === data?.root) return;
  if (data && graph && $("canvas").dataset.layout === "ready")
    directoryViews.set(data.root, {
      expanded: [...expanded],
      searchCollapsed: [...searchCollapsed],
      selected,
      query,
      changesOnly,
      viewport: {
        zoom: graph.getZoom(),
        center: graph.getCanvasByViewport([innerWidth / 2, innerHeight / 2]),
        width: innerWidth,
        height: innerHeight,
      },
    });
  const view = directoryViews.get(nextDirectory);
  expanded.clear();
  searchCollapsed.clear();
  for (const [key, value] of view?.expanded || []) expanded.set(key, value);
  for (const key of view?.searchCollapsed || []) searchCollapsed.add(key);
  query = view?.query || "";
  changesOnly = view?.changesOnly || false;
  $("search").value = query;
  $("changes").checked = changesOnly;
  selected = view?.selected || null;
  hoveredResource = hoveredEdge = null;
  refresh(nextDirectory, false, historyMode, view || {});
}
window.addEventListener("popstate", () =>
  navigateDirectory(new URLSearchParams(location.search).get("directory"), "none"),
);

$("refresh").addEventListener("click", () => refresh());
$("fit").addEventListener("click", () => scheduleLayout(false, true));
$("search").addEventListener("input", (event) => {
  query = event.target.value.trim().toLowerCase();
  searchCollapsed.clear();
  scheduleLayout(true, true);
});
$("changes").addEventListener("change", (event) => {
  changesOnly = event.target.checked;
  searchCollapsed.clear();
  scheduleLayout(true, true);
});
$("collapse").addEventListener("click", () => {
  const expand = $("collapse").title === "Expand all";
  expanded.clear();
  selected = null;
  for (const details of $("canvas").querySelectorAll("details")) {
    const key = details.dataset.expansionKey;
    if (expand) {
      expanded.set(key, true);
      searchCollapsed.delete(key);
    } else searchCollapsed.add(key);
  }
  scheduleLayout(true, true);
});
window.addEventListener("resize", () => scheduleLayout(true, true));
document.addEventListener(
  "keydown",
  (event) => {
    if (event.altKey && event.key === "ArrowUp") {
      event.preventDefault();
      if (data?.parent) navigateDirectory(data.parent);
      return;
    }
    if (event.key === "/" && !event.target.closest("input,textarea")) {
      event.preventDefault();
      $("search").focus();
    }
    if (event.key === "Escape" && event.target === $("search")) {
      $("search").value = query = "";
      searchCollapsed.clear();
      scheduleLayout(true, true);
    }
    const direction = {
      ArrowLeft: [1, 0],
      ArrowRight: [-1, 0],
      ArrowUp: [0, 1],
      ArrowDown: [0, -1],
    }[event.key];
    if (
      direction &&
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey &&
      graph &&
      !event.defaultPrevented
    ) {
      event.preventDefault();
      event.stopPropagation();
      graph.translateBy(
        direction.map((value) => value * (event.shiftKey ? 160 : 40)),
        false,
      );
    }
  },
  true,
);
$("canvas").addEventListener("pointerdown", (event) => {
  suppressClick = false;
  drag = { x: event.clientX, y: event.clientY, moved: false };
});
$("canvas").addEventListener("pointermove", (event) => {
  if (drag && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 5) drag.moved = true;
});
$("canvas").addEventListener("pointerup", () => {
  suppressClick = Boolean(drag?.moved);
  drag = null;
});
$("canvas").addEventListener("pointercancel", () => {
  drag = null;
});
for (const name of ["click", "auxclick"])
  $("canvas").addEventListener(
    name,
    (event) => {
      if (suppressClick && event.detail !== 0) {
        event.preventDefault();
        event.stopImmediatePropagation();
        suppressClick = false;
      }
    },
    true,
  );
$("canvas").addEventListener("contextmenu", (event) => event.preventDefault());
$("canvas").addEventListener("pointerover", (event) => {
  hoveredResource = event.target.closest(".resource-block")?.dataset.nodeId || null;
  highlightEdges();
});
$("canvas").addEventListener("pointerout", (event) => {
  hoveredResource = event.relatedTarget?.closest?.(".resource-block")?.dataset.nodeId || null;
  highlightEdges();
});
document.addEventListener(
  "pointerdown",
  () => {
    keyboardFocus = false;
  },
  true,
);
document.addEventListener(
  "keydown",
  () => {
    keyboardFocus = true;
  },
  true,
);
$("canvas").addEventListener("focusin", (event) => {
  if (restoringFocus || !keyboardFocus) return;
  const id = event.target.closest(".resource-block")?.dataset.nodeId;
  if (id && graph) graph.focusElement(id, false);
});
// G6's HTML node forwards pointer events, but its wheel events need forwarding.
$("canvas").addEventListener(
  "wheel",
  (event) => {
    if (!graph || !event.target.closest(".resource-block")) return;
    event.preventDefault();
    const delta =
      event.deltaY *
      (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? $("canvas").clientHeight : 1);
    const rect = $("canvas").getBoundingClientRect();
    graph.zoomTo(graph.getZoom() * Math.exp(-delta * 0.0015), false, [
      event.clientX - rect.left,
      event.clientY - rect.top,
    ]);
  },
  { passive: false },
);
refresh(directory, false);
