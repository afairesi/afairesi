"use strict";

const $ = (id) => document.getElementById(id);
let data = null;
let query = "";
let changesOnly = false;
let selected = null;
let initialFocusApplied = false;
let zoom = 1;
const pan = { x: 0, y: 0 };
let currentNodes = [];
let frame = null;
let layoutObserver = null;
const expanded = new Map();
const searchCollapsed = new Set();
const blocks = new Map();
let openedEdge = null;

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function svgElement(tag, attributes = {}, text) {
  const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) item.setAttribute(name, value);
  if (text !== undefined) item.textContent = text;
  return item;
}

function languageIcon(kind) {
  const names = { python: "Python", html: "HTML", nix: "Nix", latex: "LaTeX" };
  if (!names[kind]) return null;
  const icon = svgElement("svg", {
    class: "language-icon",
    viewBox: "0 0 24 24",
    role: "img",
    "aria-label": names[kind],
  });
  icon.append(svgElement("title", {}, names[kind]));
  if (kind === "python") {
    const snake = "M12 2C6 2 6 3 6 6v3h7v1H4c-3 0-3 8 0 8h2v-3c0-3 2-4 5-4h5c2 0 3-1 3-3V6c0-3-2-4-7-4Z";
    icon.append(
      svgElement("path", { d: snake, fill: "#3776ab" }),
      svgElement("path", { d: snake, fill: "#ffd343", transform: "rotate(180 12 12)" }),
      svgElement("circle", { cx: 9, cy: 5, r: 1, fill: "white" }),
      svgElement("circle", { cx: 15, cy: 19, r: 1, fill: "white" }),
    );
  } else if (kind === "html") {
    icon.append(
      svgElement("path", { d: "M3 2h18l-2 18-7 2-7-2Z", fill: "#e44d26" }),
      svgElement("path", { d: "M7 6h10l-.2 3H10l.2 2h6.4l-.6 6-4 1-4-1-.3-3h3l.1 1 1.2.3 1.3-.3.2-2H7.6Z", fill: "white" }),
    );
  } else if (kind === "nix") {
    for (let angle = 0; angle < 360; angle += 60)
      icon.append(svgElement("path", {
        d: "M12 12V2M12 6l-4-3M12 6l4-3",
        transform: `rotate(${angle} 12 12)`,
        stroke: angle % 120 ? "#5277c3" : "#7ebae4",
        "stroke-width": 2,
        fill: "none",
      }));
  } else {
    icon.append(svgElement("text", {
      x: 12, y: 16, "text-anchor": "middle", "font-family": "serif",
      "font-size": 12, "font-weight": "bold", fill: "#008080",
    }, "TeX"));
  }
  return icon;
}

function hasChange(tree) {
  return Boolean(tree.change) || (tree.children || []).some(hasChange);
}

function countLeaves(tree) {
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
  const comparison = element("section", "field-comparison");
  comparison.append(element("div", "comparison-heading", field));
  for (const [kind, prefix, label] of [
    ["removed", "-", "HEAD"],
    ["added", "+", "Working tree"],
  ]) {
    const start = `${prefix} ${field}: `;
    const declaration = children.find((child) => child.title.startsWith(start));
    const side = element("div", `comparison-side ${kind}`);
    side.append(
      element("span", "comparison-label", label),
      element("div", "", declaration ? declaration.title.slice(start.length) : "(not declared)"),
    );
    comparison.append(side);
  }
  return comparison;
}

function prepareData(snapshot) {
  const trees = new Map();
  function walk(nodes, path = []) {
    for (const node of nodes) {
      if (node.title.startsWith("packages/"))
        trees.set(`${path.join("/") || "."}:${node.title}`, node);
      else walk(node.children || [], [...path, node.title]);
    }
  }
  walk(snapshot.tree || []);
  const nodes = snapshot.nodes.map((node) => ({ ...node, tree: trees.get(node.id) }));
  for (const [id, tree] of trees) {
    if (!nodes.some((node) => node.id === id)) {
      const [repository, name] = id.split(":packages/");
      nodes.push({
        id,
        name,
        path: `packages/${name}`,
        repository,
        kind: "package",
        description: "Removed package",
        tree,
        removed: true,
      });
    }
  }
  const edges = snapshot.edges.map((edge) => ({ ...edge }));
  for (const node of nodes) {
    const dependencies = node.tree?.children.find((child) => child.title === "Dependencies");
    for (const change of dependencies?.children || []) {
      const match = change.title.match(/^[-+] ([^:]+): (packages\/\S+)$/);
      if (!match || !change.change) continue;
      const source = `${node.repository}:${match[2]}`;
      const existing = edges.find(
        (edge) => edge.source === source && edge.target === node.id && edge.kind === match[1],
      );
      if (existing) existing.change = change.change;
      else if (change.change === "removed") {
        if (!nodes.some((provider) => provider.id === source))
          nodes.push({
            id: source,
            name: match[2].slice("packages/".length),
            path: match[2],
            repository: node.repository,
            kind: "package-reference",
            removed: true,
          });
        edges.push({ source, target: node.id, kind: match[1], change: "removed" });
      }
    }
  }
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
  return { ...snapshot, nodes, edges };
}

