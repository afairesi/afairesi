"use strict";

const $ = (id) => document.getElementById(id);
let data = null;
let query = "";
let changesOnly = false;
let selected = null;
let initialFocusApplied = false;
let fitPending = true;
let directory = new URLSearchParams(location.search).get("directory");
let zoom = 1;
const pan = { x: 0, y: 0 };
let currentNodes = [];
let frame = null;
let layoutObserver = null;
const layoutEngine = new ELK();
let layoutGeneration = 0;
let layoutFrame = null;
let layoutPromise = Promise.resolve();
const edgeRoutes = new Map();

function edgeId(edge) {
  return JSON.stringify([edge.source, edge.target, edge.kind]);
}

function scheduleLayout() {
  if (layoutFrame !== null) cancelAnimationFrame(layoutFrame);
  const generation = ++layoutGeneration;
  const stage = document.querySelector(".graph-stage");
  if (stage) stage.dataset.layout = "pending";
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = null;
    // Serialize engine calls and discard results from obsolete renders.
    layoutPromise = layoutPromise.catch(() => {}).then(() => layoutGraph(generation));
  });
}

async function layoutGraph(generation) {
  const stage = document.querySelector(".graph-stage");
  if (!stage || generation !== layoutGeneration) return;
  const aspectRatio = Math.max(
    0.5,
    Math.min(2, window.innerWidth / Math.max(240, window.innerHeight - 88)),
  );
  const direction = window.innerWidth <= 760 ? "DOWN" : "RIGHT";
  const elements = new Map();
  const routes = new Map();
  let serial = 0;
  const options = (algorithm, top = 0) => ({
    "elk.algorithm": algorithm,
    "elk.aspectRatio": String(aspectRatio),
    "elk.direction": direction,
    "elk.padding": `[top=${top},left=0,bottom=0,right=0]`,
    "elk.spacing.nodeNode": "24",
    "elk.spacing.componentComponent": "24",
    "elk.layered.spacing.nodeNodeBetweenLayers": "48",
    "elk.layered.compaction.connectedComponents": "true",
    "elk.edgeRouting": "ORTHOGONAL",
    "elk.randomSeed": "1",
  });
  async function collectionModel(box) {
    const id = `collection-${serial++}`;
    elements.set(id, box);
    const heading = box.querySelector(".collection-heading");
    const top = Math.ceil(heading.offsetHeight) + 32;
    const children = [...box.querySelectorAll(".resource-block")].map((block) => {
      elements.set(block.dataset.nodeId, block);
      return {
        id: block.dataset.nodeId,
        width: block.offsetWidth,
        height: block.offsetHeight,
        ports: [
          {
            id: `${block.dataset.nodeId}:out`,
            x: direction === "RIGHT" ? block.offsetWidth : block.offsetWidth / 2,
            y: direction === "RIGHT" ? 26 : block.offsetHeight,
            width: 0,
            height: 0,
            layoutOptions: { "elk.port.side": direction === "RIGHT" ? "EAST" : "SOUTH" },
          },
          {
            id: `${block.dataset.nodeId}:in`,
            x: direction === "RIGHT" ? 0 : block.offsetWidth / 2,
            y: direction === "RIGHT" ? 26 : 0,
            width: 0,
            height: 0,
            layoutOptions: { "elk.port.side": direction === "RIGHT" ? "WEST" : "NORTH" },
          },
        ],
        layoutOptions: { "elk.portConstraints": "FIXED_POS" },
      };
    });
    const connected = new Set(visibleEdges().flatMap((edge) => [edge.source, edge.target]));
    const isolated = children.filter((child) => !connected.has(child.id));
    if (isolated.length > 1) {
      // Compound layering otherwise puts every unrelated package in one tall lane.
      // Pack those cards as a fixed rectangle before arranging the dependency graph.
      const groupId = `isolated-${serial++}`;
      let group = box.querySelector(".packed-resources");
      if (!group) {
        group = element("div", "packed-resources");
        box.querySelector(".lanes").append(group);
      }
      elements.set(groupId, group);
      for (const child of isolated) {
        const block = elements.get(child.id);
        if (block.parentNode !== group) group.append(block);
      }
      const packed = await layoutEngine.layout({
        id: groupId,
        children: isolated,
        layoutOptions: options("rectpacking"),
      });
      if (generation !== layoutGeneration) return null;
      apply(packed, group);
      const isolatedIds = new Set(isolated.map((child) => child.id));
      children.splice(
        0,
        children.length,
        ...children.filter((child) => !isolatedIds.has(child.id)),
        { id: groupId, width: packed.width, height: packed.height },
      );
    }
    return {
      id,
      children,
      layoutOptions: {
        ...options("layered"),
        "elk.padding": `[top=${top},left=16,bottom=16,right=16]`,
      },
    };
  }
  function apply(model, body) {
    body.style.width = `${model.width}px`;
    body.style.height = `${model.height}px`;
    for (const child of model.children || []) {
      const item = elements.get(child.id);
      item.style.position = "absolute";
      item.style.left = `${child.x}px`;
      item.style.top = `${child.y}px`;
      item.style.width = `${child.width}px`;
      if (item.classList.contains("packed-resources")) item.style.height = `${child.height}px`;
      if (item.classList.contains("collection-box")) {
        item.style.height = `${child.height}px`;
        apply(child, item.querySelector(".lanes"));
      }
    }
    for (const edge of model.edges || []) {
      // ELK may keep an edge on an ancestor while giving its sections coordinates
      // relative to the lowest common container of its endpoints.
      const owner = elements.get(edge.container);
      routes.set(edge.id, { edge, body: owner?.querySelector(":scope > .lanes") || body });
    }
  }
  async function arrange(box) {
    const body = box.querySelector(":scope > .repository-body");
    const children = [];
    for (const item of body.children) {
      if (item.classList.contains("collection-box")) {
        const model = await collectionModel(item);
        if (!model || generation !== layoutGeneration) return;
        children.push(model);
      } else {
        await arrange(item);
        if (generation !== layoutGeneration) return;
        const id = `container-${serial++}`;
        elements.set(id, item);
        children.push({ id, width: item.offsetWidth, height: item.offsetHeight });
      }
    }
    // A repository's compound graph routes package/host links together. Directory
    // parents pack the already laid-out repositories as indivisible rectangles.
    const ids = new Set(children.flatMap((child) => (child.children || []).map((node) => node.id)));
    const edges = visibleEdges().filter((edge) => ids.has(edge.source) && ids.has(edge.target));
    const graph = {
      id: `body-${serial++}`,
      children,
      edges: edges.map((edge) => ({
        id: edgeId(edge),
        sources: [`${edge.source}:out`],
        targets: [`${edge.target}:in`],
        labels: [{ text: edge.kind, width: edge.kind.length * 6 + 8, height: 14 }],
      })),
      layoutOptions: {
        ...options(edges.length ? "layered" : "rectpacking"),
        "elk.hierarchyHandling": edges.length ? "INCLUDE_CHILDREN" : "SEPARATE_CHILDREN",
      },
    };
    // Rectangle packing expects fixed rectangles; independently lay out collections
    // when no cross-container dependency graph needs compound layout.
    if (!edges.length) {
      for (let index = 0; index < children.length; index += 1) {
        if (!children[index].children) continue;
        const result = await layoutEngine.layout(children[index]);
        if (generation !== layoutGeneration) return;
        apply(result, elements.get(result.id).querySelector(".lanes"));
        elements.get(result.id).style.width = `${result.width}px`;
        elements.get(result.id).style.height = `${result.height}px`;
        children[index] = { id: result.id, width: result.width, height: result.height };
      }
    }
    const result = await layoutEngine.layout(graph);
    if (generation !== layoutGeneration) return;
    apply(result, body);
    box.style.width = `${Math.max(320, result.width + 42)}px`;
  }
  try {
    await arrange(stage.querySelector(":scope > section"));
    if (generation !== layoutGeneration || !stage.isConnected) return;
    edgeRoutes.clear();
    for (const [id, route] of routes) edgeRoutes.set(id, route);
    stage.dataset.layout = "ready";
    drawEdges();
    if (fitPending) {
      fitPending = false;
      fitGraph();
    } else if (selected) {
      const header = blocks.get(selected)?.querySelector(".resource-header");
      if (header) reveal(header);
    }
  } catch (error) {
    if (generation !== layoutGeneration) return;
    $("message").hidden = false;
    $("message").textContent =
      `Could not arrange graph: ${error.message}. Use Refresh to try again.`;
  }
}

