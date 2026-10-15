{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Canonicalize ordering and nesting in Nix expressions";
  passthru.checkInputs = [ pkgs.nix ];
  propagatedBuildInputs = [ inputs.self.packages.${pkgs.stdenv.system}.nix_syntax ];
  src = ./.;
}