function relationships() {
  return data.edges.filter((edge) => !["contains", "submodule", "checked-by"].includes(edge.kind));
}

function visibleNodes() {
  const resources = data.nodes.filter((node) => !["repository", "check"].includes(node.kind));
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

// Collapse each strongly connected component before assigning dependency depths.
// Cycles remain in one lane and all their arrows stay visible.
function dependencyLevels(nodes, edges) {
  const ids = new Set(nodes.map((node) => node.id));
  const incoming = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges)
    if (ids.has(edge.source) && ids.has(edge.target)) incoming.get(edge.target).push(edge.source);
  let index = 0;
  const indices = new Map(),
    low = new Map(),
    stack = [],
    active = new Set(),
    components = [],
    componentOf = new Map();
  function visit(id) {
    indices.set(id, index);
    low.set(id, index);
    index += 1;
    stack.push(id);
    active.add(id);
    for (const other of incoming.get(id)) {
      if (!indices.has(other)) {
        visit(other);
        low.set(id, Math.min(low.get(id), low.get(other)));
      } else if (active.has(other)) low.set(id, Math.min(low.get(id), indices.get(other)));
    }
    if (low.get(id) === indices.get(id)) {
      const members = [];
      let other;
      do {
        other = stack.pop();
        active.delete(other);
        componentOf.set(other, components.length);
        members.push(other);
      } while (other !== id);
      components.push(members);
    }
  }
  for (const node of nodes) if (!indices.has(node.id)) visit(node.id);
  const depths = new Map();
  function depth(component) {
    if (depths.has(component)) return depths.get(component);
    const providers = new Set(
      components[component]
        .flatMap((id) => incoming.get(id).map((provider) => componentOf.get(provider)))
        .filter((other) => other !== component),
    );
    const value = Math.max(-1, ...[...providers].map(depth)) + 1;
    depths.set(component, value);
    return value;
  }
  return new Map(
    nodes.map((node) => [
      node.id,
      {
        depth: depth(componentOf.get(node.id)),
        cyclic:
          components[componentOf.get(node.id)].length > 1 ||
          incoming.get(node.id).includes(node.id),
      },
    ]),
  );
}

function scheduleEdges() {
  if (frame !== null) cancelAnimationFrame(frame);
  frame = requestAnimationFrame(() => {
    frame = null;
    drawEdges();
  });
}

function updateCollapseState() {
  const canCollapse = Boolean(
    openedEdge ||
    document.querySelector(".graph-stage details[open]") ||
    [...expanded.values()].some(Boolean),
  );
  const button = $("collapse");
  const label = canCollapse ? "Collapse all" : "Expand all";
  button.disabled = !canCollapse && !document.querySelector(".graph-stage details");
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
    if (previous !== details.open) {
      expanded.set(key, details.open);
      if (automatic) {
        if (details.open) searchCollapsed.delete(key);
        else searchCollapsed.add(key);
      }
      previous = details.open;
    }
    updateCollapseState();
    scheduleEdges();
  });
}

function behaviorTree(tree, key, filterChanges) {
  const children = tree.children || [];
  if (!children.length) {
    const matches = query && tree.title.toLowerCase().includes(query);
    return element(
      "div",
      `detail-leaf ${tree.change || ""}${tree.warning ? " warning" : ""}${matches ? " match" : ""}`,
      tree.title,
    );
  }
  const details = element("details", `behavior-group${hasChange(tree) ? " changed" : ""}`);
  const visible = children.filter((child) => !filterChanges || hasChange(child));
  bindExpansion(details, key, flattenText(tree).toLowerCase().includes(query), hasChange(tree));
  const summary = element("summary", "", tree.title);
  summary.append(
    element("span", "count", String(visible.reduce((sum, child) => sum + countLeaves(child), 0))),
  );
  if (hasChange(tree)) summary.append(changeTally(tree));
  details.append(summary);
  const content = element("div", "behavior-content");
  children.forEach((child, index) => {
    if (!filterChanges || hasChange(child))
      content.append(behaviorTree(child, `${key}/${index}`, filterChanges));
  });
  details.append(content);
  return details;
}