const expanded = new Map();
const searchCollapsed = new Set();
const blocks = new Map();

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
  const names = { python: "Python", html: "HTML", nix: "Nix", nixos: "NixOS", latex: "LaTeX" };
  if (!names[kind]) return null;
  const icon = svgElement("svg", {
    class: "language-icon",
    viewBox: "0 0 24 24",
    role: "img",
    "aria-label": names[kind],
  });
  icon.append(svgElement("title", {}, names[kind]));
  if (kind === "python") {
    const snake =
      "M12 2C6 2 6 3 6 6v3h7v1H4c-3 0-3 8 0 8h2v-3c0-3 2-4 5-4h5c2 0 3-1 3-3V6c0-3-2-4-7-4Z";
    icon.append(
      svgElement("path", { d: snake, fill: "#3776ab" }),
      svgElement("path", { d: snake, fill: "#ffd343", transform: "rotate(180 12 12)" }),
      svgElement("circle", { cx: 9, cy: 5, r: 1, fill: "white" }),
      svgElement("circle", { cx: 15, cy: 19, r: 1, fill: "white" }),
    );
  } else if (kind === "html") {
    icon.append(
      svgElement("path", { d: "M3 2h18l-2 18-7 2-7-2Z", fill: "#e44d26" }),
      svgElement("path", {
        d: "M7 6h10l-.2 3H10l.2 2h6.4l-.6 6-4 1-4-1-.3-3h3l.1 1 1.2.3 1.3-.3.2-2H7.6Z",
        fill: "white",
      }),
    );
  } else if (kind === "nix" || kind === "nixos") {
    for (let angle = 0; angle < 360; angle += 60)
      icon.append(
        svgElement("path", {
          d: "M12 12V2M12 6l-4-3M12 6l4-3",
          transform: `rotate(${angle} 12 12)`,
          stroke: angle % 120 ? "#5277c3" : "#7ebae4",
          "stroke-width": 2,
          fill: "none",
        }),
      );
  } else {
    icon.append(
      svgElement(
        "text",
        {
          x: 12,
          y: 16,
          "text-anchor": "middle",
          "font-family": "serif",
          "font-size": 12,
          "font-weight": "bold",
          fill: "#008080",
        },
        "TeX",
      ),
    );
  }
  return icon;
}

