{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
in
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = false;
  meta.description = "Shared Nix parsing and rewriting helpers";
  propagatedBuildInputs = [
    pkgs.nix
    (python.pkgs.tree-sitter-language-pack.overridePythonAttrs (previous: {
      buildInputs = (previous.buildInputs or [ ]) ++ [ pkgs.zstd ];
      nativeBuildInputs = (previous.nativeBuildInputs or [ ]) ++ [ pkgs.pkg-config ];
    }))
  ];
  src = ./.;
}
