{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Remove carriage returns and line feeds from text files";
  propagatedBuildInputs = [ ];
  src = ./.;
  version = "0.0.0";
}
