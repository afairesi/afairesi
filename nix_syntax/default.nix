{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = false;
  meta.description = "Shared Nix parsing and rewriting helpers";
  propagatedBuildInputs = [ pkgs.python3Packages.tree-sitter-language-pack ];
  src = ./.;
}