function focusResource(id) {
  const block = blocks.get(id);
  if (!block) return;
  selected = id;
  expanded.set(id, true);
  searchCollapsed.delete(id);
  block.open = true;
  for (const [otherId, other] of blocks) other.classList.toggle("selected", otherId === id);
  scheduleEdges();
  requestAnimationFrame(() => reveal(block.querySelector(".resource-header")));
}

function resourceBlock(node, layout) {
  const details = element(
    "details",
    `resource-block ${node.kind}${node.changed ? " changed" : ""}${node.id === selected ? " selected" : ""}`,
  );
  details.dataset.nodeId = node.id;
  details.dataset.depth = String(layout?.depth || 0);
  bindExpansion(
    details,
    node.id,
    Boolean(node.tree && flattenText(node.tree).toLowerCase().includes(query)),
    node.changed,
  );
  const summary = element("summary", "resource-header");
  const title = element("div", "resource-title");
  const icon = languageIcon(node.package_type);
  title.append(
    icon || element("i", "kind-dot"),
    element("strong", "", node.name),
    element("span", "chevron", "›"),
  );
  summary.append(title);
  if (node.description) summary.append(element("p", "resource-description", node.description));
  summary.append(element("div", "resource-path", node.path));
  const meta = element("div", "resource-meta");
  meta.append(element("span", "kind-tag", node.package_type || node.kind));
  if (node.context) details.classList.add("context");
  if (layout?.cyclic) meta.append(element("span", "change-badge", "dependency cycle"));
  if (node.changed)
    meta.append(
      element("span", "change-badge", node.removed ? "− Removed" : "◉ Changed"),
      changeTally(node.tree),
    );
  const tests = node.tree?.children.find((child) => child.title === "Tests");
  if (tests)
    meta.append(
      element(
        "span",
        "resource-count",
        `${tests.children.filter((child) => !child.title.startsWith("(")).length} tests`,
      ),
    );
  summary.append(meta);
  summary.addEventListener("click", () => {
    selected = node.id;
    for (const [id, other] of blocks) other.classList.toggle("selected", id === node.id);
    scheduleEdges();
  });
  details.append(summary);
  const body = element("div", "resource-body");
  if (node.tree) {
    if (node.changed) body.append(element("div", "diff-baseline", "HEAD → Working tree"));
    const compared = new Set();
    node.tree.children.forEach((child, index) => {
      // The name and unchanged description are already fully visible in the header.
      if (
        child.title.startsWith("Name:") ||
        child.title.startsWith("Description:") ||
        child.title === "Dependencies"
      )
        return;
      const field = child.title.match(/^[-+] (Name|Description|Help):/);
      if (field) {
        if (!compared.has(field[1])) {
          compared.add(field[1]);
          body.append(fieldComparison(node.tree.children, field[1]));
        }
        return;
      }
      if (!changesOnly || node.context || hasChange(child))
        body.append(behaviorTree(child, `${node.id}/tree/${index}`, changesOnly && !node.context));
    });
  }
  if (node.expression) body.append(element("div", "detail-leaf", node.expression));
  details.append(body);
  blocks.set(node.id, details);
  return details;
}

function collection(items, kind, edges) {
  const box = element("section", `collection-box ${kind}-collection`);
  const heading = element("h3", "collection-heading", `${kind}/`);
  heading.append(element("span", "", `${items.length} resources`));
  box.append(heading);
  const levels = dependencyLevels(items, edges);
  const lanes = element("div", "lanes");
  const maxDepth = Math.max(0, ...[...levels.values()].map((layout) => layout.depth));
  for (let depth = 0; depth <= maxDepth; depth += 1) {
    const lane = element("div", "lane");
    for (const node of items.filter((item) => levels.get(item.id).depth === depth))
      lane.append(resourceBlock(node, levels.get(node.id)));
    lanes.append(lane);
  }
  box.append(lanes);
  return box;
}

