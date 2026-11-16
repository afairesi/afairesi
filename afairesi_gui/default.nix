{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "A local workspace for home submodules, resource actions, and Codex chat.";
  passthru.checkInputs = [ pkgs.nodejs ];
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
    pkgs.git
    pkgs.nix
  ];
  src = ./.;
}
