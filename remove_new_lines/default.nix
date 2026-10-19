{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Remove carriage returns and line feeds from text files";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.remove_empty_lines
  ];
  src = ./.;
}
