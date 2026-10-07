{ inputs, pkgs, ... }:
(inputs.perigrafo or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Canonicalize ordering and nesting in Nix expressions";
  propagatedBuildInputs = [ inputs.self.packages.${pkgs.stdenv.system}.nix_syntax ];
  src = ./.;
  version = "0.0.0";
}
