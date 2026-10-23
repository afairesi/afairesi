{ inputs, pkgs, ... }:
inputs.afairesi.lib.mkLean4Package {
  inherit pkgs;
  executable = false;
  meta.description = "Lean library consumer contract";
  postPatch = "test -f main.lean";
  src = ./.;
}
