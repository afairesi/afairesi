{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "A workspace for home submodules, resource actions, and Codex chat, locally or over SSH.";
  passthru.checkInputs = [ pkgs.nodejs ];
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
    pkgs.git
    pkgs.nix
    pkgs.openssh
  ];
  src = ./.;
}