function render() {
  if (!data) return;
  if (layoutObserver) layoutObserver.disconnect();
  blocks.clear();
  currentNodes = visibleNodes();
  const canvas = $("canvas");
  canvas.replaceChildren();
  updateCollapseState();
  if (!currentNodes.length) {
    canvas.append(
      element(
        "p",
        "empty",
        changesOnly
          ? "No semantic changes match this filter. Try clearing the search or showing all resources."
          : "No resources match your search.",
      ),
    );
    return;
  }
  const stage = element("div", "graph-stage");
  stage.style.transform = `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`;
  for (const repository of new Set(currentNodes.map((node) => node.repository))) {
    const resources = currentNodes.filter((node) => node.repository === repository);
    const box = element("section", "repository-box");
    box.dataset.repository = repository;
    const heading = element(
      "h2",
      "repository-heading",
      `◫ ${repository === "." ? data.root.split("/").pop() : repository}`,
    );
    heading.append(element("span", "scope", "FLAKE REPOSITORY"));
    box.append(heading, element("div", "routing-space"));
    const body = element("div", "repository-body");
    const packages = resources.filter(
      (node) => node.kind === "package" || node.kind === "package-reference",
    );
    const hosts = resources.filter((node) => node.kind === "host");

    const other = resources.filter(
      (node) => !["package", "package-reference", "host", "check"].includes(node.kind),
    );
    for (const [items, kind] of [
      [packages, "packages"],
      [hosts, "hosts"],
      [other, "resources"],
    ])
      if (items.length) body.append(collection(items, kind, visibleEdges()));
    box.append(body);
    stage.append(box);
  }
  const overlay = svgElement("svg", {
    class: "graph-edges",
    "aria-label": "Dependency connections",
  });
  stage.prepend(overlay);
  stage.append(element("div", "edge-notes-layer"));
  canvas.append(stage);
  layoutObserver = new ResizeObserver(scheduleEdges);
  layoutObserver.observe(stage);
  for (const block of blocks.values()) layoutObserver.observe(block);
  updateCollapseState();
  scheduleEdges();
}

