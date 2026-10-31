const container = document.querySelector("#demo-player");
if (container) {
  globalThis.AsciinemaPlayer.create("prm/afairesi-demo.cast", container, {
    cols: 100,
    controls: true,
    fit: "width",
    poster: "npt:3",
    rows: 30,
    theme: "solarized-light",
  });
}
