{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Browse package test names and their diffs in a cached, expandable terminal tree.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.afairesi
    pkgs.git
    pkgs.jd-diff-patch
  ];
  src = ./.;
}
