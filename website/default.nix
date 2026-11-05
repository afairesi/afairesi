{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkHtmlPackage {
  inherit pkgs;
  meta.description = "An opinionated CLI and Nix flake library for managing a Git-based home directory, repositories, packages, and NixOS configurations.";
  postInstall = ''
    cp ${pkgs.writeText "afairesi-demo.js" ''
      globalThis.afairesiDemoRecording = ${builtins.toJSON (builtins.readFile ./prm/afairesi-demo.cast)};
    ''} "$out/prm/afairesi-demo.js"
  '';
  src = ./.;
}
