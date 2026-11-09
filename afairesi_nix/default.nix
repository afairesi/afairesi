{ pkgs, ... }:
pkgs.stdenvNoCC.mkDerivation {
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp main.nix "$out/"
    runHook postInstall
  '';
  meta.description = "Nix library for Afairesi packages, checks, formatters, and hosts";
  pname = baseNameOf ./.;
  src = ./.;
  version = "0.0.0";
}
