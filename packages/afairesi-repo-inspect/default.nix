{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Inspect repository hosts, packages, argparse arguments, and test names";
  propagatedBuildInputs = [ inputs.self.packages.${pkgs.stdenv.system}.afairesi ];
  src = ./.;
}
