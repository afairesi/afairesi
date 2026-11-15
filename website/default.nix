{ inputs, pkgs, ... }:
inputs.self.lib.mkHtmlPackage {
  inherit pkgs;
  meta.description = "Afairesi: a Nix library and CLI for repository builds, inspection, and testing.";
  src = ./.;
}
