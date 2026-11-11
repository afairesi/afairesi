{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Record an afairesi walkthrough as an asciinema cast";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.afairesi
    pkgs.bash
    pkgs.coreutils
    pkgs.git
    pkgs.jd-diff-patch
    pkgs.jless
    pkgs.nix
    pkgs.tree
  ];
  src = ./.;
}