function drawEdges() {
  updateCollapseState();
  const stage = document.querySelector(".graph-stage");
  if (!stage) return;
  const svg = stage.querySelector(".graph-edges");
  svg.replaceChildren();
  svg.setAttribute("width", stage.offsetWidth);
  svg.setAttribute("height", stage.offsetHeight);
  svg.setAttribute("viewBox", `0 0 ${stage.offsetWidth} ${stage.offsetHeight}`);
  const defs = svgElement("defs");
  for (const [id, color] of [["dependency-arrow", "#81a589"]]) {
    const marker = svgElement("marker", {
      id,
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      orient: "auto",
    });
    marker.append(svgElement("path", { d: "M0 0 L10 5 L0 10 z", fill: color }));
    defs.append(marker);
  }
  svg.append(defs);
  const origin = stage.getBoundingClientRect();
  const rect = (item) => {
    const bounds = item.getBoundingClientRect();
    return {
      left: (bounds.left - origin.left) / zoom,
      right: (bounds.right - origin.left) / zoom,
      top: (bounds.top - origin.top) / zoom,
      bottom: (bounds.bottom - origin.top) / zoom,
      width: bounds.width / zoom,
    };
  };
  const notes = stage.querySelector(".edge-notes-layer");
  notes.replaceChildren();
  const edges = visibleEdges();
  edges.forEach((edge, index) => {
    const source = blocks.get(edge.source),
      target = blocks.get(edge.target);
    if (!source || !target) return;
    const from = rect(source.querySelector(".resource-header")),
      to = rect(target.querySelector(".resource-header"));
    const sx = from.right + 1,
      sy = from.top + 26,
      tx = to.left - 2,
      ty = to.top + 26;
    let path, lx, ly;
    if (tx > sx && tx - sx < 180) {
      const middle = (sx + tx) / 2 + ((index % 3) - 1) * 7;
      path = `M${sx},${sy} H${middle} V${ty} H${tx}`;
      lx = (sx + tx) / 2;
      ly = (sy + ty) / 2 - 6;
    } else if (Math.abs(from.left - to.left) < 5) {
      const gutter = Math.max(from.right, to.right) + 16 + (index % 3) * 9;
      path = `M${sx},${sy} H${gutter} V${ty} H${to.right + 2}`;
      lx = gutter + 24;
      ly = (sy + ty) / 2 - 6;
    } else {
      const repository = rect(source.closest(".repository-box"));
      const channel = repository.top + 65 + (index % 4) * 12;
      const exit = sx + 18 + (index % 3) * 9,
        entry = tx - 18 - (index % 3) * 9;
      path = `M${sx},${sy} H${exit} V${channel} H${entry} V${ty} H${tx}`;
      lx = (exit + entry) / 2;
      ly = channel - 5;
    }
    const line = svgElement("path", {
      d: path,
      class: `graph-edge ${edge.change || ""}${[edge.source, edge.target].includes(selected) ? " related" : ""}`,
      "marker-end": "url(#dependency-arrow)",
      tabindex: 0,
      role: "button",
      "aria-label": `${edge.source} → ${edge.target}: ${edge.kind}`,
      "data-source": edge.source,
      "data-target": edge.target,
    });
    const declaration = edge.declaration;
    line.append(
      svgElement(
        "title",
        {},
        `${edge.kind}: ${edge.source} → ${edge.target}${declaration ? `\n${declaration.path}:${declaration.line}\n${declaration.expression}` : ""}`,
      ),
    );
    const edgeKey = JSON.stringify([edge.source, edge.target, edge.kind]);
    const toggleEdge = () => {
      openedEdge = openedEdge === edgeKey ? null : edgeKey;
      scheduleEdges();
    };
    line.addEventListener("click", toggleEdge);
    line.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggleEdge();
      }
    });
    const label = svgElement(
      "text",
      { x: lx, y: ly, "text-anchor": "middle", class: `edge-label ${edge.change || ""}` },
      `${edge.change === "removed" ? "− " : edge.change === "added" ? "+ " : ""}${edge.kind}`,
    );
    label.addEventListener("click", toggleEdge);
    svg.append(line, label);
    if (openedEdge === edgeKey) {
      const note = element("div", `edge-note ${edge.change || ""}`);
      note.style.left = `${Math.max(12, Math.min(stage.offsetWidth - 320, lx - 140))}px`;
      note.style.top = `${Math.max(12, ly + 10)}px`;
      const heading = element(
        "div",
        "edge-note-heading",
        `${edge.kind}${edge.change ? ` · ${edge.change}` : ""}`,
      );
      const close = element("button", "", "×");
      close.setAttribute("aria-label", "Close dependency details");
      close.addEventListener("click", toggleEdge);
      heading.append(close);
      note.append(heading);
      for (const [id, arrow] of [
        [edge.source, ""],
        [edge.target, "→ "],
      ]) {
        const button = element(
          "button",
          "edge-resource",
          arrow + (data.nodes.find((node) => node.id === id)?.name || id),
        );
        button.addEventListener("click", () => focusResource(id));
        note.append(button);
      }
      if (declaration)
        note.append(
          element(
            "div",
            "detail-leaf",
            `${declaration.repository}/${declaration.path}:${declaration.line}\n${declaration.expression}`,
          ),
        );
      notes.append(note);
    }
  });
}

function applyViewport() {
  const stage = document.querySelector(".graph-stage");
  if (stage) stage.style.transform = `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`;
}

function fitGraph() {
  const stage = document.querySelector(".graph-stage");
  if (!stage) return;
  const width = $("canvas").clientWidth,
    height = $("canvas").clientHeight;
  zoom = Math.max(
    0.1,
    Math.min(1, (width - 32) / stage.offsetWidth, (height - 88) / stage.offsetHeight),
  );
  pan.x = (width - stage.offsetWidth * zoom) / 2;
  pan.y = 72 + (height - 72 - stage.offsetHeight * zoom) / 2;
  applyViewport();
  scheduleEdges();
}

function setZoom(value, x = $("canvas").clientWidth / 2, y = $("canvas").clientHeight / 2) {
  const next = Math.min(4, Math.max(0.1, value));
  pan.x = x - ((x - pan.x) / zoom) * next;
  pan.y = y - ((y - pan.y) / zoom) * next;
  zoom = next;
  applyViewport();
  scheduleEdges();
}

function reveal(item) {
  const bounds = item.getBoundingClientRect();
  const width = $("canvas").clientWidth,
    height = $("canvas").clientHeight;
  if (bounds.left < 24) pan.x += 24 - bounds.left;
  else if (bounds.right > width - 24) pan.x -= bounds.right - width + 24;
  if (bounds.top < 76) pan.y += 76 - bounds.top;
  else if (bounds.bottom > height - 24) pan.y -= bounds.bottom - height + 24;
  applyViewport();
}

