{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  propagatedBuildInputs = [ inputs.self.packages.${pkgs.stdenv.system}.nix_syntax ];
  src = ./.;
}
