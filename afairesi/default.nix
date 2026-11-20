{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "afairesi CLI for inspecting repositories in the afairesi computing environment.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.nix_syntax
    pkgs.git
    pkgs.jd-diff-patch
    pkgs.python3.pkgs.libcst
  ];
  src = ./.;
}
