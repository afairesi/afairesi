{ inputs, pkgs, ... }:
(inputs.perigrafo or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Remove blank and whitespace-only lines from text files";
  propagatedBuildInputs = [ ];
  src = ./.;
  version = "0.0.0";
}
