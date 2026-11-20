{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Browse home repositories and interface diffs in a cached, expandable terminal tree.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.afairesi
    pkgs.git
    pkgs.jd-diff-patch
  ];
  src = ./.;
}
