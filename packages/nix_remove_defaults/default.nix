{ inputs, pkgs, ... }:
(inputs.perigrafo or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Remove literal NixOS and treefmt assignments equal to option defaults";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.nix_syntax
    pkgs.nix
  ];
  src = ./.;
  version = "0.0.0";
}
