{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
in
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Parse, validate, and rewrite Nix source files";
  propagatedBuildInputs = [ python.pkgs.tree-sitter-language-pack ];
  src = ./.;
  version = "0.0.0";
}