async function refresh() {
  $("refresh").disabled = true;
  $("refresh").textContent = "…";
  try {
    const response = await fetch("/api/overview");
    const snapshot = await response.json();
    if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
    data = prepareData(snapshot);
    if (!initialFocusApplied && data.focus) {
      selected = data.focus;
      expanded.set(selected, true);
    }
    initialFocusApplied = true;
    $("message").hidden = !data.warning;
    $("message").textContent = data.warning || "";
    render();
  } catch (error) {
    $("message").hidden = false;
    $("message").textContent =
      `Could not read repository: ${error.message}. Use Refresh to try again.`;
    if (!data)
      $("canvas").replaceChildren(element("p", "empty", "Repository data is unavailable."));
  } finally {
    $("refresh").disabled = false;
    $("refresh").textContent = "↻";
  }
}

$("search").addEventListener("input", (event) => {
  query = event.target.value.trim().toLowerCase();
  searchCollapsed.clear();
  render();
  if (query)
    requestAnimationFrame(() => {
      const match =
        document.querySelector(".detail-leaf.match") ||
        blocks
          .get(currentNodes.find((node) => !node.context)?.id)
          ?.querySelector(".resource-header");
      if (match) reveal(match);
    });
});
$("changes").addEventListener("change", (event) => {
  changesOnly = event.target.checked;
  searchCollapsed.clear();
  render();
});
$("refresh").addEventListener("click", refresh);
$("fit").addEventListener("click", fitGraph);
$("collapse").addEventListener("click", () => {
  const expand = $("collapse").title === "Expand all";
  expanded.clear();
  for (const details of document.querySelectorAll(".graph-stage details")) {
    const key = details.dataset.expansionKey;
    if (expand) {
      expanded.set(key, true);
      searchCollapsed.delete(key);
    } else searchCollapsed.add(key);
  }
  openedEdge = null;
  selected = null;
  render();
  requestAnimationFrame(() => {
    const first = blocks.values().next().value;
    if (first) reveal(first.querySelector(".resource-header"));
  });
});
window.addEventListener("resize", scheduleEdges);
document.addEventListener("keydown", (event) => {
  const direction = {
    ArrowLeft: [1, 0],
    ArrowRight: [-1, 0],
    ArrowUp: [0, 1],
    ArrowDown: [0, -1],
  }[event.key];
  if (
    direction &&
    !event.defaultPrevented &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.target.closest("input,textarea,select,button,summary,[contenteditable]")
  ) {
    event.preventDefault();
    const step = event.shiftKey ? 160 : 40;
    pan.x += direction[0] * step;
    pan.y += direction[1] * step;
    applyViewport();
  }
  if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    event.preventDefault();
    $("search").focus();
  }
  if (event.key === "Escape" && document.activeElement === $("search")) {
    $("search").value = "";
    query = "";
    searchCollapsed.clear();
    render();
  }
});
let drag = null;
$("canvas").addEventListener("pointerdown", (event) => {
  if (
    event.button === 0 &&
    event.target.closest("details,button,input,.graph-edge,.edge-label,.edge-note")
  )
    return;
  if (![0, 1, 2].includes(event.button)) return;
  event.preventDefault();
  drag = {
    x: event.clientX,
    y: event.clientY,
    left: pan.x,
    top: pan.y,
  };
  $("canvas").setPointerCapture(event.pointerId);
  $("canvas").classList.add("panning");
});
$("canvas").addEventListener("pointermove", (event) => {
  if (drag) {
    pan.x = drag.left + event.clientX - drag.x;
    pan.y = drag.top + event.clientY - drag.y;
    applyViewport();
  }
});
function endDrag() {
  drag = null;
  $("canvas").classList.remove("panning");
}
$("canvas").addEventListener("pointerup", endDrag);
$("canvas").addEventListener("pointercancel", endDrag);
$("canvas").addEventListener("contextmenu", (event) => event.preventDefault());
$("canvas").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const delta =
      event.deltaY *
      (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? $("canvas").clientHeight : 1);
    setZoom(zoom * Math.exp(-delta * 0.0015), event.clientX, event.clientY);
  },
  { passive: false },
);
$("canvas").addEventListener("focusin", (event) => {
  if (event.target !== $("canvas")) requestAnimationFrame(() => reveal(event.target));
});
refresh();
