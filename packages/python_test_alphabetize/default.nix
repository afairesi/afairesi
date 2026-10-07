{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
in
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Sort Python test definitions alphabetically within their scopes";
  propagatedBuildInputs = [ python.pkgs.libcst ];
  src = ./.;
  version = "0.0.0";
}
