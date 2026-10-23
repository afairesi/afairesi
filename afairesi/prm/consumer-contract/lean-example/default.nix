{
  inputs,
  pkgs,
  system,
  ...
}:
inputs.afairesi.lib.mkLean4Package {
  inherit pkgs;
  executable = true;
  leanDeps = [ inputs.self.packages.${system}.lean-library ];
  meta.description = "Lean executable consumer contract";
  runtimeHook = ''
    export AFAIRESI_LEAN4_HOOK=loaded
  '';
  src = ./.;
}