function hasChange(tree) {
  return Boolean(tree.change) || (tree.children || []).some(hasChange);
}

function countLeaves(tree) {
  if (/^(?:[-+] )?\((none|not applicable|not declared|unavailable:.*)\)$/.test(tree.title))
    return 0;
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
  return data.edges.filter((edge) => !["contains", "submodule"].includes(edge.kind));
}

function visibleNodes() {
  const resources = data.nodes.filter((node) => node.kind !== "repository");
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
    document.querySelector(".graph-stage details[open]") || [...expanded.values()].some(Boolean),
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
    scheduleLayout();
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
  const icon = languageIcon(node.icon || node.package_type);
  title.append(
    icon || element("i", "kind-dot"),
    element("strong", "", node.name),
    element("span", "chevron", "›"),
  );
  summary.append(title);
  if (node.description) summary.append(element("p", "resource-description", node.description));
  summary.append(element("div", "resource-path", node.path));
  const meta = element("div", "resource-meta");
  if (!icon) meta.append(element("span", "kind-tag", node.kind));
  if (node.context) details.classList.add("context");
  if (layout?.cyclic) meta.append(element("span", "change-badge", "dependency cycle"));
  if (node.changed)
    meta.append(
      element("span", "change-badge", node.removed ? "− Removed" : "◉ Changed"),
      changeTally(node.tree),
    );
  if (meta.childNodes.length) summary.append(meta);
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
        child.title === "Dependencies" ||
        child.title === "Connections"
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
  for (const node of items) lanes.append(resourceBlock(node, levels.get(node.id)));
  box.append(lanes);
  return box;
}

