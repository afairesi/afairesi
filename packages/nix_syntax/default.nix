{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
in
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  propagatedBuildInputs = [ python.pkgs.tree-sitter-language-pack ];
  src = ./.;
}
