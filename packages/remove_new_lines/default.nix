{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  src = ./.;
}
