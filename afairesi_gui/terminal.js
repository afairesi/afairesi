/* oxlint-disable no-await-in-loop -- Preserve the order of terminal input and output bytes. */
/* oxlint-disable func-style, no-use-before-define, one-var, sort-keys, sort-vars -- Keep terminal state and handlers together. */
/* oxlint-disable no-magic-numbers, no-ternary, no-undefined, unicorn/no-null -- Terminal dimensions and protocol defaults are numeric. */
/* oxlint-disable oxc/no-async-await, oxc/no-optional-chaining -- Native browser module. */

const container = document.querySelector("#terminal"),
  pane = document.querySelector("#codex-pane"),
  status = document.querySelector("#terminal-status"),
  startButton = document.querySelector("#terminal-start"),
  terminal = new globalThis.Terminal({
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 12,
    cursorBlink: true,
    scrollback: 5000,
    screenReaderMode: true,
    theme: {
      background: "#fdf6e3",
      foreground: "#073642",
      cursor: "#268bd2",
      selectionBackground: "#268bd244",
      black: "#073642",
      red: "#dc322f",
      green: "#859900",
      yellow: "#b58900",
      blue: "#268bd2",
      magenta: "#d33682",
      cyan: "#2aa198",
      white: "#eee8d5",
      brightBlack: "#657b83",
      brightWhite: "#fdf6e3",
    },
  }),
  fit = new globalThis.FitAddon.FitAddon();
let session = "",
  offset = 0,
  running = false,
  starting = false,
  polling = false,
  writing = Promise.resolve(),
  resizeTimer = 0;

async function request(path, body) {
  const response = await fetch(
      path,
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    ),
    result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || "The workspace is unavailable.");
  }
  return result;
}
function failed(error) {
  status.textContent = `Codex connection interrupted: ${error.message}`;
  startButton.hidden = false;
}
function encode(bytes) {
  return btoa(String.fromCodePoint(...bytes));
}
function input(data) {
  const destination = session;
  writing = writing
    .then(async () => {
      const bytes = new TextEncoder().encode(data);
      for (let index = 0; index < bytes.length; index += 4096) {
        await request("/api/terminal/input", {
          session: destination,
          data: encode(bytes.subarray(index, index + 4096)),
        });
      }
    })
    .catch(failed);
  return writing;
}
function resize() {
  fit.fit();
  clearTimeout(resizeTimer);
  if (running) {
    resizeTimer = setTimeout(() => {
      request("/api/terminal/resize", {
        session,
        columns: terminal.cols,
        rows: terminal.rows,
      }).catch(failed);
    }, 80);
  }
}
async function render(result) {
  if (result.reset) {
    terminal.reset();
  }
  ({ session, offset, running } = result);
  if (result.output) {
    const bytes = Uint8Array.from(atob(result.output), (character) => character.codePointAt(0));
    await new Promise((resolve) => {
      terminal.write(bytes, resolve);
    });
  }
  status.textContent = running
    ? "Codex"
    : `Codex exited${result.exit_code === null ? "" : ` (${result.exit_code})`}`;
  startButton.hidden = running;
}
async function receive() {
  const result = await request(`/api/terminal?${new URLSearchParams({ after: offset, session })}`);
  await render(result);
}
async function attach() {
  fit.fit();
  await render(
    await request("/api/terminal/start", { columns: terminal.cols, rows: terminal.rows }),
  );
  resize();
  poll();
}
async function poll() {
  if (polling) {
    return;
  }
  polling = true;
  try {
    // oxlint-disable-next-line no-unmodified-loop-condition -- receive updates running from the process status.
    while (running) {
      try {
        await receive();
      } catch (error) {
        failed(error);
        await new Promise((resolve) => {
          setTimeout(resolve, 1200);
        });
      }
    }
  } finally {
    polling = false;
  }
}
async function start() {
  if (starting) {
    return;
  }
  starting = true;
  startButton.disabled = true;
  try {
    await attach();
  } catch (error) {
    failed(error);
  } finally {
    starting = false;
    startButton.disabled = false;
  }
}

export async function pasteToCodex(text) {
  if (!running) {
    await start();
  }
  if (!running) {
    throw new Error("Start Codex before adding job output.");
  }
  terminal.focus();
  // oxlint-disable-next-line no-control-regex -- Strip terminal controls from job output before pasting.
  terminal.paste(text.replaceAll(/[\u0000-\u0008\u000B-\u001F\u007F]/gu, ""));
}

terminal.loadAddon(fit);
terminal.loadAddon(
  new globalThis.WebLinksAddon.WebLinksAddon((event, url) => {
    event.preventDefault();
    if (/^https?:\/\//u.test(url)) {
      globalThis.open(url, "_blank", "noopener,noreferrer");
    }
  }),
);
terminal.open(container);
terminal.onData(input);
new ResizeObserver(resize).observe(container);
startButton.addEventListener("click", start);

const handle = document.querySelector("#terminal-resize");
let drag = null;
function height(value) {
  const pixels = Math.max(160, Math.min(innerHeight - 80, value));
  pane.style.height = `${pixels}px`;
  handle.setAttribute("aria-valuenow", String(Math.round(pixels)));
  handle.setAttribute("aria-valuemin", "160");
  handle.setAttribute("aria-valuemax", String(innerHeight - 80));
}
handle.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) {
    return;
  }
  event.preventDefault();
  drag = { top: event.clientY, height: pane.offsetHeight };
  handle.setPointerCapture(event.pointerId);
});
handle.addEventListener("pointermove", (event) => {
  if (drag) {
    height(drag.height + drag.top - event.clientY);
  }
});
function endResize() {
  drag = null;
}
for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
  handle.addEventListener(name, endResize);
}
handle.addEventListener("keydown", (event) => {
  if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const sizes = {
      ArrowUp: pane.offsetHeight + 30,
      ArrowDown: pane.offsetHeight - 30,
      Home: 160,
      End: innerHeight - 80,
    };
    height(sizes[event.key]);
  }
});
height(pane.offsetHeight);
start();
