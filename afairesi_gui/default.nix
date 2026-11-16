{ inputs, pkgs, ... }:
let
  fit = pkgs.fetchurl {
    hash = "sha256-JgA7RRehMrZOT/Io/Yil/aP/9eYGx2CT9tz/dy6ezsA=";
    url = "https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.11.0.tgz";
  };
  links = pkgs.fetchurl {
    hash = "sha256-v0Y8//GvS7GQNQmDDGLtBrzQWJA58V6l+6cde65ahto=";
    url = "https://registry.npmjs.org/@xterm/addon-web-links/-/addon-web-links-0.12.0.tgz";
  };
  terminalAssets = pkgs.runCommand "afairesi-terminal-assets" { } ''
    mkdir -p "$out"
    tar -xOf ${xterm} package/lib/xterm.js > "$out/xterm.js"
    tar -xOf ${xterm} package/css/xterm.css > "$out/xterm.css"
    tar -xOf ${xterm} package/LICENSE > "$out/xterm-LICENSE"
    tar -xOf ${fit} package/lib/addon-fit.js > "$out/addon-fit.js"
    tar -xOf ${fit} package/LICENSE > "$out/addon-fit-LICENSE"
    tar -xOf ${links} package/lib/addon-web-links.js > "$out/addon-web-links.js"
    tar -xOf ${links} package/LICENSE > "$out/addon-web-links-LICENSE"
  '';
  xterm = pkgs.fetchurl {
    hash = "sha256-kI5m4Er2yNxrAN07VN4Iji6B5e2GYoT9bC+zwtHHo/Y=";
    url = "https://registry.npmjs.org/@xterm/xterm/-/xterm-6.0.0.tgz";
  };
in
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "A workspace for home submodules, resource actions, and the native Codex terminal, locally or over SSH.";
  passthru.checkInputs = [ pkgs.nodejs ];
  postPatch = ''
    substituteInPlace main.py \
      --replace-fail 'BUNDLED_CODEX = "codex"' 'BUNDLED_CODEX = "${pkgs.lib.getExe pkgs.codex}"' \
      --replace-fail 'TERMINAL_ASSETS = ""' 'TERMINAL_ASSETS = "${terminalAssets}"' \
      --replace-fail 'SOURCE_REVISION = ""' 'SOURCE_REVISION = "${inputs.self.rev or ""}"'
  '';
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
    pkgs.codex
    pkgs.git
    pkgs.nix
    pkgs.openssh
  ];
  src = ./.;
}
