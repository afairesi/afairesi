{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Manage the persistent home repository and its registered Git submodules";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
    pkgs.git
    pkgs.nix
  ];
  src = ./.;
}
