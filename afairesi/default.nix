{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = false;
  meta.description = "Extract and diff package test names as a behavioral specification.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.nix_syntax
    pkgs.git
    pkgs.python3Packages.tree-sitter-language-pack
    pkgs.shfmt
  ];
  src = ./.;
}
