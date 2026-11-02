{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Remove literal assignments equal to NixOS, treefmt, or package defaults";
  passthru.tests.nixos = import ./prm/nixos-check.nix {
    inherit pkgs;
    package = inputs.self.packages.${pkgs.stdenv.system}.remove_nix_defaults;
  };
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.nix_syntax
    pkgs.nix
  ];
  src = ./.;
}
