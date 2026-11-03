const container = document.querySelector("#demo-player");
if (container) {
  globalThis.AsciinemaPlayer.create({ data: globalThis.afairesiDemoRecording }, container, {
    controls: true,
    poster: "npt:3",
    theme: "solarized-light",
  });
}
