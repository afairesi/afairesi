{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "A workspace for home submodules, resource actions, and Codex chat, locally or over SSH.";
  passthru.checkInputs = [ pkgs.nodejs ];
  postPatch = ''
    substituteInPlace main.py \
      --replace-fail 'BUNDLED_CODEX = "codex"' 'BUNDLED_CODEX = "${pkgs.lib.getExe pkgs.codex}"' \
      --replace-fail 'SOURCE_REVISION = ""' 'SOURCE_REVISION = "${inputs.self.rev or ""}"'
  '';
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.afairesi
    pkgs.codex
    pkgs.git
    pkgs.nix
    pkgs.openssh
  ];
  src = ./.;
}