function render() {
  if (!data) return;
  if (layoutObserver) layoutObserver.disconnect();
  ++layoutGeneration;
  edgeRoutes.clear();
  blocks.clear();
  currentNodes = visibleNodes();
  const canvas = $("canvas");
  canvas.replaceChildren();
  updateCollapseState();
  if (!currentNodes.length && (query || changesOnly || !data.nodes.some((node) => node.kind === "repository"))) {
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
  const repositories = new Set(currentNodes.map((node) => node.repository));
  if (!query && !changesOnly)
    for (const node of data.nodes)
      if (node.kind === "repository") repositories.add(node.repository);
  // Build real path containment, including intermediate directories and repositories
  // that themselves contain another repository.
  const hierarchy = { name: data.root.split("/").pop(), path: ".", children: new Map() };
  for (const repository of [...repositories].sort()) {
    let parent = hierarchy;
    let path = "";
    for (const name of repository === "." ? [] : repository.split("/")) {
      path = path ? `${path}/${name}` : name;
      if (!parent.children.has(name))
        parent.children.set(name, { name, path, children: new Map() });
      parent = parent.children.get(name);
    }
    parent.repository = repository;
  }
  function container(item) {
    const record = data.nodes.find(
      (node) => node.kind === "repository" && node.repository === item.repository,
    );
    const box = element(
      "section",
      item.repository !== undefined ? "repository-box" : "directory-box",
    );
    if (item.repository !== undefined) box.dataset.repository = item.repository;
    box.dataset.path = item.path;
    const heading = element("h2", "repository-heading");
    const link = element("button", "directory-link", `◫ ${item.name}`);
    const destination = item.path === "." ? data.root : `${data.root}/${item.path}`;
    link.title = `Open ${destination}`;
    link.addEventListener("click", () => navigateDirectory(destination));
    heading.append(link);
    heading.append(
      element("span", "scope", record ? `${record.profile.toUpperCase()} REPOSITORY` : "DIRECTORY"),
    );
    box.append(heading);
    const body = element("div", "repository-body");
    const resources = currentNodes.filter((node) => node.repository === item.repository);
    for (const [kinds, kind] of [
      [["package", "package-reference"], "packages"],
      [["host"], "hosts"],
      [["check"], "checks"],
      [["machine"], "resources"],
    ]) {
      const items = resources.filter((node) => kinds.includes(node.kind));
      if (items.length) body.append(collection(items, kind, visibleEdges()));
    }
    for (const child of item.children.values()) body.append(container(child));
    box.append(body);
    return box;
  }
  stage.append(container(hierarchy));
  const overlay = svgElement("svg", {
    class: "graph-edges",
    "aria-label": "Package and host references",
  });
  stage.prepend(overlay);
  canvas.append(stage);
  const sizes = new WeakMap();
  const size = (block) => `${block.offsetWidth}:${block.offsetHeight}`;
  layoutObserver = new ResizeObserver((entries) => {
    let changed = false;
    for (const { target } of entries) {
      const next = size(target);
      if (sizes.get(target) !== next) changed = true;
      sizes.set(target, next);
    }
    if (changed) scheduleLayout();
  });
  for (const block of blocks.values()) {
    sizes.set(block, size(block));
    layoutObserver.observe(block);
  }
  updateCollapseState();
  scheduleLayout();
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
  const edges = visibleEdges();
  edges.forEach((edge, index) => {
    const source = blocks.get(edge.source),
      target = blocks.get(edge.target);
    if (!source || !target) return;
    const from = rect(source.querySelector(".resource-header")),
      to = rect(target.querySelector(".resource-header"));
    let path, lx, ly;
    const route = edgeRoutes.get(edgeId(edge));
    if (route?.edge.sections?.length) {
      const offset = rect(route.body);
      path = route.edge.sections
        .map((section) => {
          const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint];
          return points
            .map(
              (point, index) =>
                `${index ? "L" : "M"}${point.x + offset.left},${point.y + offset.top}`,
            )
            .join(" ");
        })
        .join(" ");
      const label = route.edge.labels?.[0];
      lx = offset.left + (label?.x || 0) + (label?.width || 0) / 2;
      ly = offset.top + (label?.y || 0) + 11;
    } else {
      const sx = from.right + 1,
        sy = from.top + 26,
        tx = to.left - 2,
        ty = to.top + 26;
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
    }
    const line = svgElement("path", {
      d: path,
      class: `graph-edge ${edge.change || ""}${[edge.source, edge.target].includes(selected) ? " related" : ""}`,
      "marker-end": "url(#dependency-arrow)",
      "data-source": edge.source,
      "data-target": edge.target,
    });
    line.append(svgElement("title", {}, `${edge.kind}: ${edge.source} → ${edge.target}`));
    const label = svgElement(
      "text",
      { x: lx, y: ly, "text-anchor": "middle", class: `edge-label ${edge.change || ""}` },
      `${edge.change === "removed" ? "− " : edge.change === "added" ? "+ " : ""}${edge.kind}`,
    );
    svg.append(line, label);
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

async function refresh(nextDirectory = directory) {
  $("refresh").disabled = true;
  $("refresh").textContent = "…";
  try {
    const response = await fetch(nextDirectory ? `/api/overview?directory=${encodeURIComponent(nextDirectory)}` : "/api/overview");
    const snapshot = await response.json();
    if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
    data = prepareData(snapshot);
    directory = data.root;
    $("parent").disabled = !data.parent;
    $("parent").title = data.parent ? `Go to ${data.parent}` : "No parent directory";
    const url = new URL(location.href);
    url.searchParams.set("directory", directory);
    history.replaceState(null, "", url);
    document.title = `Canonical — ${directory}`;
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

function navigateDirectory(nextDirectory) {
  expanded.clear();
  searchCollapsed.clear();
  selected = null;
  initialFocusApplied = false;
  fitPending = true;
  refresh(nextDirectory);
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
  fitPending = true;
  searchCollapsed.clear();
  render();
});
$("refresh").addEventListener("click", () => refresh());
$("parent").addEventListener("click", () => navigateDirectory(data.parent));
$("fit").addEventListener("click", () => requestAnimationFrame(() => layoutPromise.then(fitGraph)));
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
  selected = null;
  render();
  requestAnimationFrame(() => {
    const first = blocks.values().next().value;
    if (first) reveal(first.querySelector(".resource-header"));
  });
});
window.addEventListener("resize", scheduleLayout);
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
  if (event.button === 0 && event.target.closest("details,button,input")) return;
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
