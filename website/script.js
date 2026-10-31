const container = document.querySelector("#demo-player");
if (container) {
  globalThis.AsciinemaPlayer.create({ data: globalThis.afairesiDemoRecording }, container, {
    cols: 100,
    controls: true,
    fit: "width",
    poster: "npt:3",
    rows: 30,
    theme: "solarized-light",
  });
}
